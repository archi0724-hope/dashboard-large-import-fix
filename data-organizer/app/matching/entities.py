"""In-memory master-entity state, used by the resolver and the review service.

An ``EntityState`` aggregates everything known about one real-world entity (aliases, places, contact data,
identifiers). It is persisted twice: as flat columns in ``master_entities`` (for exports/dashboard) and as a compact
JSON snapshot (``state_json``) so a later run - or a review decision - can pick up exactly where the last one ended
without re-reading every record.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..cleaning.name_cleaner import NameCleaner, NameForms
from ..cleaning.normalizer import display_place

MAX_ALIASES = 60
MAX_ADDRESSES = 8


@dataclass
class AliasInfo:
    alias: str                     # first observed raw spelling
    count: int = 1
    source: str = ""               # "file | sheet/page | row n" of the first occurrence
    score: float = 100.0
    method: str = ""
    verified: bool = False

    def to_json(self) -> dict:
        return {"a": self.alias, "n": self.count, "s": self.source, "sc": round(self.score, 1), "m": self.method, "v": self.verified}

    @classmethod
    def from_json(cls, d: dict) -> "AliasInfo":
        return cls(d["a"], d.get("n", 1), d.get("s", ""), d.get("sc", 100.0), d.get("m", ""), d.get("v", False))


@dataclass
class EntityState:
    id: str
    aliases: dict[str, AliasInfo] = field(default_factory=dict)          # alias_normalized -> info
    cities: Counter = field(default_factory=Counter)
    districts: Counter = field(default_factory=Counter)
    states: Counter = field(default_factory=Counter)
    pincodes: Counter = field(default_factory=Counter)
    addr_norm: Counter = field(default_factory=Counter)
    addr_raw: Counter = field(default_factory=Counter)
    phones: Counter = field(default_factory=Counter)
    emails: Counter = field(default_factory=Counter)
    websites: Counter = field(default_factory=Counter)                     # normalised url
    domains: set[str] = field(default_factory=set)                         # NON-generic web/email domains only
    regs: dict[str, set[str]] = field(default_factory=dict)                # type -> values
    categories: Counter = field(default_factory=Counter)
    type_hints: Counter = field(default_factory=Counter)
    files: set[str] = field(default_factory=set)
    record_count: int = 0
    strength: int = 0
    score_min: float = 100.0
    verification_status: str = "auto"      # auto | provisional | verified | merged
    name_locked: bool = False
    standard_name: str = ""
    merged_into: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now().replace(microsecond=0))
    dirty: bool = True

    # ------------------------------------------------------------- properties
    @staticmethod
    def _top(c: Counter) -> str:
        return c.most_common(1)[0][0] if c else ""

    @property
    def city(self) -> str:
        return self._top(self.cities)

    @property
    def state(self) -> str:
        return self._top(self.states)

    # ------------------------------------------------------------- mutation
    def add_alias(self, alias_norm: str, alias_raw: str, source: str, score: float, method: str, verified: bool = False) -> None:
        info = self.aliases.get(alias_norm)
        if info:
            info.count += 1
            info.verified = info.verified or verified
            if score > info.score and info.method.startswith("Exact") is False:
                info.score, info.method = score, method
        elif len(self.aliases) < MAX_ALIASES:
            self.aliases[alias_norm] = AliasInfo(alias_raw, 1, source, score, method, verified)
        self.dirty = True

    def add_record(self, rec: Any, score: float, method: str, source: str) -> None:
        """Fold one record (a ``RecordFeatures``) into the entity."""
        f = rec.forms
        self.add_alias(f.normalized, rec.raw_name, source, score, method)
        if rec.city_col:
            self.cities[rec.city_col] += 1
        elif rec.city:
            self.cities[rec.city] += 1
        if rec.district:
            self.districts[rec.district] += 1
        if rec.state:
            self.states[rec.state] += 1
        if rec.pincode:
            self.pincodes[rec.pincode] += 1
        if rec.address_norm:
            if len(self.addr_norm) < MAX_ADDRESSES or rec.address_norm in self.addr_norm:
                self.addr_norm[rec.address_norm] += 1
        if rec.address_raw and (len(self.addr_raw) < MAX_ADDRESSES or rec.address_raw in self.addr_raw):
            self.addr_raw[rec.address_raw] += 1
        for p in rec.phones:
            self.phones[p] += 1
        for e in rec.emails:
            self.emails[e] += 1
        if rec.website:
            self.websites[rec.website] += 1
        self.domains |= rec.domains
        for t, v in rec.regs.items():
            self.regs.setdefault(t, set()).add(v)
        if rec.category:
            self.categories[rec.category] += 1
        if rec.type_hint:
            self.type_hints[rec.type_hint] += 1
        if rec.source_file:
            self.files.add(rec.source_file)
        self.record_count += 1
        self.strength = max(self.strength, rec.strength)
        self.score_min = min(self.score_min, score)
        self.dirty = True

    def absorb(self, other: "EntityState") -> None:
        """Merge another entity into this one (used when a reviewer accepts a match)."""
        for k, info in other.aliases.items():
            mine = self.aliases.get(k)
            if mine:
                mine.count += info.count
                mine.verified = mine.verified or info.verified
            elif len(self.aliases) < MAX_ALIASES:
                self.aliases[k] = info
        for name in ("cities", "districts", "states", "pincodes", "addr_norm", "addr_raw", "phones", "emails",
                     "websites", "categories", "type_hints"):
            getattr(self, name).update(getattr(other, name))
        self.domains |= other.domains
        for t, vals in other.regs.items():
            self.regs.setdefault(t, set()).update(vals)
        self.files |= other.files
        self.record_count += other.record_count
        self.strength = max(self.strength, other.strength)
        self.score_min = min(self.score_min, other.score_min)
        self.dirty = True

    # --------------------------------------------------------- standard name
    def choose_standard_name(self, cleaner: NameCleaner) -> str:
        """Pick the best *observed* spelling (never an invented one): fuller words beat abbreviations."""
        if self.name_locked and self.standard_name:
            return self.standard_name
        best_key, best = None, ""
        for norm, info in self.aliases.items():
            forms = cleaner.clean(info.alias)
            words = [t for t in forms.tokens if len(t) >= 3]
            quality = len(words) - len(forms.notes) + 0.5 * math.log(1 + info.count) + (3 if info.verified else 0)
            key = (quality, len(forms.core))
            if best_key is None or key > best_key:
                best_key, best = key, self._core_display(forms)
        self.standard_name = best or self.standard_name
        return self.standard_name

    @staticmethod
    def _core_display(forms: NameForms) -> str:
        words = forms.display.split()
        if forms.place_hint:
            n = len(forms.place_hint.split())
            trimmed = words[:-n] if len(words) > n else words
            while len(trimmed) > 1 and trimmed[-1].lower() in {"in", "at", "of", "the", "and", "near"}:
                trimmed.pop()
            words = trimmed
        return " ".join(words)

    # ---------------------------------------------------------------- output
    def conflicts(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for name, counter in (("city", self.cities), ("district", self.districts), ("state", self.states)):
            if len(counter) > 1:
                out[name] = sorted(counter)
        for t, vals in self.regs.items():
            if t in ("gst", "cin", "udyam", "pan") and len(vals) > 1:
                out[f"registration_id ({t})"] = sorted(vals)
        return out

    def to_master_row(self) -> dict[str, Any]:
        primary_reg = ""
        for t in ("gst", "cin", "udyam", "pan", "other"):
            if self.regs.get(t):
                primary_reg = sorted(self.regs[t])[0]
                break
        return {
            "master_entity_id": self.id,
            "standard_name": self.standard_name,
            "name_normalized": max(self.aliases, key=lambda k: self.aliases[k].count) if self.aliases else "",
            "aliases": json.dumps(sorted({i.alias for i in self.aliases.values()}), ensure_ascii=False),
            "city": display_place(self.city), "district": display_place(self._top(self.districts)),
            "state": display_place(self.state),
            "address": self._top(self.addr_raw), "pincode": self._top(self.pincodes),
            "phone": self._top(self.phones), "email": self._top(self.emails), "website": self._top(self.websites),
            "registration_id": primary_reg,
            "all_phones": json.dumps(sorted(self.phones)), "all_emails": json.dumps(sorted(self.emails)),
            "all_websites": json.dumps(sorted(self.websites)),
            "entity_type": self._top(self.type_hints), "category": self._top(self.categories),
            "confidence": round(self.score_min, 1), "verification_status": self.verification_status,
            "record_count": self.record_count, "source_file_count": len(self.files),
            "evidence_strength": self.strength,
            "data_conflicts": json.dumps(self.conflicts(), ensure_ascii=False) if self.conflicts() else "",
            "state_json": self.to_json(), "merged_into": self.merged_into,
            "created_at": self.created_at, "updated_at": datetime.now().replace(microsecond=0),
        }

    def alias_rows(self) -> list[dict[str, Any]]:
        return [
            {"alias": i.alias, "alias_normalized": k, "master_entity_id": self.id, "original_source": i.source,
             "match_score": i.score, "match_method": i.method, "occurrences": i.count, "verified": i.verified}
            for k, i in self.aliases.items()
        ]

    # ----------------------------------------------------------- persistence
    def to_json(self) -> str:
        d = {
            "al": {k: v.to_json() for k, v in self.aliases.items()},
            "ci": self.cities, "di": self.districts, "st": self.states, "pi": self.pincodes,
            "an": self.addr_norm, "ar": self.addr_raw, "ph": self.phones, "em": self.emails, "we": self.websites,
            "do": sorted(self.domains), "rg": {t: sorted(v) for t, v in self.regs.items()},
            "ca": self.categories, "th": self.type_hints, "fi": sorted(self.files),
            "rc": self.record_count, "sg": self.strength, "sm": self.score_min, "vs": self.verification_status,
            "nl": self.name_locked, "sn": self.standard_name, "mi": self.merged_into,
        }
        return json.dumps(d, ensure_ascii=False, default=dict)

    @classmethod
    def from_json(cls, entity_id: str, payload: str, created_at: datetime | None = None) -> "EntityState":
        d = json.loads(payload)
        e = cls(id=entity_id, created_at=created_at or datetime.now())
        e.aliases = {k: AliasInfo.from_json(v) for k, v in d.get("al", {}).items()}
        for attr, key in (("cities", "ci"), ("districts", "di"), ("states", "st"), ("pincodes", "pi"), ("addr_norm", "an"),
                          ("addr_raw", "ar"), ("phones", "ph"), ("emails", "em"), ("websites", "we"),
                          ("categories", "ca"), ("type_hints", "th")):
            setattr(e, attr, Counter(d.get(key, {})))
        e.domains = set(d.get("do", []))
        e.regs = {t: set(v) for t, v in d.get("rg", {}).items()}
        e.files = set(d.get("fi", []))
        e.record_count = d.get("rc", 0)
        e.strength = d.get("sg", 0)
        e.score_min = d.get("sm", 100.0)
        e.verification_status = d.get("vs", "auto")
        e.name_locked = d.get("nl", False)
        e.standard_name = d.get("sn", "")
        e.merged_into = d.get("mi")
        e.dirty = False
        return e
