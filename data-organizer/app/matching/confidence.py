"""Confidence scoring: turns a (record, entity) pair into a score, a band and an explanation.

Design rules (from the specification):
* similar names are only *possible* matches - a fuzzy name match is never auto-merged without corroborating
  evidence (city, address, phone, e-mail, website, registration id, PIN code);
* identical identifiers (GSTIN/CIN/Udyam/PAN) are the strongest evidence; contradicting identifiers or places are
  the strongest counter-evidence;
* anything that should not be merged automatically is flagged ``forced_review`` and capped below the auto-merge
  threshold, so lowering the threshold can never silently merge an ambiguous pair;
* every decision carries a per-field evidence checklist and a plain-language reason.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

from ..cleaning.name_cleaner import NameCleaner, NameForms
from ..cleaning.validators import GENERIC_EMAIL_DOMAINS, GENERIC_WEB_DOMAINS, email_domain
from ..config import RuntimeConfig
from .entities import EntityState
from .fuzzy_match import NameSim, name_similarity

BAND_ORDER = ("Very High", "High", "Possible Match", "Manual Review", "Probably Different")
UNIQUE_ID_TYPES = {"gst", "cin", "udyam", "pan"}
MIN_NAME_SCORE = 70.0        # below this, similar-looking names are different entities unless an identifier says otherwise


def band_for(score: float, t: dict[str, float]) -> str:
    if score >= t["very_high"]:
        return "Very High"
    if score >= t["high"]:
        return "High"
    if score >= t["possible"]:
        return "Possible Match"
    if score >= t["review"]:
        return "Manual Review"
    return "Probably Different"


@dataclass
class RecordFeatures:
    record_id: str
    forms: NameForms
    raw_name: str
    city: str = ""              # effective city: column value, else the place named inside the name
    city_col: str = ""
    district: str = ""
    state: str = ""
    pincode: str = ""
    address_norm: str = ""
    address_raw: str = ""
    phones: frozenset[str] = frozenset()
    emails: frozenset[str] = frozenset()
    website: str = ""
    domains: frozenset[str] = frozenset()
    regs: dict[str, str] = field(default_factory=dict)
    category: str = ""
    type_hint: str = ""
    source_file: str = ""
    source_ref: str = ""
    strength: int = 0

    @property
    def alias_norm(self) -> str:
        return self.forms.normalized

    @classmethod
    def from_row(cls, row: dict[str, Any], forms: NameForms) -> "RecordFeatures":
        phones = frozenset(json.loads(row.get("phones_all") or "[]"))
        emails = frozenset(json.loads(row.get("emails_all") or "[]"))
        domains = {d for d in (row.get("website_domain") or "",) if d and d not in GENERIC_WEB_DOMAINS}
        domains |= {d for d in (email_domain(e) for e in emails) if d and d not in GENERIC_EMAIL_DOMAINS}
        regs: dict[str, str] = {}
        if row.get("registration_id_norm"):
            regs[row.get("registration_type") or "other"] = row["registration_id_norm"]
        city_col = row.get("city_norm") or ""
        loc = f'{row.get("source_sheet") or ""}'.strip() or (f'page {row["source_page"]}' if row.get("source_page") else "")
        ref = " | ".join(x for x in (row.get("source_file") or "", loc, f'row {row["source_row"]}' if row.get("source_row") else "") if x)
        return cls(
            record_id=row["record_id"], forms=forms, raw_name=row.get("original_name") or "",
            city=city_col or forms.place_hint, city_col=city_col,
            district=row.get("district_norm") or "", state=row.get("state_norm") or "", pincode=row.get("pincode") or "",
            address_norm=row.get("address_norm") or "", address_raw=(row.get("original_address") or "").strip(),
            phones=phones, emails=emails, website=row.get("website_norm") or "", domains=frozenset(domains), regs=regs,
            category=(row.get("original_category") or "").strip(), type_hint=row.get("entity_type_hint") or "",
            source_file=row.get("source_file") or "", source_ref=ref, strength=int(row.get("evidence_strength") or 0),
        )


@dataclass
class Evidence:
    key: str
    status: str            # match | conflict | missing | partial
    detail: str = ""

    def to_dict(self) -> dict:
        return {"key": self.key, "status": self.status, "detail": self.detail}


@dataclass
class PairResult:
    entity_id: str
    score: float
    name_score: float
    name_method: str
    evidence: list[Evidence]
    corroborations: int
    forced_review: bool
    reason: str
    method: str
    strong_conflict: bool = False
    capped_by: str = ""
    best_alias: str = ""

    def evidence_dicts(self) -> list[dict]:
        return [e.to_dict() for e in self.evidence]


def _cmp_set(rec_val: str, ent_counter, label: str) -> Evidence:
    if not rec_val:
        return Evidence(label, "missing", "record has no value")
    if not ent_counter:
        return Evidence(label, "missing", "master record has no value")
    if rec_val in ent_counter:
        return Evidence(label, "match", rec_val)
    return Evidence(label, "conflict", f"{rec_val} vs {', '.join(list(ent_counter)[:3])}")


def score_pair(rec: RecordFeatures, ent: EntityState, cfg: RuntimeConfig, cleaner: NameCleaner) -> PairResult:
    # ---- name (best alias) -----------------------------------------------------
    best: NameSim | None = None
    best_alias = ""
    for info in sorted(ent.aliases.values(), key=lambda i: -i.count)[:25]:
        sim = name_similarity(rec.forms, cleaner.clean(info.alias), cleaner.distinguishing)
        if best is None or sim.score > best.score:
            best, best_alias = sim, info.alias
    best = best or NameSim(0.0, "none")
    score = best.score
    ev: list[Evidence] = [Evidence("name", "match" if best.score >= 85 else "partial",
                                   f'{best.score:.0f}% similar to "{best_alias}"' + (f" ({'; '.join(best.notes)})" if best.notes else ""))]

    # ---- identifiers -----------------------------------------------------------
    id_match = False
    reg_conflict = False
    reg_ev = Evidence("registration_id", "missing", "no identifier on one side")
    for t, v in rec.regs.items():
        known = ent.regs.get(t)
        if known and v in known:
            reg_ev, id_match = Evidence("registration_id", "match", f"{t.upper()} {v}"), True
            score += 30 if t in UNIQUE_ID_TYPES else 10
        elif known and t in UNIQUE_ID_TYPES:
            reg_ev, reg_conflict = Evidence("registration_id", "conflict", f"{v} vs {', '.join(sorted(known))}"), True
    ev.append(reg_ev)

    phone_hit = rec.phones & set(ent.phones)
    if phone_hit:
        ev.append(Evidence("phone", "match", sorted(phone_hit)[0]))
        score += 12
    else:
        ev.append(Evidence("phone", "missing" if not rec.phones or not ent.phones else "partial",
                           "no phone on one side" if not rec.phones or not ent.phones else "different numbers"))
    email_hit = rec.emails & set(ent.emails)
    dom_hit = rec.domains & ent.domains
    if email_hit:
        ev.append(Evidence("email", "match", sorted(email_hit)[0]))
        score += 10
    elif dom_hit:
        ev.append(Evidence("email", "match", "same organisation domain " + sorted(dom_hit)[0]))
        score += 8
    else:
        ev.append(Evidence("email", "missing", "no e-mail/domain on one side" if not rec.emails or not ent.emails else "different"))
    web_hit = bool(rec.website) and rec.website in ent.websites
    if web_hit:
        ev.append(Evidence("website", "match", rec.website))
        score += 6 if dom_hit else 10
    else:
        ev.append(Evidence("website", "missing", "no website on one side" if not rec.website or not ent.websites else "different"))
    id_match = id_match or bool(phone_hit or email_hit or dom_hit or web_hit)

    # ---- place ------------------------------------------------------------------
    city_ev = _cmp_set(rec.city, ent.cities, "city")
    state_ev = _cmp_set(rec.state, ent.states, "state")
    dist_ev = _cmp_set(rec.district, ent.districts, "district")
    if city_ev.status == "match":
        score += 5
    if state_ev.status == "match":
        score += 2
    if dist_ev.status == "match":
        score += 2
    ev += [city_ev, state_ev, dist_ev]
    pin_ev = _cmp_set(rec.pincode, ent.pincodes, "pincode")
    if pin_ev.status == "conflict":
        same_zone = any(p[:3] == rec.pincode[:3] for p in ent.pincodes)
        pin_ev = Evidence("pincode", "partial" if same_zone else "conflict", pin_ev.detail)
        score -= 2 if same_zone else 6
    elif pin_ev.status == "match":
        score += 6
    ev.append(pin_ev)

    addr_sim = 0.0
    if rec.address_norm and ent.addr_norm:
        addr_sim = max(fuzz.token_set_ratio(rec.address_norm, a) for a in ent.addr_norm)
        if addr_sim >= 85:
            ev.append(Evidence("address", "match", f"{addr_sim:.0f}% similar"))
            score += 8
        elif addr_sim >= 70:
            ev.append(Evidence("address", "match", f"{addr_sim:.0f}% similar"))
            score += 5
        elif addr_sim >= 55:
            ev.append(Evidence("address", "partial", f"{addr_sim:.0f}% similar"))
            score += 2
        else:
            ev.append(Evidence("address", "conflict", f"only {addr_sim:.0f}% similar"))
            score -= 5
    else:
        ev.append(Evidence("address", "missing", "no address on one side"))

    corr_keys = {e.key for e in ev if e.status == "match" and e.key in ("registration_id", "phone", "email", "website", "address", "city", "pincode")}
    corr = len(corr_keys)

    # ---- rules, strongest first ---------------------------------------------------
    forced = False
    capped_by = ""
    strong_conflict = False
    review_cap = max(cfg.thresholds["review"], cfg.auto_match_min_score - 0.1)

    if best.method == "fuzzy" and best.score < MIN_NAME_SCORE and not id_match:
        score, capped_by = min(score, 45.0), "names are too different"
    if best.method == "fuzzy" and best.sig_score < 50:
        score, capped_by, strong_conflict = min(score, 45.0), "distinctive words differ", True
    if best.distinct:
        score, capped_by, strong_conflict = min(score, 45.0), "names differ in a distinguishing word or number", True
    if reg_conflict:
        score, capped_by, strong_conflict = min(score, 45.0), "different registration identifiers", True
    if city_ev.status == "conflict":
        if id_match:
            forced, capped_by = True, "identifier matches but the city differs"
        else:
            score, capped_by, strong_conflict = min(score, 45.0), "different city", True
    if state_ev.status == "conflict":
        if id_match:
            forced, capped_by = True, "identifier matches but the state differs"
        else:
            score, capped_by, strong_conflict = min(score, 35.0), "different state", True

    if not strong_conflict:
        if best.method == "exact":
            one_sided = (city_ev.status == "missing") and bool(rec.city or ent.cities)
            if corr == 0 and not one_sided:
                score = max(score, cfg.exact_name_no_evidence_score)      # identical name, nothing contradicts it
                ev[0] = Evidence("name", "match", "identical normalised name, no other evidence")
            elif corr == 0 and one_sided:
                forced, capped_by = True, "identical name, but only one record states a location"
        elif best.method == "exact_core":
            if corr == 0:
                forced, capped_by = True, "same name apart from a place/title; nothing else confirms it"
        elif best.method in ("acronym", "variant"):
            if corr < 2:
                forced, capped_by = True, ("initialism" if best.method == "acronym" else "ambiguous abbreviation") + " match needs at least two confirming facts"
        elif corr == 0:
            forced, capped_by = True, "names are similar but nothing else confirms they are the same"
        if forced:
            score = min(score, review_cap)

    score = max(0.0, min(100.0, score))
    matched_labels = [k for k in ("registration_id", "phone", "email", "website", "address", "city", "pincode") if k in corr_keys]
    label = {"registration_id": "Registration ID", "phone": "Phone", "email": "Email", "website": "Website", "address": "Address",
             "city": "City", "pincode": "PIN code"}
    name_label = {"exact": "Exact name", "exact_core": "Same name", "acronym": "Initialism", "variant": "Name (abbreviation)"}.get(best.method, "Name")
    if "registration_id" in matched_labels:
        method = " + ".join(["Registration ID", name_label] + [label[k] for k in matched_labels if k != "registration_id"])
    else:
        method = " + ".join([name_label] + [label[k] for k in matched_labels])
    if not matched_labels and best.method == "exact":
        method = "Exact name only"

    reason_parts = [f"name {best.method.replace('_', ' ')} {best.score:.0f}%"]
    for e in ev[1:]:
        if e.status == "match":
            reason_parts.append(f"same {e.key.replace('_', ' ')}")
        elif e.status == "conflict":
            reason_parts.append(f"different {e.key.replace('_', ' ')}")
    if corr == 0:
        reason_parts.append("no corroborating evidence")
    if capped_by:
        reason_parts.append(capped_by)
    return PairResult(
        entity_id=ent.id, score=score, name_score=best.score, name_method=best.method, evidence=ev, corroborations=corr,
        forced_review=forced, reason="; ".join(reason_parts), method=method, strong_conflict=strong_conflict,
        capped_by=capped_by, best_alias=best_alias,
    )
