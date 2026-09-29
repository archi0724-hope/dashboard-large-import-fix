"""Maps whatever column headers a file uses onto the universal schema.

    "Hospital Name" | "Organization" | "Name of Institution" | ...   ->  name
    "Mobile No." | "Tel" | "Contact Number"                          ->  phone

Resolution order (first hit wins): user override -> exact alias -> keyword rule -> fuzzy alias ->
(for unmapped columns only) content sniffing. The ORIGINAL header is always kept next to the mapped
field, so nothing is lost and every mapping is inspectable/overridable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from rapidfuzz import fuzz

from .config import UNIVERSAL_FIELDS, load_resource
from .cleaning.normalizer import basic_clean, normalize_text
from .cleaning.validators import GLOBALLY_UNIQUE_ID_TYPES, parse_registration_id

SINGLE_VALUED = ("name", "city", "district", "state", "pincode", "category")


@dataclass
class HeaderMatch:
    field: str | None
    confidence: float
    method: str            # override | exact | keyword | fuzzy | content | none

    def to_dict(self) -> dict:
        return {"field": self.field, "confidence": round(self.confidence, 2), "method": self.method}


@dataclass
class ColumnMap:
    headers: list[str]
    matches: list[HeaderMatch]
    fields: dict[str, list[int]] = field(default_factory=dict)

    def indices(self, fld: str) -> list[int]:
        return self.fields.get(fld, [])

    def to_dict(self) -> dict:
        return {
            "columns": [
                {"original": h, **m.to_dict(), "used": any(i in idx for idx in self.fields.values())}
                for i, (h, m) in enumerate(zip(self.headers, self.matches))
            ],
            "fields": {k: [self.headers[i] for i in v] for k, v in self.fields.items()},
        }


class SchemaMapper:
    def __init__(self, overrides: dict[str, str] | None = None, aliases: dict | None = None):
        data = aliases or load_resource("schema_aliases.json")
        self.overrides = {k.strip(): v for k, v in (overrides or {}).items()}
        self._overrides_norm = {normalize_text(k.split("::")[-1]): v for k, v in self.overrides.items() if "::" not in k}
        self.alias_to_field: dict[str, str] = {}
        for fld, aliases_ in data["fields"].items():
            for a in aliases_:
                self.alias_to_field.setdefault(normalize_text(a), fld)
        self.never_name = [normalize_text(x) for x in data.get("never_name", [])]
        self.rules = data.get("keyword_rules", [])
        self.multi_value = set(data.get("multi_value_fields", []))
        self.fuzzy_min = float(data.get("fuzzy_min_score", 90))

    # ------------------------------------------------------------------ header
    def map_header(self, header: str, file_name: str = "") -> HeaderMatch:
        h = basic_clean(header)
        if not h:
            return HeaderMatch(None, 0.0, "none")
        for key in (f"{file_name}::{h}", h):
            if key in self.overrides:
                f = self.overrides[key]
                return HeaderMatch(None if f == "ignore" else f, 1.0, "override")
        n = normalize_text(h)
        if n in self._overrides_norm:
            f = self._overrides_norm[n]
            return HeaderMatch(None if f == "ignore" else f, 1.0, "override")
        if not n:
            return HeaderMatch(None, 0.0, "none")

        padded = f" {n} "
        excluded_name = any(f" {p} " in padded for p in self.never_name)

        f = self.alias_to_field.get(n)
        if f:
            if f == "name" and excluded_name:
                return HeaderMatch(None, 0.0, "none")
            return HeaderMatch(f, 1.0, "exact")

        for rule in self.rules:
            fld = rule["field"]
            if fld == "name" and excluded_name:
                continue
            if "any" in rule and any(f" {normalize_text(p)} " in padded for p in rule["any"]):
                return HeaderMatch(fld, 0.85, "keyword")
            if "all" in rule and all(f" {normalize_text(p)} " in padded for p in rule["all"]):
                return HeaderMatch(fld, 0.8, "keyword")

        if len(n) >= 4:
            best_alias, best_score = None, 0.0
            for alias in self.alias_to_field:
                if len(alias) < 4:
                    continue
                sc = fuzz.ratio(n, alias)
                if sc > best_score:
                    best_alias, best_score = alias, sc
            if best_alias and best_score >= self.fuzzy_min:
                fld = self.alias_to_field[best_alias]
                if not (fld == "name" and excluded_name):
                    return HeaderMatch(fld, round(best_score / 100 * 0.9, 2), "fuzzy")
        return HeaderMatch(None, 0.0, "none")

    # -------------------------------------------------------------- header row
    def count_recognised(self, row: Sequence[str]) -> int:
        seen = set()
        for cell in row:
            if not cell or len(str(cell)) > 60:
                continue
            m = self.map_header(str(cell))
            if m.field and m.method in ("exact", "keyword", "fuzzy", "override"):
                seen.add(m.field)
        return len(seen)

    def detect_header_row(self, rows: Sequence[Sequence[str]], max_scan: int = 30) -> int | None:
        """Index of the most header-like row within the first ``max_scan`` rows, else None."""
        best_idx, best_score = None, 0
        for i, row in enumerate(rows[:max_scan]):
            non_empty = [c for c in row if str(c).strip()]
            if len(non_empty) < 2:
                continue
            score = self.count_recognised(non_empty)
            # header cells are short text; penalise rows that look like data (numbers, long text)
            numeric = sum(1 for c in non_empty if re.fullmatch(r"[\d.,\-/\s]+", str(c)))
            if numeric > len(non_empty) / 2:
                continue
            if score > best_score:
                best_idx, best_score = i, score
        return best_idx if best_score >= 2 or (best_score == 1 and best_idx is not None and self._has_name(rows[best_idx])) else None

    def _has_name(self, row: Sequence[str]) -> bool:
        return any(self.map_header(str(c)).field == "name" for c in row if c)

    # ---------------------------------------------------------------- columns
    def map_columns(
        self,
        headers: Sequence[str],
        sample_columns: Sequence[Sequence[str]] | None = None,
        file_name: str = "",
    ) -> ColumnMap:
        matches = [self.map_header(h, file_name) for h in headers]
        # content sniffing for columns nothing else claimed
        if sample_columns:
            for i, m in enumerate(matches):
                if m.field is None and m.method == "none" and i < len(sample_columns):
                    sniffed = sniff_column(sample_columns[i])
                    if sniffed:
                        matches[i] = HeaderMatch(sniffed, 0.6, "content")
            if not any(m.field == "name" for m in matches):
                guess = self._guess_name_column(matches, sample_columns)
                if guess is not None:
                    matches[guess] = HeaderMatch("name", 0.5, "content")
        cmap = ColumnMap(list(headers), matches)
        for i, m in enumerate(matches):
            if m.field:
                cmap.fields.setdefault(m.field, []).append(i)
        # single-valued fields keep only the best column (highest confidence, then leftmost)
        for fld in SINGLE_VALUED:
            idxs = cmap.fields.get(fld)
            if idxs and len(idxs) > 1:
                best = sorted(idxs, key=lambda i: (-matches[i].confidence, i))[0]
                cmap.fields[fld] = [best]
        return cmap

    @staticmethod
    def _guess_name_column(matches: Sequence[HeaderMatch], samples: Sequence[Sequence[str]]) -> int | None:
        best, best_score = None, 0.0
        for i, col in enumerate(samples):
            if i >= len(matches) or matches[i].field:
                continue
            vals = [str(v).strip() for v in col if str(v).strip()]
            if len(vals) < 3:
                continue
            texty = sum(1 for v in vals if re.search(r"[^\W\d_]{3,}", v) and not v.replace(" ", "").isdigit())
            unique = len(set(v.lower() for v in vals)) / len(vals)
            score = (texty / len(vals)) * 0.7 + unique * 0.3
            if texty / len(vals) >= 0.7 and unique >= 0.5 and score > best_score:
                best, best_score = i, score
        return best


_PHONE_LIKE = re.compile(r"^\+?[\d\s\-().]{8,16}$")


def sniff_column(values: Sequence[str], threshold: float = 0.6) -> str | None:
    vals = [str(v).strip() for v in values if str(v).strip()]
    if len(vals) < 2:
        return None

    def share(pred) -> float:
        return sum(1 for v in vals if pred(v)) / len(vals)

    if share(lambda v: "@" in v and "." in v.split("@")[-1]) >= threshold:
        return "email"
    if share(lambda v: bool(re.match(r"^(https?://|www\.)\S+$", v, re.I))) >= threshold:
        return "website"
    if share(lambda v: parse_registration_id(v).type in GLOBALLY_UNIQUE_ID_TYPES) >= threshold:
        return "registration_id"
    if share(lambda v: bool(_PHONE_LIKE.match(v)) and len(re.sub(r"\D", "", v)) in (10, 11, 12)) >= threshold:
        return "phone"
    return None


def join_values(values: list[str], fld: str) -> str:
    """Combine multiple source columns that map to the same multi-valued field, keeping every value."""
    vals = [v for v in (str(x).strip() for x in values) if v]
    if not vals:
        return ""
    if len(vals) == 1:
        return vals[0]
    sep = {"address": ", ", "phone": " / ", "email": "; ", "website": " ", "registration_id": "; "}.get(fld, "; ")
    return sep.join(vals)


def universal_fields() -> tuple[str, ...]:
    return UNIVERSAL_FIELDS
