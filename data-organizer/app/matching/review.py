"""Manual review: apply a human decision and make the system remember it.

    accept      the provisional entity IS the suggested master entity -> merge, and remember alias -> master
    reject      it is NOT that entity                                -> stays separate, and never suggest that pair again
    new_entity  it is a genuinely new entity (optionally with a chosen standard name)

A decision is applied to every record that shares the alias (they share one review item), stored in
``user_decisions`` and re-applied automatically to future files. Every decision is written to the audit log.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..cleaning.name_cleaner import NameCleaner
from ..database.repository import Repository
from ..logging_setup import audit
from .entities import EntityState


class ReviewError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


class ReviewService:
    def __init__(self, repo: Repository, cleaner: NameCleaner):
        self.repo, self.cleaner = repo, cleaner

    # ---------------------------------------------------------------- reading
    def list_items(self, status: str = "pending", limit: int = 50, offset: int = 0, search: str = "", min_score: float | None = None,
                   max_score: float | None = None) -> dict[str, Any]:
        where, params = ["status = ?"], [status]
        if search:
            where.append("(lower(original_name) LIKE ? OR lower(alias_normalized) LIKE ? OR lower(context_city) LIKE ?)")
            params += [f"%{search.lower()}%"] * 3
        if min_score is not None:
            where.append("match_score >= ?")
            params.append(min_score)
        if max_score is not None:
            where.append("match_score <= ?")
            params.append(max_score)
        w = " AND ".join(where)
        total = int(self.repo.scalar(f"SELECT count(*) FROM review_items WHERE {w}", params))
        rows = self.repo.dicts(f"SELECT * FROM review_items WHERE {w} ORDER BY record_count DESC, match_score DESC, review_id LIMIT ? OFFSET ?",
                               params + [limit, offset])
        return {"total": total, "items": [self._enrich(r) for r in rows]}

    def get(self, review_id: int) -> dict[str, Any]:
        rows = self.repo.dicts("SELECT * FROM review_items WHERE review_id = ?", [review_id])
        if not rows:
            raise ReviewError(f"Review item {review_id} not found")
        return self._enrich(rows[0], detail=True)

    def _entity_summary(self, eid: str | None) -> dict | None:
        if not eid:
            return None
        rows = self.repo.dicts("SELECT master_entity_id, standard_name, city, district, state, address, phone, registration_id, "
                               "aliases, record_count, verification_status FROM master_entities WHERE master_entity_id = ?", [eid])
        if not rows:
            return None
        r = rows[0]
        try:
            r["aliases"] = json.loads(r["aliases"] or "[]")
        except ValueError:
            r["aliases"] = []
        return r

    def _enrich(self, r: dict, detail: bool = False) -> dict:
        try:
            ev = json.loads(r.get("evidence") or "{}")
        except ValueError:
            ev = {}
        r["evidence"] = ev
        try:
            r["ai_verdict"] = json.loads(r["ai_verdict"]) if r.get("ai_verdict") else None
        except ValueError:
            r["ai_verdict"] = None
        r["candidate"] = self._entity_summary(r.get("candidate_master_id"))
        r["provisional"] = self._entity_summary(r.get("provisional_master_id"))
        sample = self.repo.dicts(
            "SELECT record_id, source_file, source_path, source_sheet, source_page, source_row, original_name, original_address, "
            "original_city, original_state, original_phone, original_email, original_registration_id, original_data "
            "FROM raw_records WHERE record_id = ?", [r.get("sample_record_id")])
        r["sample"] = sample[0] if sample else None
        if detail:
            for alt in ev.get("alternatives", []):
                alt["entity"] = self._entity_summary(alt.get("entity_id"))
            r["records"] = self.repo.dicts(
                "SELECT r.record_id, r.source_file, r.source_sheet, r.source_page, r.source_row, r.original_name "
                "FROM resolutions m JOIN raw_records r USING (record_id) WHERE m.review_id = ? ORDER BY r.seq LIMIT 50", [r["review_id"]])
        return r

    # --------------------------------------------------------------- entities
    def _load(self, eid: str) -> EntityState:
        rows = self.repo.fetchall("SELECT state_json, created_at FROM master_entities WHERE master_entity_id = ?", [eid])
        if not rows:
            raise ReviewError(f"Entity {eid} not found")
        return EntityState.from_json(eid, rows[0][0], rows[0][1])

    def _save(self, *states: EntityState) -> None:
        rows, aliases, ids = [], [], []
        for s in states:
            if s.verification_status != "merged":
                s.choose_standard_name(self.cleaner)
            rows.append(s.to_master_row())
            aliases += s.alias_rows() if s.verification_status != "merged" else []
            ids.append(s.id)
        self.repo.save_entities(rows, aliases, ids)

    def _canonical(self, eid: str) -> str:
        for _ in range(20):
            row = self.repo.fetchone("SELECT merged_into FROM master_entities WHERE master_entity_id = ?", [eid])
            if not row or not row[0]:
                return eid
            eid = row[0]
        return eid

    # -------------------------------------------------------------- decisions
    def decide(self, review_id: int, action: str, target_id: str | None = None, standard_name: str | None = None,
               scope: str = "city", user: str = "dashboard") -> dict[str, Any]:
        item = self.repo.dicts("SELECT * FROM review_items WHERE review_id = ?", [review_id])
        if not item:
            raise ReviewError(f"Review item {review_id} not found")
        item = item[0]
        if item["status"] != "pending":
            raise ReviewError(f"Review item {review_id} was already decided ({item['status']})")
        if action not in ("accept", "reject", "new_entity"):
            raise ReviewError("action must be accept, reject or new_entity")
        done = [self._apply(item, action, target_id, standard_name, scope, user)]
        if action == "accept" and scope == "alias":       # same alias in other cities / contexts, same suggestion
            target = self._canonical(target_id or item["candidate_master_id"])
            for other in self.repo.dicts("SELECT * FROM review_items WHERE status = 'pending' AND alias_normalized = ? AND review_id <> ? "
                                         "AND candidate_master_id = ?", [item["alias_normalized"], review_id, item["candidate_master_id"]]):
                done.append(self._apply(other, "accept", target, None, "alias", user))
        return {"decided": done}

    def _apply(self, item: dict, action: str, target_id: str | None, standard_name: str | None, scope: str, user: str) -> dict:
        rid, prov_id = item["review_id"], item["provisional_master_id"]
        alias, city = item["alias_normalized"], item["context_city"] or ""
        decision_city = "" if scope == "alias" else city
        with self.repo.transaction():
            if action == "accept":
                target_id = self._canonical(target_id or item["candidate_master_id"])
                if target_id == prov_id:
                    raise ReviewError("Cannot merge an entity into itself")
                prov, tgt = self._load(prov_id), self._load(target_id)
                if alias in prov.aliases:
                    prov.aliases[alias].verified = True
                tgt.absorb(prov)
                if tgt.verification_status == "provisional":
                    tgt.verification_status = "auto"
                prov.verification_status, prov.merged_into, prov.record_count = "merged", target_id, 0
                self._save(tgt, prov)
                self._repoint(prov_id, target_id)
                self.repo.execute(
                    "UPDATE resolutions SET match_status='user_accepted', match_method='User accepted', verified_by_user=TRUE, "
                    "match_band='Very High' WHERE review_id = ?", [rid])
                self.repo.add_user_decision(alias, decision_city, target_id, "map", rid)
                status = "accepted"
            else:
                prov = self._load(prov_id)
                if action == "new_entity":
                    prov.verification_status = "verified"
                    if standard_name and standard_name.strip():
                        prov.standard_name, prov.name_locked = standard_name.strip(), True
                    self.repo.add_user_decision(alias, decision_city, prov_id, "map", rid)
                    status, rstatus, method = "new_entity", "user_created", "User created new entity"
                else:
                    prov.verification_status = "auto"
                    status, rstatus, method = "rejected", "new_entity", "User rejected the suggested match"
                self._save(prov)
                self.repo.add_user_decision(alias, decision_city, item["candidate_master_id"], "reject", rid)
                self.repo.execute(
                    "UPDATE resolutions SET match_status=?, match_method=?, verified_by_user=TRUE, match_band='New entity', "
                    "reason='Confirmed by a reviewer as a separate entity' WHERE review_id = ?", [rstatus, method, rid])
            self.repo.execute("UPDATE review_items SET status=?, decided_at=?, decided_by=? WHERE review_id = ?", [status, _now(), user, rid])
        audit("review_decision", review_id=rid, action=action, alias=alias, target=target_id or prov_id, scope=scope, user=user)
        return {"review_id": rid, "action": action, "status": status, "master_entity_id": target_id if action == "accept" else prov_id}

    def _repoint(self, old: str, new: str) -> None:
        self.repo.execute("UPDATE resolutions SET master_entity_id = ? WHERE master_entity_id = ?", [new, old])
        self.repo.execute("UPDATE user_decisions SET master_entity_id = ? WHERE master_entity_id = ?", [new, old])
        self.repo.execute("UPDATE review_items SET candidate_master_id = ? WHERE candidate_master_id = ? AND status = 'pending'", [new, old])
        self.repo.execute("UPDATE duplicate_groups SET master_entity_id = ? WHERE master_entity_id = ?", [new, old])

    # ------------------------------------------------------ direct entity edits
    def merge_entities(self, source_id: str, target_id: str, user: str = "dashboard") -> dict:
        source_id, target_id = self._canonical(source_id), self._canonical(target_id)
        if source_id == target_id:
            raise ReviewError("Choose two different entities")
        with self.repo.transaction():
            src, tgt = self._load(source_id), self._load(target_id)
            for a in src.aliases.values():
                a.verified = True
            tgt.absorb(src)
            if tgt.verification_status == "provisional":
                tgt.verification_status = "auto"
            src.verification_status, src.merged_into, src.record_count = "merged", target_id, 0
            self._save(tgt, src)
            self._repoint(source_id, target_id)
            for alias in src.aliases:
                self.repo.add_user_decision(alias, "", target_id, "map", None)
        audit("entity_merge", source=source_id, target=target_id, user=user)
        return {"merged": source_id, "into": target_id}

    def rename_entity(self, entity_id: str, standard_name: str, user: str = "dashboard") -> dict:
        name = (standard_name or "").strip()
        if not name:
            raise ReviewError("Standard name cannot be empty")
        ent = self._load(self._canonical(entity_id))
        ent.standard_name, ent.name_locked = name, True
        if ent.verification_status in ("auto", "provisional"):
            ent.verification_status = "verified"
        self._save(ent)
        audit("entity_rename", entity=ent.id, name=name, user=user)
        return {"master_entity_id": ent.id, "standard_name": name}
