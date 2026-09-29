"""Name similarity, candidate blocking and (optional) TF-IDF retrieval.

Blocking is what keeps entity resolution from being O(n^2): a record is only ever compared with the few entities
that share a cheap key (identifier, exact name, distinctive token, token prefix, acronym) or that are its nearest
neighbours in a character n-gram TF-IDF space. Everything else is never scored.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

import numpy as np
from rapidfuzz import fuzz

from ..cleaning.name_cleaner import NameForms
from .entities import EntityState


# ---------------------------------------------------------------------------
# Name similarity
# ---------------------------------------------------------------------------
@dataclass
class NameSim:
    score: float
    method: str            # exact | exact_core | fuzzy | acronym | variant | none
    sig_score: float = 100.0
    notes: tuple[str, ...] = ()
    distinct: bool = False     # both names carry a DIFFERENT distinguishing word/number (east vs west, unit 2 vs unit 3)


def _has_digit(t: str) -> bool:
    return any(c.isdigit() for c in t)


def name_similarity(a: NameForms, b: NameForms, distinguishing: frozenset[str] = frozenset()) -> NameSim:
    """Similarity of two names on a 0-100 scale. Only an identical normalised name scores 100."""
    if not a.core or not b.core:
        return NameSim(0.0, "none")
    if a.normalized == b.normalized:
        return NameSim(100.0, "exact")
    if a.core == b.core:
        return NameSim(98.0, "exact_core")

    # initialism: "SMS Hospital" <-> "Sawai Man Singh Hospital"
    if (a.acronym_key and len(b.sig_tokens) == 1 and b.sig_tokens[0] == a.acronym_key) or (
        b.acronym_key and len(a.sig_tokens) == 1 and a.sig_tokens[0] == b.acronym_key
    ):
        return NameSim(82.0, "acronym", notes=("initialism match",))

    # only-through-ambiguous-abbreviation ("St Mary" ~ "Saint Mary")
    if (a.variants and b.core in a.variants) or (b.variants and a.core in b.variants) or (
        a.variants and b.variants and set(a.variants) & set(b.variants)
    ):
        return NameSim(90.0, "variant", notes=("matches only if an ambiguous abbreviation is expanded",))

    core = max(fuzz.token_sort_ratio(a.core, b.core), fuzz.ratio(a.compact, b.compact))
    if a.sig_tokens and b.sig_tokens:
        sa, sb = " ".join(a.sig_tokens), " ".join(b.sig_tokens)
        sig = max(fuzz.token_sort_ratio(sa, sb), fuzz.ratio(sa.replace(" ", ""), sb.replace(" ", "")))
    else:
        sig = core
    score = 0.6 * core + 0.4 * sig
    notes: list[str] = []

    # words like east/west/new/branch present on one side only
    da = {t for t in a.tokens if t in distinguishing}
    db = {t for t in b.tokens if t in distinguishing}
    distinct = False
    if da != db:
        score -= min(20, 10 * len(da ^ db))
        notes.append("differs in distinguishing word(s): " + ", ".join(sorted(da ^ db)))
        distinct = bool(da - db) and bool(db - da)
    na = {t for t in a.tokens if _has_digit(t)}
    nb = {t for t in b.tokens if _has_digit(t)}
    if na != nb:
        score -= 15
        notes.append("different numbers in the name")
        distinct = distinct or (bool(na) and bool(nb))
    # short distinctive parts (ABC vs ABD) are not typos, they are different names
    if a.sig_tokens and b.sig_tokens and a.sig_tokens != b.sig_tokens:
        if max(len(t) for t in a.sig_tokens + b.sig_tokens) <= 4:
            score = min(score, 70.0)
            notes.append("short distinctive part differs")
    return NameSim(max(0.0, min(99.0, score)), "fuzzy", sig, tuple(notes), distinct)


# ---------------------------------------------------------------------------
# Blocking index
# ---------------------------------------------------------------------------
_W = {"reg": 12, "phone": 6, "email": 6, "dom": 3, "norm": 10, "core": 9, "cmp": 8, "tok": 2, "acr": 4, "p4": 1, "pfx": 2}


def name_keys(f: NameForms) -> list[str]:
    keys: list[str] = []
    if f.normalized:
        keys.append("norm:" + f.normalized)
    if f.core:
        keys.append("core:" + f.core)
        keys.append("cmp:" + f.compact)
        if len(f.compact) >= 5:
            keys.append("pfx:" + f.compact[:5])
    for t in f.sig_tokens:
        if len(t) >= 3:
            keys.append("tok:" + t)
        if len(t) >= 5:
            keys.append("p4:" + t[:4])
    if f.acronym_key:
        keys.append("acr:" + f.acronym_key)
    return keys


class EntityIndex:
    def __init__(self, max_block: int = 300, max_candidates: int = 40):
        self.entities: dict[str, EntityState] = {}
        self._idx: dict[str, set[str]] = defaultdict(set)
        self.max_block = max_block
        self.max_candidates = max_candidates

    def __len__(self) -> int:
        return len(self.entities)

    # -- building -------------------------------------------------------------
    def add_entity(self, ent: EntityState, alias_forms: Iterable[NameForms]) -> None:
        self.entities[ent.id] = ent
        for f in alias_forms:
            self.index_forms(ent.id, f)
        self.index_identifiers(ent)

    def index_forms(self, eid: str, f: NameForms) -> None:
        for k in name_keys(f):
            self._idx[k].add(eid)

    def index_identifiers(self, ent: EntityState) -> None:
        for t, vals in ent.regs.items():
            for v in vals:
                self._idx[f"reg:{t}:{v}"].add(ent.id)
        for p in ent.phones:
            self._idx[f"phone:{p}"].add(ent.id)
        for e in ent.emails:
            self._idx[f"email:{e}"].add(ent.id)
        for d in ent.domains:
            self._idx[f"dom:{d}"].add(ent.id)

    # -- lookup ---------------------------------------------------------------
    def candidates(self, rec) -> list[str]:
        f: NameForms = rec.forms
        keys = name_keys(f)
        # acronym cross-links
        if f.acronym_key:
            keys.append("tok:" + f.acronym_key)
        if len(f.sig_tokens) == 1 and 2 <= len(f.sig_tokens[0]) <= 6:
            keys.append("acr:" + f.sig_tokens[0])
        keys += [f"reg:{t}:{v}" for t, v in rec.regs.items()]
        keys += [f"phone:{p}" for p in rec.phones]
        keys += [f"email:{e}" for e in rec.emails]
        keys += [f"dom:{d}" for d in rec.domains]
        scores: dict[str, float] = defaultdict(float)
        for k in keys:
            bucket = self._idx.get(k)
            if not bucket:
                continue
            kind = k.split(":", 1)[0]
            if len(bucket) > self.max_block and kind in ("tok", "p4", "pfx", "dom", "phone", "email"):
                continue                       # a block this large carries no information
            w = _W.get(kind, 1)
            for eid in bucket:
                scores[eid] += w
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        return [eid for eid, _ in ranked[: self.max_candidates] if eid in self.entities]


# ---------------------------------------------------------------------------
# TF-IDF nearest neighbours (scikit-learn) - catches typos in the first letters / word-order changes
# ---------------------------------------------------------------------------
class TfidfBlocker:
    def __init__(self, k: int = 5, min_sim: float = 0.5, chunk: int = 512):
        self.k, self.min_sim, self.chunk = k, min_sim, chunk
        self._vec = None
        self._matrix = None
        self._ids: list[str] = []
        self.size = 0

    def fit(self, ids: list[str], texts: list[str]) -> None:
        from sklearn.feature_extraction.text import TfidfVectorizer

        self.size = len(set(ids))
        if len(texts) < 2:
            self._vec = self._matrix = None
            return
        self._vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 4), sublinear_tf=True, dtype=np.float32)
        self._matrix = self._vec.fit_transform(texts).T.tocsr()
        self._ids = ids

    @property
    def ready(self) -> bool:
        return self._vec is not None

    def query(self, texts: list[str]) -> list[list[str]]:
        if not self.ready or not texts:
            return [[] for _ in texts]
        out: list[list[str]] = []
        for start in range(0, len(texts), self.chunk):
            q = self._vec.transform(texts[start:start + self.chunk])
            sims = (q @ self._matrix).tocsr()
            for i in range(sims.shape[0]):
                lo, hi = sims.indptr[i], sims.indptr[i + 1]
                if hi == lo:
                    out.append([])
                    continue
                data, idx = sims.data[lo:hi], sims.indices[lo:hi]
                order = np.argsort(-data)[: self.k * 3]
                seen: list[str] = []
                for j in order:
                    if data[j] < self.min_sim:
                        break
                    eid = self._ids[idx[j]]
                    if eid not in seen:
                        seen.append(eid)
                    if len(seen) >= self.k:
                        break
                out.append(seen)
        return out
