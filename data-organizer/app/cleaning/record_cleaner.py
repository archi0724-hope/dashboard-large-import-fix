"""RAW record -> CLEANED record (normalised values + data-quality flags).

The raw row is never modified. Everything derived here goes to ``cleaned_records`` / ``quality_issues``; invalid
values are flagged (and kept in the raw layer) but are never used as matching evidence.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from ..config import RuntimeConfig
from .name_cleaner import NameCleaner
from .normalizer import Gazetteer, display_place, normalize_address, normalize_text, strip_null_like
from .validators import (
    GENERIC_EMAIL_DOMAINS, GLOBALLY_UNIQUE_ID_TYPES, Flag, email_domain, parse_emails, parse_phones,
    parse_pincode, parse_registration_id, parse_website, validate_name,
)

_PIN_IN_TEXT = re.compile(r"(?<!\d)([1-9]\d{2})\s?(\d{3})(?!\d)")
_SEV_RANK = {"": 0, "info": 1, "warning": 2, "error": 3}


class RecordCleaner:
    def __init__(self, cfg: RuntimeConfig, gazetteer: Gazetteer | None = None, name_cleaner: NameCleaner | None = None):
        self.cfg = cfg
        self.gaz = gazetteer or Gazetteer()
        self.names = name_cleaner or NameCleaner(entity_type=cfg.entity_type, gazetteer=self.gaz)
        self._known_states = set(self.gaz.state_lookup.values())

    def clean(self, raw: dict[str, Any], source_year: int | None = None) -> tuple[dict[str, Any], list[Flag]]:
        flags: list[Flag] = []
        raw_name = strip_null_like(raw.get("original_name") or "")
        forms = self.names.clean(raw_name)

        name_flags = validate_name(raw_name, forms.normalized)
        if name_flags and name_flags[0].issue_type == "missing_name":
            role = self._text_role(raw)
            if role and role != "entity":
                name_flags = [Flag("not_an_entity_line", "info", "name", "",
                                   f"Text line kept for traceability but not treated as an entity ({role.replace('_', ' ')})")]
        flags += name_flags

        # ---- location -------------------------------------------------------------
        city_norm = self.gaz.normalize_city(raw.get("original_city") or "")
        district_norm = self.gaz.normalize_district(raw.get("original_district") or "")
        state_raw = raw.get("original_state") or ""
        state_norm = self.gaz.normalize_state(state_raw)
        if state_norm and state_norm not in self._known_states:
            flags.append(Flag("unknown_state", "warning", "state", str(state_raw), "State is not recognised (typo or unlisted region?)"))
        if city_norm and (city_norm.isdigit() or len(city_norm) < 2):
            flags.append(Flag("invalid_city", "warning", "city", str(raw.get("original_city")), "City value looks invalid"))
            city_norm = ""
        pin, pin_ok = parse_pincode(raw.get("original_pincode") or "")
        if raw.get("original_pincode") and not pin_ok:
            flags.append(Flag("invalid_pincode", "warning", "pincode", str(raw.get("original_pincode")), "PIN code must have 6 digits"))
            pin = ""
        if not pin and raw.get("original_address"):
            # a PIN code written inside the address text is still an explicit value from the source
            m = _PIN_IN_TEXT.search(str(raw["original_address"]))
            if m:
                pin = m.group(1) + m.group(2)
        expected_state = self.gaz.state_of_city(city_norm) if city_norm else None
        if expected_state and state_norm and expected_state != state_norm:
            flags.append(Flag("city_state_mismatch", "warning", "state", f"{raw.get('original_city')} / {state_raw}",
                              f"{display_place(city_norm)} is normally in {display_place(expected_state)}, but the record says {display_place(state_norm)}"))
        if pin and state_norm:
            pin_state = self.gaz.pincode_state(pin)
            if pin_state and pin_state != state_norm:
                flags.append(Flag("pincode_state_mismatch", "warning", "pincode", pin,
                                  f"PIN {pin} belongs to {display_place(pin_state)}, record says {display_place(state_norm)}"))
        if city_norm and forms.place_hint and forms.place_hint != city_norm:
            flags.append(Flag("city_name_conflict", "warning", "city", f"{forms.place_hint} vs {city_norm}",
                              "The place named inside the entity name differs from the city column"))

        # ---- contact / identifiers ---------------------------------------------------
        phones = parse_phones(raw.get("original_phone") or "")
        for bad in phones.invalid:
            flags.append(Flag("invalid_phone", "warning", "phone", bad, "Not a valid phone number"))
        for bad in phones.placeholder:
            flags.append(Flag("placeholder_phone", "warning", "phone", bad, "Placeholder / dummy phone number"))
        emails = parse_emails(raw.get("original_email") or "")
        for bad in emails.invalid:
            flags.append(Flag("invalid_email", "warning", "email", bad, "Not a valid e-mail address"))
        web = parse_website(raw.get("original_website") or "")
        if web.raw and not web.valid:
            flags.append(Flag("invalid_website", "warning", "website", web.raw, "Not a valid website address"))
        reg = parse_registration_id(raw.get("original_registration_id") or "")
        if reg.invalid:
            flags.append(Flag("invalid_registration_id", "warning", "registration_id", "; ".join(reg.invalid),
                              reg.message or "Registration identifier has an unrecognised format"))

        address_norm = normalize_address(raw.get("original_address") or "")
        strength = self._evidence_strength(
            bool(city_norm or forms.place_hint), bool(state_norm), bool(district_norm), len(address_norm) >= 8, bool(pin),
            bool(phones.valid), emails.valid, bool(web.valid), reg,
        )
        worst = max((f.severity for f in flags), key=lambda s: _SEV_RANK[s], default="")
        resolvable = bool(forms.normalized) and not any(f.severity == "error" and f.field == "name" for f in flags)
        row = {
            "record_id": raw["record_id"],
            "name_normalized": forms.normalized, "name_core": forms.core, "name_display": forms.display,
            "place_hint": forms.place_hint, "name_variants": json.dumps(list(forms.variants)),
            "acronym_key": forms.acronym_key,
            "city_norm": city_norm, "city_display": display_place(city_norm),
            "district_norm": district_norm, "district_display": display_place(district_norm),
            "state_norm": state_norm, "state_display": display_place(state_norm),
            "pincode": pin, "address_norm": address_norm,
            "phone_norm": phones.valid[0] if phones.valid else "", "phones_all": json.dumps(phones.valid),
            "email_norm": emails.valid[0] if emails.valid else "", "emails_all": json.dumps(emails.valid),
            "website_norm": web.url, "website_domain": web.domain,
            "registration_id_norm": reg.value, "registration_type": reg.type,
            "category_norm": normalize_text(raw.get("original_category") or ""),
            "entity_type_hint": self.names.type_hint(forms.normalized),
            "source_year": source_year, "evidence_strength": strength,
            "quality_severity": worst or "ok",
            "quality_flags": json.dumps([f.issue_type for f in flags]),
            "is_resolvable": resolvable, "cleaned_at": datetime.now().replace(microsecond=0),
        }
        return row, flags

    @staticmethod
    def _evidence_strength(city, state, district, address, pin, phone, emails, web, reg) -> int:
        score = 15 * city + 5 * state + 5 * district + 15 * address + 10 * pin + 15 * phone + 10 * web
        if emails:
            score += 10 if any(email_domain(e) not in GENERIC_EMAIL_DOMAINS for e in emails) else 5
        if reg.valid:
            score += 15 if reg.type in GLOBALLY_UNIQUE_ID_TYPES else 5
        return min(100, score)

    @staticmethod
    def _text_role(raw: dict[str, Any]) -> str:
        data = raw.get("original_data")
        if not data or '"text_role"' not in data:
            return ""
        try:
            return json.loads(data).get("text_role", "")
        except (TypeError, ValueError):
            return ""
