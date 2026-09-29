"""Entity resolution: assigns every cleaned record to a master entity - or sends it to a human.

Order of evidence (cheapest / most certain first):

    1. the user's verified mappings          (exact, learned)
    2. identical earlier record              (exact, cached)
    3. unique identifiers / phone / e-mail / website / exact name   (blocking keys)
    4. fuzzy name + address / place evidence (RapidFuzz, TF-IDF n-gram neighbours)
An ambiguous record is never force-merged: it gets its own *provisional* entity and a review item; the reviewer's
decision (accept / reject / new entity) is then applied to every record that shares that alias.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..cleaning.name_cleaner import NameCleaner
from ..config import RuntimeConfig, Settings
from ..database.repository import Repository
from ..logging_setup import get_logger
from .confidence import PairResult, RecordFeatures, band_for, score_pair
from .entities import EntityState
from .exact_match import VerifiedMappings
from .fuzzy_match import EntityIndex, TfidfBlocker

log = get_logger("matching")

STATUS_AUTO = "auto_matched"
STATUS_NEW = "new_entity"
STATUS_REVIEW = "needs_review"
STATUS_VERIFIED = "verified_mapping"
STATUS_SEPARATE = "uncertain_kept_separate"
STATUS_UNRESOLVABLE = "unresolvable"


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


@dataclass
class ResolveStats:
    records: int = 0
    auto_matched: int = 0
    new_entities: int = 0
    needs_review: int = 0
    verified_mapping: int = 0
    separate: int = 0
    unresolvable: int = 0
    cached: int = 0


class EntityResolver:
    def __init__(
        self, repo: Repository, cfg: RuntimeConfig, settings: Settings, cleaner: NameCleaner,
        use_tfidf: bool = True,
    ):
        self.repo, self.cfg, self.settings, self.cleaner = repo, cfg, settings, cleaner
        self.index = EntityIndex()
        self.tfidf = TfidfBlocker()
        self.use_tfidf = use_tfidf
        self.mappings = VerifiedMappings()
        self.merged_into: dict[str, str] = {}
        self.pending: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._sig_cache: dict[tuple, tuple[str, str]] = {}
        self._next_no = 1
        self._entities_at_fit = -1

    # ------------------------------------------------------------------ setup
    def load(self) -> None:
        self.index = EntityIndex()
        self.merged_into.clear()
        self.pending.clear()
        self._sig_cache.clear()
        top = 0
        for eid, payload, created, merged in self.repo.fetchall(
            "SELECT master_entity_id, state_json, created_at, merged_into FROM master_entities"
        ):
            try:
                top = max(top, int(eid.rsplit("-", 1)[-1]))
            except ValueError:
                pass
            if merged:
                self.merged_into[eid] = merged
                continue
            ent = EntityState.from_json(eid, payload, created)
            self.index.add_entity(ent, (self.cleaner.clean(i.alias) for i in ent.aliases.values()))
        self._next_no = top + 1
        self.mappings = VerifiedMappings(self.repo.load_user_decisions())
        for rid, alias, city, cand, prov in self.repo.fetchall(
            "SELECT review_id, alias_normalized, context_city, candidate_master_id, provisional_master_id "
            "FROM review_items WHERE status = 'pending'"
        ):
            self.pending[(alias, city or "", cand)] = {"review_id": rid, "provisional": prov}
        self._entities_at_fit = -1
        log.info(f"resolver loaded: {len(self.index)} entities, {len(self.mappings.maps)} verified mappings, "
                 f"{len(self.pending)} pending reviews")

    # -------------------------------------------------------------- tf-idf
    def _maybe_refit(self) -> None:
        n = len(self.index)
        if not self.use_tfidf or n < 30:
            return
        if self._entities_at_fit >= 0 and n - self._entities_at_fit < max(50, 0.2 * self._entities_at_fit):
            return
        ids, texts = [], []
        for ent in self.index.entities.values():
            for info in list(ent.aliases.values())[:6]:
                ids.append(ent.id)
                texts.append(self.cleaner.clean(info.alias).core)
        self.tfidf.fit(ids, texts)
        self._entities_at_fit = n

    # ------------------------------------------------------------------ batch
    def resolve_batch(self, rows: list[dict[str, Any]]) -> ResolveStats:
        stats = ResolveStats()
        self._maybe_refit()
        feats: list[RecordFeatures | None] = []
        for r in rows:
            if not r.get("is_resolvable"):
                feats.append(None)
                continue
            forms = self.cleaner.clean(r["original_name"])
            feats.append(RecordFeatures.from_row(r, forms))
        tf_hits = self.tfidf.query([f.forms.core if f else "" for f in feats]) if self.tfidf.ready else [[] for _ in feats]

        res_rows: list[dict] = []
        new_reviews: list[dict] = []
        bumps: Counter = Counter()
        dirty: set[str] = set()
        for row, rec, extra in zip(rows, feats, tf_hits):
            stats.records += 1
            if rec is None:
                stats.unresolvable += 1
                res_rows.append(self._res_row(row["record_id"], None, None, "", STATUS_UNRESOLVABLE,
                                              "No usable entity name in this record", [], None))
                continue
            d = self._decide(rec, extra, stats, new_reviews, bumps, dirty)
            res_rows.append(d)

        t = self.cfg.thresholds
        for r in res_rows:                       # fill the band for matched records
            if not r["match_band"] and r["match_score"] is not None:
                r["match_band"] = band_for(r["match_score"], t)
        entity_rows, alias_rows, ids = [], [], []
        for eid in dirty:
            ent = self.index.entities.get(eid)
            if ent is None:
                continue
            ent.choose_standard_name(self.cleaner)
            entity_rows.append(ent.to_master_row())
            alias_rows += ent.alias_rows()
            ids.append(eid)
            ent.dirty = False
        with self.repo.transaction():
            self.repo.save_entities(entity_rows, alias_rows, ids)
            self.repo.insert_review_items(new_reviews)
            self.repo.add_review_counts(bumps)
            self.repo.insert_resolutions(res_rows)
        return stats

    # --------------------------------------------------------------- decisions
    def _decide(self, rec: RecordFeatures, tf_ids: list[str], stats: ResolveStats, new_reviews: list[dict],
                bumps: Counter, dirty: set[str]) -> dict:
        alias, city = rec.alias_norm, rec.city
        # 1. verified user mapping -----------------------------------------------------------------
        mid = self.mappings.lookup(alias, city)
        if mid:
            mid = self._canonical(mid)
            ent = self.index.entities.get(mid)
            if ent:
                self._attach(ent, rec, 100.0, "Verified user mapping", dirty, verified=True)
                stats.verified_mapping += 1
                return self._res_row(rec.record_id, ent.id, 100.0, "Verified user mapping", STATUS_VERIFIED,
                                     "Alias previously confirmed by a user", [], None, verified=True)
        # 2. identical earlier record ----------------------------------------------------------------
        sig = (alias, rec.city, rec.state, rec.district, rec.pincode, rec.address_norm, tuple(sorted(rec.phones)),
               tuple(sorted(rec.emails)), rec.website, tuple(sorted(rec.regs.items())))
        cached = self._sig_cache.get(sig)
        if cached and cached[0] in self.index.entities:
            ent = self.index.entities[cached[0]]
            score = 100.0 if rec.strength else self.cfg.exact_name_no_evidence_score
            self._attach(ent, rec, score, "Identical to an earlier record", dirty)
            stats.cached += 1
            stats.auto_matched += 1
            return self._res_row(rec.record_id, ent.id, score, "Identical to an earlier record", STATUS_AUTO,
                                 "Same name and details as a record already matched", [], None)
        # 3-4. candidates & scoring ---------------------------------------------------------------------
        cids = list(dict.fromkeys(self.index.candidates(rec) + [i for i in tf_ids if i in self.index.entities]))
        pairs: list[PairResult] = []
        for cid in cids:
            if self.mappings.is_rejected(alias, city, cid):
                continue
            pairs.append(score_pair(rec, self.index.entities[cid], self.cfg, self.cleaner))
        pairs.sort(key=lambda p: -p.score)
        top = pairs[0] if pairs else None

        t = self.cfg.thresholds
        if top is None or top.score < t["review"]:
            ent = self._create(rec, "auto", dirty)
            reason = "No similar entity found" if top is None else \
                f'Closest existing entity "{top.best_alias}" scored {top.score:.0f}% ({top.capped_by or "names differ"})'
            self._sig_cache[sig] = (ent.id, STATUS_NEW)
            stats.new_entities += 1
            return self._res_row(rec.record_id, ent.id, top.score if top else None, "New entity", STATUS_NEW, reason, [], None,
                                 band="New entity")

        ambiguous = False
        why = top.reason
        if not top.forced_review and top.score >= self.cfg.auto_match_min_score:
            rivals = [p for p in pairs[1:] if p.score >= self.cfg.auto_match_min_score and
                      top.score - p.score <= self.cfg.ambiguity_margin]
            if rivals:
                ambiguous = True
                why = f"Several close candidates: {self._nm(top.entity_id)} ({top.score:.0f}%), " + \
                      ", ".join(f"{self._nm(p.entity_id)} ({p.score:.0f}%)" for p in rivals[:2])
        auto_ok = (self.cfg.automatic_matching and not top.forced_review and not ambiguous
                   and top.score >= self.cfg.auto_match_min_score)
        if auto_ok:
            ent = self.index.entities[top.entity_id]
            self._attach(ent, rec, top.score, top.method, dirty)
            self._sig_cache[sig] = (ent.id, STATUS_AUTO)
            stats.auto_matched += 1
            return self._res_row(rec.record_id, ent.id, top.score, top.method, STATUS_AUTO, top.reason,
                                 top.evidence_dicts(), None)
        if not self.cfg.automatic_matching and top.score >= self.cfg.auto_match_min_score:
            why = "Automatic matching is switched off - " + why

        # ---- uncertain -----------------------------------------------------------------------------
        band = band_for(top.score, t)
        if not self.cfg.manual_review_required:
            ent = self._create(rec, "auto", dirty)
            stats.separate += 1
            return self._res_row(rec.record_id, ent.id, top.score, top.method, STATUS_SEPARATE,
                                 f"Uncertain match to {self._nm(top.entity_id)} kept separate (manual review is off): {why}",
                                 top.evidence_dicts(), None, band=band)
        key = (alias, city, top.entity_id)
        pend = self.pending.get(key)
        if pend and pend["provisional"] in self.index.entities:
            ent = self.index.entities[pend["provisional"]]
            self._attach(ent, rec, top.score, top.method, dirty)
            bumps[pend["review_id"]] += 1
            review_id = pend["review_id"]
        else:
            ent = self._create(rec, "provisional", dirty)
            review_id = self.repo.next_review_id()
            self.pending[key] = {"review_id": review_id, "provisional": ent.id}
            others = [{"entity_id": p.entity_id, "name": self._nm(p.entity_id), "score": round(p.score, 1), "reason": p.reason}
                      for p in pairs[1:4] if p.score >= t["review"]]
            new_reviews.append({
                "review_id": review_id, "alias_normalized": alias, "original_name": rec.raw_name, "context_city": city,
                "provisional_master_id": ent.id, "candidate_master_id": top.entity_id, "match_score": round(top.score, 1),
                "match_band": band, "reason": why,
                "evidence": json.dumps({"checklist": top.evidence_dicts(), "alternatives": others,
                                        "candidate_alias": top.best_alias, "name_method": top.name_method,
                                        "capped_by": top.capped_by}, ensure_ascii=False),
                "ai_verdict": None,
                "record_count": 1, "sample_record_id": rec.record_id, "source_file": rec.source_file,
                "source_row": None, "source_page": None, "status": "pending", "created_at": _now(),
            })
        stats.needs_review += 1
        return self._res_row(rec.record_id, ent.id, top.score, top.method, STATUS_REVIEW, why, top.evidence_dicts(),
                             review_id, band=band)

    # ------------------------------------------------------------------ helpers
    def _canonical(self, eid: str) -> str:
        seen = 0
        while eid in self.merged_into and seen < 20:
            eid, seen = self.merged_into[eid], seen + 1
        return eid

    def _nm(self, eid: str) -> str:
        e = self.index.entities.get(eid)
        return (e.standard_name or (next(iter(e.aliases.values())).alias if e and e.aliases else eid)) if e else eid

    def _new_id(self) -> str:
        eid = f"ENT-{self._next_no:06d}"
        self._next_no += 1
        return eid

    def _create(self, rec: RecordFeatures, status: str, dirty: set[str]) -> EntityState:
        ent = EntityState(id=self._new_id(), verification_status=status)
        self._attach(ent, rec, 100.0, "New entity", dirty)
        ent.standard_name = ent._core_display(rec.forms) or rec.raw_name
        self.index.add_entity(ent, [rec.forms])
        return ent

    def _attach(self, ent: EntityState, rec: RecordFeatures, score: float, method: str, dirty: set[str],
                verified: bool = False) -> None:
        is_new_alias = rec.alias_norm not in ent.aliases
        ent.add_record(rec, score, method, rec.source_ref)
        if verified and rec.alias_norm in ent.aliases:
            ent.aliases[rec.alias_norm].verified = True
        if is_new_alias and ent.id in self.index.entities:
            self.index.index_forms(ent.id, rec.forms)
        if ent.id in self.index.entities:
            self.index.index_identifiers(ent)
        dirty.add(ent.id)

    @staticmethod
    def _res_row(record_id: str, master_id: str | None, score: float | None, method: str, status: str, reason: str,
                 evidence: list[dict], review_id: int | None, band: str = "", verified: bool = False) -> dict:
        return {
            "record_id": record_id, "master_entity_id": master_id,
            "match_score": None if score is None else round(float(score), 1), "match_band": band or ("Very High" if verified else ""),
            "match_method": method, "match_status": status, "reason": reason,
            "evidence": json.dumps(evidence, ensure_ascii=False), "review_id": review_id, "verified_by_user": verified,
            "resolved_at": _now(),
        }
