"""Name normalisation engine.

Turns messy names into comparable forms *without ever replacing the original*:

    "S.M.S. Hosp., Jaipur"  ->  normalized: "sms hospital jaipur"
                                core:       "sms hospital"     (trailing place removed)
                                place_hint: "jaipur"

Rules
-----
* case / whitespace / punctuation are folded ("A.B.C. Hospital, Jaipur" -> "abc hospital jaipur")
* dotted or spaced initials are collapsed ("S M S" / "S.M.S." -> "sms")
* ``safe`` abbreviations (hosp, pvt, ltd, co, corp, ...) are always expanded
* ``contextual`` abbreviations (dr, st, med, inst, ...) are AMBIGUOUS and never expanded in the
  normalised name; they only generate alternative *variants* that the matcher may try, and a match
  that exists only through a variant is capped and routed to manual review unless other evidence
  backs it up.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Iterable

import regex

from ..config import load_resource
from .normalizer import Gazetteer, basic_clean, normalize_text

_DOTTED_INITIALS = regex.compile(r"(?<![\p{L}\p{N}])\p{L}(?:\.\p{L})+(?![\p{L}\p{N}])\.?")
_TRAILING_CONNECTORS = {"in", "at", "of", "the", "and", "near", "opp", "opposite"}
_VOWELS = set("aeiou")
_NOT_ACRONYMS = {"dr", "st", "mr", "mrs", "ms", "jr", "sr", "nd", "rd", "th"}


@dataclass(frozen=True)
class NameForms:
    raw: str
    display: str
    normalized: str
    core: str
    place_hint: str
    tokens: tuple[str, ...]
    sig_tokens: tuple[str, ...]
    acronym_key: str
    variants: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def compact(self) -> str:
        return self.core.replace(" ", "")

    @property
    def is_empty(self) -> bool:
        return not self.normalized

    def to_dict(self) -> dict:
        return {
            "raw": self.raw, "display": self.display, "normalized": self.normalized, "core": self.core,
            "place_hint": self.place_hint, "sig_tokens": list(self.sig_tokens), "acronym_key": self.acronym_key,
            "variants": list(self.variants), "notes": list(self.notes),
        }


class NameCleaner:
    def __init__(
        self,
        entity_type: str = "hospital",
        gazetteer: Gazetteer | None = None,
        abbreviations: dict | None = None,
        profiles: dict | None = None,
        collapse_initials: bool = True,
        cache_size: int = 200_000,
    ):
        abbr = abbreviations or load_resource("abbreviations.json")
        profiles = profiles or load_resource("entity_profiles.json")
        profile = profiles.get(entity_type) or profiles["generic"]
        self.entity_type = entity_type
        self.gazetteer = gazetteer or Gazetteer()
        self.safe: dict[str, str] = {normalize_text(k): normalize_text(v) for k, v in abbr.get("safe", {}).items()}
        self.contextual: dict[str, list[str]] = {
            normalize_text(k): [normalize_text(x) for x in v] for k, v in abbr.get("contextual", {}).items()
        }
        self._protected = self._compile_protected(abbr.get("protected_phrases", {}))
        self.generic = frozenset(profile.get("generic_tokens", []))
        self.titles = frozenset(profile.get("titles", []))
        self.distinguishing = frozenset(profile.get("distinguishing_tokens", []))
        self.type_keywords: dict[str, str] = profile.get("type_keywords", {})
        self.collapse_initials = collapse_initials
        self._cache: dict[str, NameForms] = {}
        self._cache_size = cache_size

    # ------------------------------------------------------------------ setup
    @staticmethod
    def _compile_protected(mapping: dict[str, str]) -> list[tuple[re.Pattern, str]]:
        out = []
        for phrase in sorted(mapping, key=len, reverse=True):
            pat = re.escape(phrase.lower()).replace(r"\ ", r"\s*")
            out.append((re.compile(rf"(?<!\w){pat}(?!\w)"), mapping[phrase]))
        return out

    # ---------------------------------------------------------------- public
    def clean(self, raw: str | None) -> NameForms:
        raw = "" if raw is None else str(raw)
        hit = self._cache.get(raw)
        if hit is not None:
            return hit
        forms = self._clean_uncached(raw)
        if len(self._cache) >= self._cache_size:
            self._cache.clear()
        self._cache[raw] = forms
        return forms

    def is_generic(self, token: str) -> bool:
        return token in self.generic

    def type_hint(self, normalized: str) -> str:
        """Entity type ONLY when a keyword literally appears in the name; otherwise ''."""
        padded = f" {normalized} "
        for kw, label in self.type_keywords.items():
            if f" {kw}" in padded:
                return label
        return ""

    def forms_from_normalized(
        self, normalized: str, core: str | None = None, place_hint: str = "", variants: Iterable[str] = ()
    ) -> NameForms:
        """Rebuild a NameForms object from stored values (used when reloading masters from the DB)."""
        core = core if core is not None else normalized
        tokens = tuple(core.split())
        sig = self._sig(tokens)
        return NameForms(
            raw=normalized, display="", normalized=normalized, core=core, place_hint=place_hint,
            tokens=tokens, sig_tokens=sig, acronym_key=self._acronym_key(sig), variants=tuple(variants),
        )

    # --------------------------------------------------------------- internals
    def _sig(self, tokens: Iterable[str]) -> tuple[str, ...]:
        return tuple(t for t in tokens if t not in self.generic)

    @staticmethod
    def _acronym_key(sig: tuple[str, ...]) -> str:
        letters = [t[0] for t in sig if t and t[0].isalpha()]
        return "".join(letters) if len(sig) >= 2 and len(letters) == len(sig) else ""

    def _clean_uncached(self, raw: str) -> NameForms:
        notes: list[str] = []
        text = basic_clean(raw).lower()
        if not text:
            return NameForms(raw, "", "", "", "", (), (), "", (), ())

        for pat, repl in self._protected:
            text = pat.sub(f" {repl} " if repl else " ", text)

        collapsed: set[str] = set()
        if self.collapse_initials:
            def _join(m: regex.Match) -> str:
                joined = m.group(0).replace(".", "")
                collapsed.add(joined)
                return f" {joined} "
            text = _DOTTED_INITIALS.sub(_join, text)

        text = normalize_text(text)
        tokens: list[str] = []
        for tok in text.split():
            exp = self.safe.get(tok)
            if exp and exp != tok:
                notes.append(f"{tok}->{exp}")
                tokens.extend(exp.split())
            else:
                tokens.append(tok)

        if self.collapse_initials:
            tokens, spaced = self._collapse_spaced_initials(tokens)
            collapsed |= spaced

        normalized = " ".join(tokens)

        # ---- core: drop honorifics and a trailing place name ----------------
        core_tokens = list(tokens)
        while len(core_tokens) > 1 and core_tokens[0] in self.titles:
            core_tokens.pop(0)
        place_hint = ""
        place, n = self.gazetteer.trailing_place(core_tokens)
        if n and len(core_tokens) > n:
            core_tokens = core_tokens[:-n]
            place_hint = place
            while len(core_tokens) > 1 and core_tokens[-1] in _TRAILING_CONNECTORS:
                core_tokens.pop()
        core = " ".join(core_tokens)
        sig = self._sig(core_tokens)

        variants = self._variants(core_tokens)
        display = self._display(raw, tokens, collapsed)
        return NameForms(
            raw=raw, display=display, normalized=normalized, core=core, place_hint=place_hint,
            tokens=tuple(core_tokens), sig_tokens=sig, acronym_key=self._acronym_key(sig),
            variants=tuple(variants), notes=tuple(notes),
        )

    @staticmethod
    def _collapse_spaced_initials(tokens: list[str]) -> tuple[list[str], set[str]]:
        out: list[str] = []
        joined_set: set[str] = set()
        run: list[str] = []

        def flush() -> None:
            if len(run) >= 2:
                j = "".join(run)
                out.append(j)
                joined_set.add(j)
            else:
                out.extend(run)
            run.clear()

        for t in tokens:
            if len(t) == 1 and t.isalpha():
                run.append(t)
            else:
                flush()
                out.append(t)
        flush()
        return out, joined_set

    def _variants(self, core_tokens: list[str], limit: int = 4) -> list[str]:
        """Alternative cores obtained by expanding AMBIGUOUS abbreviations. Never used as the normal form."""
        amb = [(i, t) for i, t in enumerate(core_tokens) if t in self.contextual]
        if not amb:
            return []
        options = [self.contextual[t] for _, t in amb]
        out: list[str] = []
        for combo in itertools.islice(itertools.product(*options), limit):
            toks = list(core_tokens)
            for (i, _), exp in zip(amb, combo):
                toks[i] = exp
            v = " ".join(toks)
            if v not in out:
                out.append(v)
        return out

    def _display(self, raw: str, tokens: list[str], collapsed: set[str]) -> str:
        """A presentable rendering built from an OBSERVED name (never invents words)."""
        raw_clean = basic_clean(raw)
        letters = [c for c in raw_clean if c.isalpha()]
        all_caps = bool(letters) and all(c.isupper() for c in letters)
        raw_words = {w for w in regex.split(r"[^\p{L}\p{N}]+", raw_clean) if w}
        raw_upper = {w.lower() for w in raw_words if len(w) >= 2 and w.isupper()}
        out = []
        for i, t in enumerate(tokens):
            if t in collapsed or (not all_caps and t in raw_upper):
                out.append(t.upper())
            elif (
                t.isalpha() and len(t) <= 4 and not (set(t) & _VOWELS) and t.isascii()
                and t not in _NOT_ACRONYMS and t not in self.titles and t not in self.contextual
            ):
                out.append(t.upper())
            elif t in {"and", "of", "the"} and i > 0:
                out.append(t)
            else:
                out.append(t.capitalize())
        return " ".join(out)
