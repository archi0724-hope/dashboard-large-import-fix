"""Thread-safe DuckDB access.

A single DuckDB connection is shared behind an ``RLock``: DuckDB parallelises inside a query, and
the dashboard/API threads only ever run short statements, so serialising statements is simple and
safe. Bulk inserts go through pandas DataFrames (much faster than row-by-row inserts).
"""
from __future__ import annotations

import json
import threading
import traceback as tb
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import duckdb
import pandas as pd

from ..logging_setup import get_logger, log_error
from .models import DERIVED_TABLES, SCHEMA_SQL

_MASTER_COLS = (
    "master_entity_id", "standard_name", "name_normalized", "aliases", "city", "district", "state", "address", "pincode",
    "phone", "email", "website", "registration_id", "all_phones", "all_emails", "all_websites", "entity_type", "category",
    "confidence", "verification_status", "record_count", "source_file_count", "evidence_strength", "data_conflicts",
    "state_json", "merged_into", "created_at", "updated_at",
)
_ALIAS_COLS = ("alias", "alias_normalized", "master_entity_id", "original_source", "match_score", "match_method",
               "occurrences", "verified")
_RESOLUTION_COLS = ("record_id", "master_entity_id", "match_score", "match_band", "match_method", "match_status", "reason",
                    "evidence", "review_id", "verified_by_user", "resolved_at")
_REVIEW_COLS = ("review_id", "alias_normalized", "original_name", "context_city", "provisional_master_id",
                "candidate_master_id", "match_score", "match_band", "reason", "evidence", "ai_verdict", "record_count",
                "sample_record_id", "source_file", "source_row", "source_page", "status", "created_at")


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


class Repository:
    def __init__(self, db_path: str | Path = ":memory:"):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(self.db_path)
        self._lock = threading.RLock()
        self._log = get_logger("application")
        self.init_schema()

    # ---- lifecycle ---------------------------------------------------------
    def init_schema(self) -> None:
        with self._lock:
            for stmt in [s.strip() for s in SCHEMA_SQL.split(";") if s.strip()]:
                self.conn.execute(stmt)

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except Exception:  # pragma: no cover
                pass

    # ---- basic execution ---------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
        with self._lock:
            self.conn.execute(sql, list(params) if params else [])

    def fetchall(self, sql: str, params: Sequence[Any] | None = None) -> list[tuple]:
        with self._lock:
            return self.conn.execute(sql, list(params) if params else []).fetchall()

    def fetchone(self, sql: str, params: Sequence[Any] | None = None) -> tuple | None:
        with self._lock:
            return self.conn.execute(sql, list(params) if params else []).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] | None = None, default: Any = 0) -> Any:
        row = self.fetchone(sql, params)
        return default if not row or row[0] is None else row[0]

    def dicts(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        with self._lock:
            cur = self.conn.execute(sql, list(params) if params else [])
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def fetchdf(self, sql: str, params: Sequence[Any] | None = None) -> pd.DataFrame:
        with self._lock:
            return self.conn.execute(sql, list(params) if params else []).fetchdf()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            self.conn.execute("BEGIN TRANSACTION")
            try:
                yield
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    # ---- bulk insert -------------------------------------------------------
    def insert_rows(self, table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        df = pd.DataFrame(rows, columns=list(columns))
        return self.insert_df(table, df)

    def insert_df(self, table: str, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        cols = ", ".join(f'"{c}"' for c in df.columns)
        with self._lock:
            self.conn.register("_ins_df", df)
            try:
                self.conn.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM _ins_df")
            finally:
                self.conn.unregister("_ins_df")
        return len(df)

    # ---- key/value state ---------------------------------------------------
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.fetchone("SELECT value FROM kv_state WHERE key = ?", [key])
        if not row:
            return default
        try:
            return json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            return row[0]

    def kv_set(self, key: str, value: Any) -> None:
        payload = json.dumps(value, default=str)
        self.execute(
            "INSERT INTO kv_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            [key, payload, _now()],
        )

    # ---- errors ------------------------------------------------------------
    def add_error(
        self,
        file_name: str,
        message: str,
        *,
        file_key: str = "",
        location: str = "",
        stage: str = "",
        severity: str = "error",
        error_type: str = "",
        exc: BaseException | None = None,
    ) -> None:
        """Persist an error row AND write it to logs/errors.log; never raises."""
        try:
            trace = "".join(tb.format_exception(type(exc), exc, exc.__traceback__))[-4000:] if exc else ""
            self.execute(
                "INSERT INTO processing_errors (file_key, file_name, location, stage, severity, error_type, message, "
                "traceback, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [file_key, file_name, location, stage, severity, error_type or (type(exc).__name__ if exc else ""),
                 str(message)[:2000], trace, _now()],
            )
            if severity == "error":
                log_error(file_name, location, message, stage)
            else:
                get_logger("extraction").warning(f"file={file_name!r} location={location or '-'} {message}")
        except Exception as inner:  # pragma: no cover
            self._log.error(f"could not record error for {file_name}: {inner}")

    # ---- entity resolution persistence ---------------------------------------
    def save_entities(self, entity_rows: list[dict], alias_rows: list[dict], entity_ids: list[str]) -> None:
        if not entity_ids:
            return
        with self._lock:
            ph = ", ".join("?" for _ in entity_ids)
            self.conn.execute(f"DELETE FROM aliases WHERE master_entity_id IN ({ph})", entity_ids)
            df = pd.DataFrame(entity_rows, columns=list(_MASTER_COLS))
            cols = ", ".join(_MASTER_COLS)
            self.conn.register("_ent_df", df)
            try:
                self.conn.execute(f"INSERT OR REPLACE INTO master_entities ({cols}) SELECT {cols} FROM _ent_df")
            finally:
                self.conn.unregister("_ent_df")
            if alias_rows:
                self.insert_df("aliases", pd.DataFrame(alias_rows, columns=list(_ALIAS_COLS)))

    def insert_resolutions(self, rows: list[dict]) -> int:
        return self.insert_df("resolutions", pd.DataFrame(rows, columns=list(_RESOLUTION_COLS))) if rows else 0

    def insert_review_items(self, rows: list[dict]) -> int:
        return self.insert_df("review_items", pd.DataFrame(rows, columns=list(_REVIEW_COLS))) if rows else 0

    def next_review_id(self) -> int:
        return int(self.scalar("SELECT nextval('review_seq')"))

    def add_review_counts(self, deltas: dict[int, int]) -> None:
        for rid, n in deltas.items():
            self.execute("UPDATE review_items SET record_count = record_count + ? WHERE review_id = ?", [n, rid])

    def load_user_decisions(self) -> list[dict]:
        return self.dicts("SELECT alias_normalized, context_city, master_entity_id, decision FROM user_decisions")

    def add_user_decision(self, alias_norm: str, city: str, master_id: str, decision: str, review_id: int | None) -> None:
        self.execute("DELETE FROM user_decisions WHERE alias_normalized = ? AND context_city = ? AND master_entity_id = ? "
                     "AND decision = ?", [alias_norm, city or "", master_id, decision])
        self.execute("INSERT INTO user_decisions (alias_normalized, context_city, master_entity_id, decision, verified_by_user, "
                     "review_id, decided_at) VALUES (?, ?, ?, ?, TRUE, ?, ?)",
                     [alias_norm, city or "", master_id, decision, review_id, _now()])

    def fetch_unresolved(self, after_seq: int, limit: int) -> list[dict]:
        """Cleaned + raw columns needed for matching, for records that have no resolution yet (keyset paged)."""
        return self.dicts(
            "SELECT r.seq, r.record_id, r.original_name, r.original_address, r.original_category, r.source_file, "
            "r.source_sheet, r.source_page, r.source_row, c.* EXCLUDE (record_id) "
            "FROM raw_records r JOIN cleaned_records c USING (record_id) "
            "LEFT JOIN resolutions m ON m.record_id = r.record_id "
            "WHERE r.is_current AND m.record_id IS NULL AND r.seq > ? ORDER BY r.seq LIMIT ?",
            [after_seq, limit],
        )

    # ---- files / raw / cleaned -------------------------------------------------
    def get_source_file(self, file_key: str) -> dict | None:
        rows = self.dicts("SELECT * FROM source_files WHERE file_key = ?", [file_key])
        return rows[0] if rows else None

    def upsert_source_file(self, **f: Any) -> None:
        cols = ["file_key", "source_kind", "file_name", "source_path", "mime_type", "ext", "size_bytes", "modified_time",
                "fingerprint", "local_path", "status", "records_extracted", "sheets", "pages", "notes", "error",
                "inspection", "discovered_at", "processed_at"]
        cur = self.get_source_file(f["file_key"]) or {}
        merged = {c: f.get(c, cur.get(c)) for c in cols}
        if merged["discovered_at"] is None:
            merged["discovered_at"] = _now()
        with self._lock:
            self.conn.execute("DELETE FROM source_files WHERE file_key = ?", [f["file_key"]])
            self.conn.execute(f"INSERT INTO source_files ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                              [merged[c] for c in cols])

    def insert_raw(self, records: list) -> int:
        if not records:
            return 0
        df = pd.DataFrame([r.as_row() for r in records], columns=list(records[0].COLUMNS))
        df["is_current"] = True
        df["extracted_at"] = _now()
        return self.insert_df("raw_records", df)

    def fetch_uncleaned(self, after_seq: int, limit: int) -> list[dict]:
        return self.dicts(
            "SELECT r.*, f.modified_time AS _modified FROM raw_records r LEFT JOIN source_files f USING (file_key) "
            "LEFT JOIN cleaned_records c ON c.record_id = r.record_id "
            "WHERE r.is_current AND c.record_id IS NULL AND r.seq > ? ORDER BY r.seq LIMIT ?", [after_seq, limit])

    def insert_cleaned(self, rows: list[dict], issues: list[dict]) -> None:
        if rows:
            self.insert_df("cleaned_records", pd.DataFrame(rows))
        if issues:
            self.insert_df("quality_issues", pd.DataFrame(issues))

    # ---- housekeeping ------------------------------------------------------
    def reset_derived(self) -> None:
        """Drop derived analysis tables' rows (cleaned/resolved/master/...). Raw records, source files
        and the user's saved decisions are kept."""
        with self.transaction():
            for t in DERIVED_TABLES:
                self.conn.execute(f"DELETE FROM {t}")

    def table_counts(self) -> dict[str, int]:
        out = {}
        for (name,) in self.fetchall("SELECT table_name FROM information_schema.tables WHERE table_schema='main'"):
            out[name] = int(self.scalar(f'SELECT count(*) FROM "{name}"'))
        return out

