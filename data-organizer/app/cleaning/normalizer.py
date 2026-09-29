"""Low-level text and location normalisation.

Everything here is *pure*: it never mutates source values, it only derives normalised forms.
Unicode is handled with the ``regex`` module so that combining marks (Devanagari matras, accents)
survive punctuation stripping instead of being torn out of words.
"""
from __future__ import annotations

import math
import re
import unicodedata
from functools import lru_cache
from typing import Iterable

import regex

from ..config import load_resource

_WS = regex.compile(r"\s+")
_CTRL = regex.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_ZERO_WIDTH = regex.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")
_PUNCT = regex.compile(r"[^\p{L}\p{M}\p{N}\s]+")
_APOS = regex.compile(r"['\u2019\u2018`\u00b4]")
NULL_LIKE = {
    "", "n/a", "na", "n.a", "n.a.", "nil", "none", "null", "nan", "-", "--", "---", ".", "..", "not available",
    "not applicable", "not provided", "not given", "unknown", "?", "??", "no", "#n/a", "#ref!", "#value!", "<na>",
}
_SMALL_WORDS = {"and", "of", "the", "in", "at", "for", "on"}


def to_text(value) -> str:
    """Convert any cell value to a plain string ('' for None/NaN). Does not alter content otherwise."""
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
    return str(value)


def basic_clean(text: str) -> str:
    """Unicode NFKC, drop control/zero-width chars, collapse whitespace. Keeps case and punctuation."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    text = _ZERO_WIDTH.sub("", text)
    text = _CTRL.sub(" ", text)
    return _WS.sub(" ", text).strip()


def is_null_like(text: str) -> bool:
    return basic_clean(text).lower() in NULL_LIKE


def strip_null_like(text: str) -> str:
    t = basic_clean(text)
    return "" if t.lower() in NULL_LIKE else t


def normalize_text(text: str) -> str:
    """lower-case, '&'->'and', apostrophes removed, punctuation -> space, whitespace collapsed."""
    t = basic_clean(text).lower()
    if not t:
        return ""
    t = t.replace("&", " and ")
    t = _APOS.sub("", t)
    t = _PUNCT.sub(" ", t)
    return _WS.sub(" ", t).strip()


def title_case(text: str) -> str:
    words = normalize_text(text).split()
    out = []
    for i, w in enumerate(words):
        out.append(w if (w in _SMALL_WORDS and i > 0) else w.capitalize())
    return " ".join(out)


@lru_cache(maxsize=1)
def _address_abbr() -> dict[str, str]:
    return load_resource("abbreviations.json").get("address_safe", {})


def normalize_address(text: str) -> str:
    t = normalize_text(text)
    if not t:
        return ""
    abbr = _address_abbr()
    return " ".join(abbr.get(tok, tok) for tok in t.split())


# ---------------------------------------------------------------------------
# Gazetteer: cities / districts / states
# ---------------------------------------------------------------------------
class Gazetteer:
    """Normalises place names and knows which (well-known) city belongs to which state.

    It is *evidence only*: it never fills in a missing city/state. Cities observed in the data can
    be registered at runtime with :meth:`add_places`.
    """

    def __init__(self, data: dict | None = None):
        data = data or load_resource("places.json")
        self.state_lookup: dict[str, str] = {}
        for state, aliases in data.get("states", {}).items():
            self.state_lookup[normalize_text(state)] = state
            for a in aliases:
                self.state_lookup.setdefault(normalize_text(a), state)
        self.city_synonyms = {normalize_text(k): normalize_text(v) for k, v in data.get("city_synonyms", {}).items()}
        self.city_state: dict[str, str] = {}
        for state, cities in data.get("cities", {}).items():
            for c in cities:
                self.city_state[normalize_text(c)] = state
        self.pin_prefix = {k: v for k, v in data.get("pincode_prefix_state", {}).items() if not k.startswith("_")}
        self._extra: set[str] = set()
        self._max_words = 3

    # -- normalisation -----------------------------------------------------
    def normalize_state(self, raw: str) -> str:
        t = normalize_text(strip_null_like(raw))
        if not t:
            return ""
        return self.state_lookup.get(t, t)

    def normalize_city(self, raw: str) -> str:
        t = normalize_text(strip_null_like(raw))
        if not t:
            return ""
        t = self.city_synonyms.get(t, t)
        # "jaipur rajasthan" -> "jaipur"
        for alias, state in self.state_lookup.items():
            if len(alias) > 3 and t.endswith(" " + alias) and len(t) > len(alias) + 2:
                t = t[: -len(alias) - 1].strip()
                break
        return self.city_synonyms.get(t, t)

    def normalize_district(self, raw: str) -> str:
        t = normalize_text(strip_null_like(raw))
        t = re.sub(r"\s+(district|dist)$", "", t)
        return self.city_synonyms.get(t, t)

    def state_of_city(self, city_norm: str) -> str | None:
        return self.city_state.get(city_norm)

    def pincode_state(self, pin: str) -> str | None:
        return self.pin_prefix.get(pin[:2]) if pin and len(pin) >= 2 else None

    # -- place vocabulary --------------------------------------------------
    def add_places(self, names: Iterable[str]) -> None:
        for n in names:
            n = normalize_text(n)
            if len(n) >= 3 and not n.isdigit():
                self._extra.add(n)
                self._max_words = max(self._max_words, len(n.split()))

    def is_place(self, phrase: str) -> bool:
        return phrase in self.city_state or phrase in self._extra or phrase in self.state_lookup

    def trailing_place(self, tokens: list[str]) -> tuple[str, int]:
        """Longest place-name suffix of ``tokens`` -> (canonical place, number of tokens)."""
        for n in range(min(self._max_words, len(tokens)), 0, -1):
            phrase = " ".join(tokens[-n:])
            phrase = self.city_synonyms.get(phrase, phrase)
            if self.is_place(phrase):
                return phrase, n
        return "", 0


def display_place(norm: str) -> str:
    """'sri ganganagar' -> 'Sri Ganganagar'; keeps small words lower-case ('jammu and kashmir')."""
    if not norm:
        return ""
    parts = norm.split()
    return " ".join(w if (w in _SMALL_WORDS and i > 0) else w.capitalize() for i, w in enumerate(parts))
