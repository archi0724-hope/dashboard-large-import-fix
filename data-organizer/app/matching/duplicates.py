"""Duplicate detection and removal of exact duplicate records.

* record level  ``exact_name_phone``         same normalised name AND same phone
                ``exact_name_registration``  same normalised name AND same registration id
                ``identical_record``         every compared field identical (repeated rows / repeated files)
* entity level  ``near_duplicate_entity``    two *different* master entities with similar names and similar
                                              addresses in the same place - candidates for a manual merge

Every group carries a suggested "primary" (the record / entity with the strongest evidence). Exact
duplicate records are marked inactive after detection; source files and audit groups are retained.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
from rapidfuzz import fuzz

from ..database.repository import Repository

_COLS = ["group_id", "dup_type", "dup_key", "record_id", "master_entity_id", "similarity", "is_suggested_primary", "detected_at"]


def _record_groups(repo: Repository, dup_type: str, key_sql: str, key_filter: str, prefix: str, start: int) -> tuple[list[dict], int]:
    rows = repo.dicts(f"""
        WITH k AS (
          SELECT {key_sql} AS dup_key, c.record_id, r.seq, c.evidence_strength, m.master_entity_id
          FROM cleaned_records c JOIN raw_records r USING (record_id)
          LEFT JOIN resolutions m ON m.record_id = c.record_id
          WHERE r.is_current AND c.is_resolvable AND {key_filter})
        SELECT * FROM k WHERE dup_key IN (SELECT dup_key FROM k GROUP BY dup_key HAVING count(*) > 1)
        ORDER BY dup_key, evidence_strength DESC, seq""")
    out: list[dict] = []
    now = datetime.now().replace(microsecond=0)
    gid, current, first = start, None, True
    for r in rows:
        if r["dup_key"] != current:
            gid, current, first = gid + 1, r["dup_key"], True
        out.append({"group_id": f"{prefix}-{gid:06d}", "dup_type": dup_type, "dup_key": r["dup_key"], "record_id": r["record_id"],
                    "master_entity_id": r["master_entity_id"], "similarity": 100.0, "is_suggested_primary": first, "detected_at": now})
        first = False
    return out, gid


def detect_duplicates(repo: Repository, near_min: float = 92.0, max_block: int = 150) -> dict[str, int]:
    repo.execute("DELETE FROM duplicate_groups")
    rows: list[dict] = []
    n = 0
    g, n = _record_groups(repo, "exact_name_phone", "c.name_normalized || ' | ' || c.phone_norm", "c.phone_norm <> ''", "DP", n)
    rows += g
    g, n = _record_groups(repo, "exact_name_registration", "c.name_normalized || ' | ' || c.registration_id_norm",
                          "c.registration_id_norm <> ''", "DR", n)
    rows += g
    g, n = _record_groups(
        repo, "identical_record",
        "c.name_normalized || '|' || c.city_norm || '|' || c.state_norm || '|' || c.address_norm || '|' || c.phone_norm || '|' || "
        "c.email_norm || '|' || c.registration_id_norm", "c.name_normalized <> ''", "DI", n)
    rows += g

    # ---- near-duplicate entities: same place, similar name, similar address -----------------------------
    ents = repo.dicts("SELECT master_entity_id, standard_name, city, address, evidence_strength FROM master_entities "
                      "WHERE merged_into IS NULL")
    blocks: dict[tuple[str, str], list[dict]] = {}
    for e in ents:
        toks = (e["standard_name"] or "").lower().split()
        if not toks:
            continue
        blocks.setdefault(((e["city"] or "").lower(), toks[0][:4]), []).append(e)
    now = datetime.now().replace(microsecond=0)
    seen: set[frozenset[str]] = set()
    ng = 0
    for (_, _), block in blocks.items():
        if len(block) < 2 or len(block) > max_block:
            continue
        for i, a in enumerate(block):
            for b in block[i + 1:]:
                nm = fuzz.token_sort_ratio((a["standard_name"] or "").lower(), (b["standard_name"] or "").lower())
                if nm < near_min:
                    continue
                if a["address"] and b["address"] and fuzz.token_set_ratio(a["address"].lower(), b["address"].lower()) < 60:
                    continue
                pair = frozenset((a["master_entity_id"], b["master_entity_id"]))
                if pair in seen:
                    continue
                seen.add(pair)
                ng += 1
                first, second = (a, b) if (a["evidence_strength"] or 0) >= (b["evidence_strength"] or 0) else (b, a)
                for ent, primary in ((first, True), (second, False)):
                    rows.append({"group_id": f"DN-{ng:06d}", "dup_type": "near_duplicate_entity",
                                 "dup_key": f'{a["standard_name"]} ~ {b["standard_name"]}', "record_id": None,
                                 "master_entity_id": ent["master_entity_id"], "similarity": float(nm),
                                 "is_suggested_primary": primary, "detected_at": now})
    if rows:
        repo.insert_df("duplicate_groups", pd.DataFrame(rows, columns=_COLS))
        exact_duplicate_ids = [r["record_id"] for r in rows
                               if r["dup_type"] == "identical_record" and not r["is_suggested_primary"]]
        if exact_duplicate_ids:
            repo.conn.executemany("UPDATE raw_records SET is_current = FALSE WHERE record_id = ?",
                                  [(record_id,) for record_id in exact_duplicate_ids])
    return {t: len({r["group_id"] for r in rows if r["dup_type"] == t})
            for t in ("exact_name_phone", "exact_name_registration", "identical_record", "near_duplicate_entity")}
