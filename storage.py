"""Local SQLite/files plus optional PostgreSQL persistent cloud storage.

V2 tables coexist with the original V1 tables. Migration is non-destructive and
idempotent. Cloud bytes are stored in PostgreSQL, not Streamlit's temporary disk.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from io import BytesIO
import hashlib
import json
import os
import re
import sqlite3
import zipfile
import shutil

import pandas as pd
from vendor_core import (Decision, classify, clean_company, company_key, file_basename, natural_key,
                          export_filename, normalize_document_types, supporting_category)

DOC_COLUMNS = ["id", "company_key", "company_name", "types_json", "filename", "original_path", "stored_path", "file_hash", "uploaded_at", "source_batch", "handover", "method", "reason", "reviewed", "size_bytes"]
PRODUCT_COLUMNS = ["id", "document_id", "company_key", "company_name", "record_type", "product_name", "sku", "description", "unit", "price", "mrp", "discount", "gst", "currency", "brand", "product_category", "specification", "pack_size", "moq", "availability", "source_file", "source_sheet", "source_page", "source_row", "image_reference", "extraction_confidence", "record_hash", "extracted_at"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Query:
    def __init__(self, connection, cloud: bool):
        self.connection, self.cloud = connection, cloud

    def execute(self, sql: str, params=()):
        return self.connection.execute(sql.replace("?", "%s") if self.cloud else sql, params)


class Store:
    def __init__(self, data_dir: Path, database_url: str = ""):
        self.data_dir = Path(data_dir).resolve()
        self.files_dir = self.data_dir / "files"
        self.uploads_dir = self.data_dir / "uploaded_zips"
        self.db_path = self.data_dir / "vendor_documents.db"
        self.database_url = database_url.strip()
        self.cloud = bool(self.database_url)
        if self.cloud and not self.database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL must be a PostgreSQL URL.")
        if not self.cloud:
            self.files_dir.mkdir(parents=True, exist_ok=True)
            self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connection(self):
        if self.cloud:
            import psycopg
            from psycopg.rows import dict_row
            connection = psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=15, sslmode="require")
        else:
            connection = sqlite3.connect(self.db_path, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield Query(connection, self.cloud)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        ident = "BIGSERIAL PRIMARY KEY" if self.cloud else "INTEGER PRIMARY KEY AUTOINCREMENT"
        blob = "BYTEA" if self.cloud else "BLOB"
        with self.connection() as db:
            if self.cloud:
                db.execute("SELECT pg_advisory_xact_lock(739182641)")
            db.execute("CREATE TABLE IF NOT EXISTS vdd_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS vdd_vendors (
                company_key TEXT PRIMARY KEY, canonical_id TEXT UNIQUE, company_name TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'auto')""")
            db.execute("""CREATE TABLE IF NOT EXISTS vdd_company_aliases (
                alias_key TEXT PRIMARY KEY, canonical_key TEXT NOT NULL, alias_name TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '', confidence INTEGER NOT NULL DEFAULT 100,
                status TEXT NOT NULL DEFAULT 'Confirmed', created_at TEXT NOT NULL)""")
            db.execute(f"""CREATE TABLE IF NOT EXISTS vdd_review_queue (
                id {ident}, detected_name TEXT NOT NULL DEFAULT '', possible_company TEXT NOT NULL DEFAULT '',
                canonical_id TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', record TEXT NOT NULL DEFAULT '',
                confidence INTEGER NOT NULL DEFAULT 0, evidence TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'Review Required',
                created_at TEXT NOT NULL, decision TEXT NOT NULL DEFAULT '')""")
            db.execute(f"""CREATE TABLE IF NOT EXISTS vdd_documents (
                id {ident}, company_key TEXT NOT NULL DEFAULT '', company_name TEXT NOT NULL DEFAULT '',
                types_json TEXT NOT NULL DEFAULT '[]', filename TEXT NOT NULL, original_path TEXT NOT NULL,
                stored_path TEXT NOT NULL DEFAULT '', file_hash TEXT NOT NULL, uploaded_at TEXT NOT NULL,
                source_batch TEXT NOT NULL DEFAULT '', handover TEXT NOT NULL DEFAULT '',
                method TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', reviewed INTEGER NOT NULL DEFAULT 0,
                size_bytes BIGINT NOT NULL DEFAULT 0, payload {blob}, UNIQUE(company_key, file_hash))""")
            db.execute("CREATE INDEX IF NOT EXISTS vdd_docs_company ON vdd_documents(company_key)")
            db.execute(f"""CREATE TABLE IF NOT EXISTS vdd_product_records (
                id {ident}, document_id BIGINT NOT NULL, company_key TEXT NOT NULL DEFAULT '',
                company_name TEXT NOT NULL DEFAULT '', record_type TEXT NOT NULL,
                product_name TEXT NOT NULL, sku TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
                unit TEXT NOT NULL DEFAULT '', price DOUBLE PRECISION, mrp DOUBLE PRECISION, discount DOUBLE PRECISION,
                gst DOUBLE PRECISION, currency TEXT NOT NULL DEFAULT '', brand TEXT NOT NULL DEFAULT '',
                product_category TEXT NOT NULL DEFAULT '', specification TEXT NOT NULL DEFAULT '',
                pack_size TEXT NOT NULL DEFAULT '', moq TEXT NOT NULL DEFAULT '',
                availability TEXT NOT NULL DEFAULT '', source_sheet TEXT NOT NULL DEFAULT '',
                source_file TEXT NOT NULL DEFAULT '',
                source_page TEXT NOT NULL DEFAULT '', source_row BIGINT NOT NULL DEFAULT 0,
                image_reference TEXT NOT NULL DEFAULT '', extraction_confidence TEXT NOT NULL DEFAULT 'high',
                record_hash TEXT NOT NULL, extracted_at TEXT NOT NULL,
                UNIQUE(document_id, record_hash))""")
            self._ensure_product_columns(db)
            db.execute("CREATE INDEX IF NOT EXISTS vdd_products_search ON vdd_product_records(product_name, sku)")
            db.execute(f"""CREATE TABLE IF NOT EXISTS vdd_upload_archives (
                id TEXT PRIMARY KEY, file_hash TEXT UNIQUE NOT NULL, filename TEXT NOT NULL,
                first_uploaded_at TEXT NOT NULL, last_uploaded_at TEXT NOT NULL, upload_count INTEGER NOT NULL DEFAULT 1,
                size_bytes BIGINT NOT NULL DEFAULT 0, detected_companies INTEGER NOT NULL DEFAULT 0,
                company_names_json TEXT NOT NULL DEFAULT '[]', processed_files INTEGER NOT NULL DEFAULT 0,
                issues_count INTEGER NOT NULL DEFAULT 0, stored_path TEXT NOT NULL DEFAULT '', payload {blob})""")
            db.execute(f"CREATE TABLE IF NOT EXISTS vdd_backups (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload {blob} NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS vdd_audit (event_at TEXT NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL)")
            self._ensure_canonical_ids(db)
            for row in db.execute("SELECT company_key,company_name FROM vdd_vendors").fetchall():
                self._record_alias(db, row["company_key"], row["company_name"], "Canonical company master")
        if not self.cloud:
            self.migrate_original()
        self.backfill_product_records()

    def _ensure_canonical_ids(self, db: Query):
        if self.cloud:
            columns = {row["column_name"] for row in db.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name='vdd_vendors'").fetchall()}
        else:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(vdd_vendors)").fetchall()}
        if "canonical_id" not in columns:
            db.execute("ALTER TABLE vdd_vendors ADD COLUMN canonical_id TEXT")
        rows = db.execute("SELECT company_key FROM vdd_vendors WHERE canonical_id IS NULL OR canonical_id='' ORDER BY created_at,company_key").fetchall()
        used = {str(row["canonical_id"]) for row in db.execute("SELECT canonical_id FROM vdd_vendors WHERE canonical_id IS NOT NULL").fetchall()}
        next_id = max([int(value[5:]) for value in used if value.startswith("COMP-") and value[5:].isdigit()] or [0]) + 1
        for row in rows:
            while f"COMP-{next_id:03d}" in used:
                next_id += 1
            canonical_id = f"COMP-{next_id:03d}"
            db.execute("UPDATE vdd_vendors SET canonical_id=? WHERE company_key=?", (canonical_id, row["company_key"]))
            used.add(canonical_id)
            next_id += 1

    def _ensure_product_columns(self, db: Query):
        expected = {
            "mrp": "DOUBLE PRECISION", "discount": "DOUBLE PRECISION", "gst": "DOUBLE PRECISION",
            "brand": "TEXT NOT NULL DEFAULT ''", "product_category": "TEXT NOT NULL DEFAULT ''",
            "specification": "TEXT NOT NULL DEFAULT ''", "pack_size": "TEXT NOT NULL DEFAULT ''",
            "moq": "TEXT NOT NULL DEFAULT ''", "availability": "TEXT NOT NULL DEFAULT ''",
            "source_file": "TEXT NOT NULL DEFAULT ''",
            "source_page": "TEXT NOT NULL DEFAULT ''", "source_row": "BIGINT NOT NULL DEFAULT 0",
            "image_reference": "TEXT NOT NULL DEFAULT ''", "extraction_confidence": "TEXT NOT NULL DEFAULT 'high'",
        }
        if self.cloud:
            columns = {row["column_name"] for row in db.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name='vdd_product_records'").fetchall()}
        else:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(vdd_product_records)").fetchall()}
        for column, definition in expected.items():
            if column not in columns:
                db.execute(f"ALTER TABLE vdd_product_records ADD COLUMN {column} {definition}")

    def _record_alias(self, db: Query, canonical_key: str, alias_name: str, source: str, confidence: int = 100, status: str = "Confirmed"):
        alias_key = company_key(alias_name)
        if not alias_key:
            return
        db.execute("""INSERT INTO vdd_company_aliases(alias_key,canonical_key,alias_name,source,confidence,status,created_at)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(alias_key) DO UPDATE SET canonical_key=excluded.canonical_key,
            alias_name=excluded.alias_name,source=excluded.source,confidence=excluded.confidence,status=excluded.status""",
            (alias_key, canonical_key, clean_company(alias_name), source, int(confidence), status, now()))

    def aliases(self) -> pd.DataFrame:
        with self.connection() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM vdd_company_aliases ORDER BY lower(alias_name)").fetchall()]
        return pd.DataFrame(rows, columns=["alias_key", "canonical_key", "alias_name", "source", "confidence", "status", "created_at"])

    def add_review_item(self, detected_name: str, possible_company: str = "", source: str = "", record: str = "", confidence: int = 0, evidence: str = ""):
        with self.connection() as db:
            canonical_id = ""
            if possible_company:
                row = db.execute("SELECT canonical_id FROM vdd_vendors WHERE company_key=?", (company_key(possible_company),)).fetchone()
                canonical_id = row["canonical_id"] if row else ""
            db.execute("""INSERT INTO vdd_review_queue(detected_name,possible_company,canonical_id,source,record,confidence,evidence,created_at)
                VALUES(?,?,?,?,?,?,?,?)""", (clean_company(detected_name), clean_company(possible_company), canonical_id,
                source, record, int(confidence), evidence, now()))

    def review_queue(self) -> pd.DataFrame:
        with self.connection() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM vdd_review_queue ORDER BY created_at DESC").fetchall()]
        return pd.DataFrame(rows, columns=["id", "detected_name", "possible_company", "canonical_id", "source", "record", "confidence", "evidence", "status", "created_at", "decision"])

    def matching_companies(self) -> dict[str, str]:
        vendors = self.vendors()
        mapping = {row.company_name: row.company_name for row in vendors.itertuples(index=False)}
        aliases = self.aliases()
        if not aliases.empty:
            canonical = dict(zip(vendors.company_key, vendors.company_name))
            mapping.update({row.alias_name: canonical.get(row.canonical_key, row.alias_name) for row in aliases.itertuples(index=False)})
        return mapping

    def _upsert_vendor(self, db: Query, name: str, source: str = "auto") -> str:
        name = clean_company(name)
        key = company_key(name)
        if not key:
            raise ValueError("Company name cannot be empty.")
        timestamp = now()
        existing = db.execute("SELECT canonical_id FROM vdd_vendors WHERE company_key=?", (key,)).fetchone()
        canonical_id = existing["canonical_id"] if existing and existing["canonical_id"] else None
        if not canonical_id:
            used = [str(row["canonical_id"]) for row in db.execute("SELECT canonical_id FROM vdd_vendors WHERE canonical_id IS NOT NULL").fetchall()]
            next_id = max([int(value[5:]) for value in used if value.startswith("COMP-") and value[5:].isdigit()] or [0]) + 1
            canonical_id = f"COMP-{next_id:03d}"
        db.execute("""INSERT INTO vdd_vendors(company_key,canonical_id,company_name,created_at,updated_at,source)
            VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(company_key) DO UPDATE SET updated_at=excluded.updated_at""",
            (key, canonical_id, name, timestamp, timestamp, source))
        self._record_alias(db, key, name, source)
        return key

    def upsert_vendors(self, frame: pd.DataFrame) -> int:
        with self.connection() as db:
            for name in frame.company_name:
                self._upsert_vendor(db, str(name), "master")
        return len(frame)

    def vendors(self) -> pd.DataFrame:
        with self.connection() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM vdd_vendors ORDER BY lower(company_name)").fetchall()]
        return pd.DataFrame(rows, columns=["company_key", "canonical_id", "company_name", "created_at", "updated_at", "source"])

    def merge_duplicate_companies(self) -> dict:
        """Merge existing vendor rows that normalize to the same company key."""
        with self.connection() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT company_key,company_name,created_at FROM vdd_vendors ORDER BY created_at,company_name"
            ).fetchall()]
            groups = {}
            for row in rows:
                groups.setdefault(company_key(row["company_name"]), []).append(row)

            merged_companies = 0
            merged_documents = 0
            for normalized_key, matches in groups.items():
                if not normalized_key or not matches:
                    continue
                canonical = matches[0]
                canonical_name = clean_company(canonical["company_name"])
                canonical_key = canonical["company_key"]
                for duplicate in matches[1:]:
                    old_key = duplicate["company_key"]
                    self._record_alias(db, canonical_key, duplicate["company_name"], "Company merge", 90, "Likely Match")
                    old_documents = db.execute(
                        "SELECT * FROM vdd_documents WHERE company_key=? ORDER BY id", (old_key,)
                    ).fetchall()
                    for document in old_documents:
                        existing = db.execute(
                            "SELECT id,types_json,reviewed FROM vdd_documents WHERE company_key=? AND file_hash=?",
                            (canonical_key, document["file_hash"]),
                        ).fetchone()
                        if existing:
                            types = normalize_document_types(json.loads(existing["types_json"]) + json.loads(document["types_json"]))
                            db.execute(
                                "UPDATE vdd_documents SET types_json=?,reviewed=? WHERE id=?",
                                (json.dumps(types), max(int(existing["reviewed"]), int(document["reviewed"])), existing["id"]),
                            )
                            db.execute("DELETE FROM vdd_documents WHERE id=?", (document["id"],))
                            db.execute("DELETE FROM vdd_product_records WHERE document_id=?", (document["id"],))
                            merged_documents += 1
                        else:
                            db.execute(
                                "UPDATE vdd_documents SET company_key=?,company_name=? WHERE id=?",
                                (canonical_key, canonical_name, document["id"]),
                            )
                            db.execute("UPDATE vdd_product_records SET company_key=?,company_name=? WHERE document_id=?",
                                       (canonical_key, canonical_name, document["id"]))
                    db.execute("DELETE FROM vdd_vendors WHERE company_key=?", (old_key,))
                    merged_companies += 1
                db.execute(
                    "UPDATE vdd_vendors SET company_name=?,updated_at=? WHERE company_key=?",
                    (canonical_name, now(), canonical_key),
                )
                db.execute(
                    "UPDATE vdd_documents SET company_name=? WHERE company_key=?",
                    (canonical_name, canonical_key),
                )
                db.execute("UPDATE vdd_product_records SET company_name=? WHERE company_key=?", (canonical_name, canonical_key))
            result = {"merged_companies": merged_companies, "merged_documents": merged_documents}
            if merged_companies or merged_documents:
                db.execute("INSERT INTO vdd_audit VALUES(?,?,?)", (now(), "Duplicate companies merged", json.dumps(result)))
            return result

    def merge_named_companies(self, canonical_name: str, aliases: list[str]) -> dict:
        """Merge explicitly confirmed aliases into one canonical company."""
        canonical_name = clean_company(canonical_name)
        canonical_key = company_key(canonical_name)
        alias_keys = {company_key(name) for name in aliases if company_key(name)}
        alias_keys.discard(canonical_key)
        with self.connection() as db:
            rows = [dict(row) for row in db.execute("SELECT company_key,company_name FROM vdd_vendors").fetchall()]
            matched = {row["company_key"]: row for row in rows if row["company_key"] == canonical_key or row["company_key"] in alias_keys}
            if canonical_key not in matched:
                raise ValueError(f"Canonical company not found: {canonical_name}")
            merged_companies = 0
            merged_documents = 0
            for old_key in sorted(set(matched) - {canonical_key}):
                self._record_alias(db, canonical_key, matched[old_key]["company_name"], "Named company merge", 90, "Likely Match")
                for document in db.execute("SELECT * FROM vdd_documents WHERE company_key=? ORDER BY id", (old_key,)).fetchall():
                    existing = db.execute(
                        "SELECT id,types_json,reviewed FROM vdd_documents WHERE company_key=? AND file_hash=?",
                        (canonical_key, document["file_hash"]),
                    ).fetchone()
                    if existing:
                        types = normalize_document_types(json.loads(existing["types_json"]) + json.loads(document["types_json"]))
                        db.execute("UPDATE vdd_documents SET types_json=?,reviewed=? WHERE id=?",
                                   (json.dumps(types), max(int(existing["reviewed"]), int(document["reviewed"])), existing["id"]))
                        db.execute("DELETE FROM vdd_documents WHERE id=?", (document["id"],))
                        db.execute("DELETE FROM vdd_product_records WHERE document_id=?", (document["id"],))
                        merged_documents += 1
                    else:
                        db.execute("UPDATE vdd_documents SET company_key=?,company_name=? WHERE id=?",
                                   (canonical_key, canonical_name, document["id"]))
                        db.execute("UPDATE vdd_product_records SET company_key=?,company_name=? WHERE document_id=?",
                                   (canonical_key, canonical_name, document["id"]))
                db.execute("DELETE FROM vdd_vendors WHERE company_key=?", (old_key,))
                merged_companies += 1
            db.execute("UPDATE vdd_vendors SET company_name=?,updated_at=? WHERE company_key=?",
                       (canonical_name, now(), canonical_key))
            db.execute("UPDATE vdd_documents SET company_name=? WHERE company_key=?", (canonical_name, canonical_key))
            result = {"canonical_name": canonical_name, "merged_companies": merged_companies, "merged_documents": merged_documents}
            db.execute("INSERT INTO vdd_audit VALUES(?,?,?)", (now(), "Named companies merged", json.dumps(result)))
            return result

    def safe_path(self, path: str) -> Path | None:
        if not path:
            return None
        p = (self.data_dir / path.replace("\\", "/")).resolve()
        return p if p.is_relative_to(self.files_dir) else None

    def documents(self) -> pd.DataFrame:
        with self.connection() as db:
            cols = ", ".join(DOC_COLUMNS)
            rows = [dict(row) for row in db.execute(f"SELECT {cols}, CASE WHEN payload IS NOT NULL THEN 1 ELSE 0 END AS has_payload FROM vdd_documents ORDER BY lower(company_name), filename").fetchall()]
        frame = pd.DataFrame(rows, columns=DOC_COLUMNS + ["has_payload"])
        frame["types"] = frame.types_json.map(lambda value: normalize_document_types(json.loads(value)))
        frame["available"] = frame.has_payload.astype(bool) if self.cloud else frame.stored_path.map(lambda p: bool((path := self.safe_path(p)) and path.is_file() and path.stat().st_size > 0))
        frame["needs_review"] = frame.apply(lambda r: not r.company_key or (not r.types and not bool(r.reviewed) and r.method != "Supporting document"), axis=1) if len(frame) else pd.Series(dtype=bool)
        return frame

    def _upload_path(self, stored_path: str) -> Path | None:
        if not stored_path:
            return None
        path = (self.data_dir / stored_path.replace("\\", "/")).resolve()
        return path if path.is_relative_to(self.uploads_dir) else None

    def save_upload_archive(self, filename: str, content: bytes, company_names: list[str], processed_files: int, issues_count: int = 0) -> dict:
        """Store the original ZIP exactly once by content hash and retain upload history metadata."""
        if not content or not zipfile.is_zipfile(BytesIO(content)):
            raise ValueError("Uploaded archive is not a readable ZIP file.")
        digest = hashlib.sha256(content).hexdigest()
        archive_id = digest[:20]
        clean_name = file_basename(filename) or "Vendor_Upload.zip"
        timestamp = now()
        unique_names = sorted({company_key(n): clean_company(n) for n in company_names if company_key(n)}.values(), key=str.casefold)
        stored = ""
        if not self.cloud:
            destination = self.uploads_dir / f"{digest}.zip"
            if not destination.exists():
                import tempfile
                with tempfile.NamedTemporaryFile(dir=self.uploads_dir, delete=False) as temp:
                    temp.write(content)
                    temporary = temp.name
                os.replace(temporary, destination)
            stored = "uploaded_zips/" + destination.name
        with self.connection() as db:
            existing = db.execute("SELECT id,upload_count,stored_path FROM vdd_upload_archives WHERE file_hash=?", (digest,)).fetchone()
            if existing:
                if not self.cloud:
                    path = self._upload_path(existing["stored_path"])
                    if path is None or not path.exists():
                        destination = self.uploads_dir / f"{digest}.zip"
                        destination.write_bytes(content)
                        stored = "uploaded_zips/" + destination.name
                    else:
                        stored = existing["stored_path"]
                db.execute("""UPDATE vdd_upload_archives SET filename=?,last_uploaded_at=?,upload_count=?,size_bytes=?,
                    detected_companies=?,company_names_json=?,processed_files=?,issues_count=?,stored_path=?,payload=? WHERE file_hash=?""",
                    (clean_name, timestamp, int(existing["upload_count"])+1, len(content), len(unique_names), json.dumps(unique_names, ensure_ascii=False),
                     int(processed_files), int(issues_count), stored, content if self.cloud else None, digest))
                added = False
            else:
                db.execute("""INSERT INTO vdd_upload_archives(id,file_hash,filename,first_uploaded_at,last_uploaded_at,upload_count,size_bytes,
                    detected_companies,company_names_json,processed_files,issues_count,stored_path,payload)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (archive_id,digest,clean_name,timestamp,timestamp,1,len(content),len(unique_names),json.dumps(unique_names,ensure_ascii=False),
                     int(processed_files),int(issues_count),stored,content if self.cloud else None))
                added = True
        return {"id": archive_id, "added": added, "filename": clean_name, "companies": len(unique_names), "processed_files": int(processed_files)}

    def upload_archives(self) -> pd.DataFrame:
        columns = ["id","file_hash","filename","first_uploaded_at","last_uploaded_at","upload_count","size_bytes",
                   "detected_companies","company_names_json","processed_files","issues_count","stored_path"]
        with self.connection() as db:
            rows = [dict(r) for r in db.execute("""SELECT id,file_hash,filename,first_uploaded_at,last_uploaded_at,upload_count,size_bytes,
                detected_companies,company_names_json,processed_files,issues_count,stored_path FROM vdd_upload_archives
                ORDER BY last_uploaded_at DESC""").fetchall()]
        frame = pd.DataFrame(rows, columns=columns)
        if len(frame):
            frame["company_names"] = frame.company_names_json.map(json.loads)
            if self.cloud:
                frame["available"] = True
            else:
                frame["available"] = frame.stored_path.map(lambda value: bool((path := self._upload_path(value)) and path.is_file() and path.stat().st_size > 0))
        else:
            frame["company_names"] = pd.Series(dtype=object)
            frame["available"] = pd.Series(dtype=bool)
        return frame

    def read_upload_archive(self, archive_id: str) -> bytes | None:
        with self.connection() as db:
            row = db.execute("SELECT stored_path,payload FROM vdd_upload_archives WHERE id=?", (archive_id,)).fetchone()
        if row is None:
            return None
        if self.cloud:
            return bytes(row["payload"]) if row["payload"] is not None else None
        path = self._upload_path(row["stored_path"])
        return path.read_bytes() if path and path.is_file() else None

    def migrate_original(self):
        """Recover company ownership from original ZIP paths, preserve every file."""
        with self.connection() as db:
            if db.execute("SELECT value FROM vdd_meta WHERE key='legacy_migrated'").fetchone():
                return
            has_legacy = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='documents'").fetchone()
            if not has_legacy:
                db.execute("INSERT INTO vdd_meta(key,value) VALUES('legacy_migrated','no_legacy') ON CONFLICT(key) DO NOTHING")
                return
            old_docs = [dict(row) for row in db.execute("SELECT * FROM documents ORDER BY id").fetchall()]
            old_vendors = [dict(row) for row in db.execute("SELECT * FROM vendors").fetchall()]
        backup = self.data_dir / "backups" / "vendor_documents_before_update.db"
        if not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.db_path) as source, sqlite3.connect(backup) as target:
                source.backup(target)
        report = []
        occupied_old_keys = {row["company_key"] for row in old_docs}
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT value FROM vdd_meta WHERE key='legacy_migrated'").fetchone():
                return
            for row in old_vendors:
                if row["company_key"] not in occupied_old_keys:
                    self._upsert_vendor(db, row["company_name"], "legacy master")
            for row in old_docs:
                original = row["filename"].replace("\\", "/")
                decision = classify(original)
                name = decision.company_name or ""
                key = self._upsert_vendor(db, name) if name else ""
                basename = file_basename(row["stored_path"])
                candidate = self.files_dir / basename
                if not candidate.is_file():
                    candidates = list(self.files_dir.glob(row["file_hash"][:12] + "*"))
                    candidate = candidates[0] if len(candidates) == 1 else candidate
                exists = candidate.is_file()
                relative = "files/" + candidate.name if exists else ""
                reason = decision.reason if exists else "Stored bytes were missing from the original project. Re-upload this document."
                db.execute("""INSERT INTO vdd_documents(id,company_key,company_name,types_json,filename,original_path,
                    stored_path,file_hash,uploaded_at,source_batch,handover,method,reason,size_bytes)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(company_key,file_hash) DO NOTHING""",
                    (row["id"], key, name, json.dumps(decision.document_types), file_basename(original), original,
                     relative, row["file_hash"], row["uploaded_at"], "Original project import", decision.handover,
                     decision.method, reason, candidate.stat().st_size if exists else 0))
                report.append({"Document ID": row["id"], "Old company": row["company_name"], "Company Name": name,
                    "Old type": row["document_type"], "New types": ", ".join(decision.document_types) or "Needs review / Other",
                    "File": file_basename(original), "Source path": original, "File available": exists})
            db.execute("INSERT INTO vdd_meta(key,value) VALUES('legacy_migrated',?)", (now(),))
            db.execute("INSERT INTO vdd_audit VALUES(?,?,?)", (now(), "Legacy migration", json.dumps({"documents": len(old_docs), "backup": str(backup.name)})))
        (self.data_dir / "migration_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    def save_document(self, original_path: str, content: bytes, decision: Decision, source_batch: str = "") -> bool:
        if not content:
            raise ValueError("Empty files are not saved.")
        name = decision.company_name or ""
        key = company_key(name) if name else ""
        digest = hashlib.sha256(content).hexdigest()
        with self.connection() as db:
            existing = db.execute("SELECT id,stored_path,reviewed,types_json FROM vdd_documents WHERE company_key=? AND file_hash=?", (key, digest)).fetchone()
            if existing:
                if not bool(existing["reviewed"]):
                    types = normalize_document_types(json.loads(existing["types_json"]) + list(decision.document_types))
                    db.execute("UPDATE vdd_documents SET types_json=?,method=?,reason=? WHERE id=?",
                               (json.dumps(types), decision.method, decision.reason, existing["id"]))
                if not self.cloud:
                    previous = self.safe_path(existing["stored_path"])
                    if previous is None or not previous.exists():
                        path = self._write_file(digest, file_basename(original_path), content)
                        db.execute("UPDATE vdd_documents SET stored_path=?,size_bytes=? WHERE id=?", (path, len(content), existing["id"]))
                return False
            if name:
                self._upsert_vendor(db, name)
            stored = "" if self.cloud else self._write_file(digest, file_basename(original_path), content)
            cursor = db.execute("""INSERT INTO vdd_documents(company_key,company_name,types_json,filename,original_path,
                stored_path,file_hash,uploaded_at,source_batch,handover,method,reason,size_bytes,payload)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(company_key,file_hash) DO NOTHING""",
                (key, name, json.dumps(decision.document_types), file_basename(original_path), original_path, stored, digest,
                 now(), source_batch, decision.handover, decision.method, decision.reason, len(content), content if self.cloud else None))
            return cursor.rowcount > 0

    def document_id_for(self, original_path: str, content: bytes, company_name: str = "") -> int | None:
        key = company_key(company_name)
        digest = hashlib.sha256(content).hexdigest()
        with self.connection() as db:
            row = db.execute("SELECT id FROM vdd_documents WHERE company_key=? AND file_hash=?", (key, digest)).fetchone()
        return int(row["id"]) if row else None

    def save_product_records(self, document_id: int, records: list[dict]) -> int:
        if not records:
            return 0
        from catalogue_extraction import record_hash
        with self.connection() as db:
            document = db.execute("SELECT company_key,company_name FROM vdd_documents WHERE id=?", (int(document_id),)).fetchone()
            if not document:
                raise ValueError("Document not found for extracted records.")
            saved = 0
            for record in records:
                digest = record_hash(record)
                cursor = db.execute("""INSERT INTO vdd_product_records
                    (document_id,company_key,company_name,record_type,product_name,sku,description,unit,price,mrp,discount,gst,currency,
                     brand,product_category,specification,pack_size,moq,availability,source_file,source_sheet,source_page,source_row,image_reference,
                     extraction_confidence,record_hash,extracted_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(document_id,record_hash) DO NOTHING""",
                    (int(document_id), document["company_key"], document["company_name"], record.get("record_type", "catalogue"),
                     record.get("product_name", ""), record.get("sku", ""), record.get("description", ""),
                     record.get("unit", ""), record.get("price"), record.get("mrp"), record.get("discount"), record.get("gst"),
                     record.get("currency", ""), record.get("brand", ""), record.get("product_category", ""),
                     record.get("specification", ""), record.get("pack_size", ""), record.get("moq", ""),
                     record.get("availability", ""), record.get("source_file") or self._document_filename(db, document_id),
                     record.get("source_sheet", ""), record.get("source_page", ""),
                     int(record.get("source_row") or 0), record.get("image_reference", ""),
                     record.get("extraction_confidence", "high"), digest, now()))
                saved += int(cursor.rowcount > 0)
        return saved

    @staticmethod
    def _document_filename(db: Query, document_id: int) -> str:
        row = db.execute("SELECT filename FROM vdd_documents WHERE id=?", (int(document_id),)).fetchone()
        return str(row["filename"]) if row else ""

    def add_document_types(self, document_id: int, types: list[str]):
        if not types:
            return
        with self.connection() as db:
            row = db.execute("SELECT types_json FROM vdd_documents WHERE id=?", (int(document_id),)).fetchone()
            if row:
                merged = normalize_document_types(json.loads(row["types_json"]) + types)
                db.execute("UPDATE vdd_documents SET types_json=?,method='Content extraction',reason=? WHERE id=?",
                           (json.dumps(merged), "Catalogue/price-list classification from filename and extracted content.", int(document_id)))

    def product_records(self, query: str = "", record_type: str = "") -> pd.DataFrame:
        sql = """SELECT p.*, COALESCE(NULLIF(p.source_file, ''), d.filename) AS source_file
            FROM vdd_product_records p
            LEFT JOIN vdd_documents d ON d.id=p.document_id"""
        clauses, params = [], []
        if query.strip():
            clauses.append("(lower(product_name) LIKE ? OR lower(sku) LIKE ? OR lower(description) LIKE ? OR lower(company_name) LIKE ?)")
            term = f"%{query.casefold().strip()}%"
            params.extend([term, term, term, term])
        if record_type:
            clauses.append("record_type=?")
            params.append(record_type)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY lower(p.company_name), lower(p.product_name), p.id"
        with self.connection() as db:
            rows = [dict(row) for row in db.execute(sql, params).fetchall()]
        return pd.DataFrame(rows, columns=PRODUCT_COLUMNS)

    def backfill_product_records(self) -> int:
        """Extract records for documents imported before catalogue extraction existed."""
        from catalogue_extraction import extract_records
        with self.connection() as db:
            rows = db.execute("""SELECT d.id,d.filename,d.company_name,d.stored_path,d.payload
                FROM vdd_documents d
                WHERE NOT EXISTS (SELECT 1 FROM vdd_product_records p WHERE p.document_id=d.id)""").fetchall()
        extracted = 0
        for row in rows:
            payload = bytes(row["payload"]) if self.cloud and row["payload"] is not None else None
            if payload is None and not self.cloud:
                path = self.safe_path(row["stored_path"])
                if path and path.is_file():
                    payload = path.read_bytes()
            if not payload:
                continue
            kind, records = extract_records(row["filename"], payload)
            if kind:
                self.add_document_types(int(row["id"]), ["Price" if kind == "price" else "Catalogue"])
            extracted += self.save_product_records(int(row["id"]), records)
        return extracted

    def _write_file(self, digest: str, basename: str, content: bytes) -> str:
        # Content-addressed short filenames prevent Windows MAX_PATH extraction
        # failures when the dashboard is moved under a long Downloads path.
        suffix = Path(basename).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
            suffix = ".bin"
        destination = self.files_dir / f"{digest}{suffix}"
        if not destination.exists():
            import tempfile
            with tempfile.NamedTemporaryFile(dir=self.files_dir, delete=False) as temp:
                temp.write(content)
                temporary = temp.name
            os.replace(temporary, destination)
        return "files/" + destination.name

    def read_bytes(self, document_id: int) -> bytes | None:
        with self.connection() as db:
            row = db.execute("SELECT stored_path,payload FROM vdd_documents WHERE id=?", (int(document_id),)).fetchone()
        if not row:
            return None
        if self.cloud:
            return bytes(row["payload"]) if row["payload"] is not None else None
        path = self.safe_path(row["stored_path"])
        return path.read_bytes() if path and path.is_file() else None

    def correct_document(self, document_id: int, company: str, types: list[str], note: str = ""):
        from vendor_core import DOCUMENT_TYPES
        if any(t not in DOCUMENT_TYPES for t in types):
            raise ValueError("Unknown document type.")
        name = clean_company(company)
        if not name:
            raise ValueError("Enter/select the company name.")
        with self.connection() as db:
            old = db.execute("SELECT * FROM vdd_documents WHERE id=?", (int(document_id),)).fetchone()
            if old is None:
                raise ValueError("Document not found.")
            key = company_key(name)
            duplicate = db.execute("SELECT id FROM vdd_documents WHERE company_key=? AND file_hash=? AND id<>?", (key, old["file_hash"], int(document_id))).fetchone()
            if duplicate:
                raise ValueError("This identical file already belongs to that company. No records changed.")
            self._upsert_vendor(db, name)
            db.execute("UPDATE vdd_documents SET company_key=?,company_name=?,types_json=?,reviewed=1,method='Reviewed',reason=? WHERE id=?",
                       (key, name, json.dumps(types), note or "Manually reviewed classification; validity not verified.", int(document_id)))
            db.execute("UPDATE vdd_product_records SET company_key=?,company_name=? WHERE document_id=?", (key, name, int(document_id)))
            db.execute("INSERT INTO vdd_audit VALUES(?,?,?)", (now(), "Classification correction", json.dumps({"id": int(document_id), "old_company": old["company_name"], "company": name, "types": types})))

    def company_zip(self, company: str, docs: pd.DataFrame) -> bytes:
        """Enforce company isolation even if the caller passes the full register."""
        from exports import workbook_bytes, csv_bytes
        from vendor_core import build_checklist, DOCUMENT_TYPES
        docs = docs[docs.company_key == company_key(company)].copy()
        output = BytesIO()
        folder = export_filename(company, "Documents", "zip").removesuffix("_Documents.zip")
        vendors = pd.DataFrame({"company_key": [company_key(company)], "company_name": [company]})
        checklist = build_checklist(vendors, docs)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(f"{folder}/{export_filename(company, 'Checklist', 'xlsx')}", workbook_bytes(checklist, docs, True, self.aliases()))
            for doc in docs.sort_values("filename", key=lambda s: s.map(str.casefold)).itertuples(index=False):
                if doc.available and (payload := self.read_bytes(doc.id)) is not None:
                    category = doc.types[0] if doc.types else "Other documents"
                    ordinal = DOCUMENT_TYPES.index(category) + 1 if category in DOCUMENT_TYPES else len(DOCUMENT_TYPES)+1
                    basename = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", file_basename(doc.filename))
                    archive.writestr(f"{folder}/{ordinal:02d} - {category}/{doc.id}_{basename}", payload)
        return output.getvalue()

    def log_event(self, action: str, details: dict):
        with self.connection() as db:
            db.execute("INSERT INTO vdd_audit VALUES(?,?,?)", (now(), action, json.dumps(details)))

    def history(self) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("SELECT * FROM vdd_audit ORDER BY event_at DESC LIMIT 100").fetchall()
        return [{"time": r["event_at"], "action": r["action"], "details": json.loads(r["details"])} for r in rows]

    def _snapshot(self, db) -> bytes:
        vendors = [dict(r) for r in db.execute("SELECT * FROM vdd_vendors ORDER BY company_key").fetchall()]
        aliases = [dict(r) for r in db.execute("SELECT * FROM vdd_company_aliases ORDER BY alias_key").fetchall()]
        documents = [dict(r) for r in db.execute("SELECT * FROM vdd_documents ORDER BY id").fetchall()]
        uploads = [dict(r) for r in db.execute("SELECT * FROM vdd_upload_archives ORDER BY last_uploaded_at").fetchall()]
        manifest = {"format": "vendor-dashboard-backup-v3", "created_at": now(), "vendors": vendors, "aliases": aliases, "documents": [], "uploads": []}
        out = BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
            written = set()
            for row in documents:
                payload = row.pop("payload", None)
                if not self.cloud:
                    path = self.safe_path(row["stored_path"])
                    payload = path.read_bytes() if path and path.is_file() else None
                if payload is None:
                    raise ValueError("Some stored documents are unavailable. Re-upload them before resetting; no data was deleted.")
                payload = bytes(payload)
                if hashlib.sha256(payload).hexdigest() != row["file_hash"]:
                    raise ValueError("A document failed its integrity check; reset cancelled.")
                member = "files/" + row["file_hash"]
                if member not in written:
                    archive.writestr(member, payload); written.add(member)
                row["backup_member"] = member
                manifest["documents"].append(row)
            for row in uploads:
                payload = row.pop("payload", None)
                if not self.cloud:
                    path = self._upload_path(row["stored_path"])
                    payload = path.read_bytes() if path and path.is_file() else None
                if payload is None:
                    raise ValueError("An uploaded ZIP archive is unavailable. Re-upload it before resetting; no data was deleted.")
                payload = bytes(payload)
                if hashlib.sha256(payload).hexdigest() != row["file_hash"]:
                    raise ValueError("An uploaded ZIP failed its integrity check; reset cancelled.")
                member = "uploads/" + row["file_hash"] + ".zip"
                archive.writestr(member, payload)
                row["backup_member"] = member
                manifest["uploads"].append(row)
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        return out.getvalue()

    def backup_bytes(self) -> bytes:
        with self.connection() as db:
            if not self.cloud:
                db.execute("BEGIN")
            return self._snapshot(db)

    def reset_status(self) -> dict:
        """Return whether any user-created dashboard data still exists.

        The operational ``legacy_migrated`` marker and the live SQLite database file are
        intentionally excluded; everything else is considered user data that a full reset
        must remove.
        """
        with self.connection() as db:
            tables = set()
            if not self.cloud:
                tables = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            counts = {
                "companies": int(db.execute("SELECT COUNT(*) AS n FROM vdd_vendors").fetchone()["n"]),
                "documents": int(db.execute("SELECT COUNT(*) AS n FROM vdd_documents").fetchone()["n"]),
                "product_records": int(db.execute("SELECT COUNT(*) AS n FROM vdd_product_records").fetchone()["n"]),
                "history": int(db.execute("SELECT COUNT(*) AS n FROM vdd_audit").fetchone()["n"]),
                "backups": int(db.execute("SELECT COUNT(*) AS n FROM vdd_backups").fetchone()["n"]),
                "uploaded_zips": int(db.execute("SELECT COUNT(*) AS n FROM vdd_upload_archives").fetchone()["n"]),
                "legacy_documents": int(db.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]) if "documents" in tables else 0,
                "legacy_vendors": int(db.execute("SELECT COUNT(*) AS n FROM vendors").fetchone()["n"]) if "vendors" in tables else 0,
            }

        if not self.cloud:
            counts["stored_document_files"] = sum(1 for p in self.files_dir.rglob("*") if p.is_file()) if self.files_dir.exists() else 0
            counts["stored_zip_files"] = sum(1 for p in self.uploads_dir.rglob("*") if p.is_file()) if self.uploads_dir.exists() else 0
            allowed = {self.db_path.name, self.files_dir.name, self.uploads_dir.name}
            counts["other_runtime_files"] = sum(
                1 for child in self.data_dir.iterdir()
                if child.name not in allowed
                for p in ([child] if child.is_file() else child.rglob("*"))
                if p.is_file()
            )
        else:
            counts["stored_document_files"] = 0
            counts["stored_zip_files"] = 0
            counts["other_runtime_files"] = 0

        counts["clean"] = not any(value for key, value in counts.items() if key != "clean")
        return counts

    def reset_data(self, confirmation: str) -> tuple[str, bytes]:
        """Permanently delete ALL user dashboard data, including original ZIP archives."""
        if confirmation not in {"RESET HISTORY", "RESET DATA"}:
            raise ValueError("Type RESET HISTORY exactly to confirm.")

        # First clear all database-backed user data in one transaction.
        with self.connection() as db:
            if self.cloud:
                db.execute("LOCK TABLE vdd_documents, vdd_product_records, vdd_vendors, vdd_audit, vdd_backups, vdd_upload_archives IN ACCESS EXCLUSIVE MODE")
            else:
                db.execute("BEGIN IMMEDIATE")

            db.execute("DELETE FROM vdd_documents")
            db.execute("DELETE FROM vdd_product_records")
            db.execute("DELETE FROM vdd_vendors")
            db.execute("DELETE FROM vdd_company_aliases")
            db.execute("DELETE FROM vdd_review_queue")
            db.execute("DELETE FROM vdd_audit")
            db.execute("DELETE FROM vdd_backups")
            db.execute("DELETE FROM vdd_upload_archives")

            if not self.cloud:
                tables = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                for legacy in ("documents", "vendors"):
                    if legacy in tables:
                        db.execute(f"DELETE FROM {legacy}")
                if "vdd_meta" in tables:
                    db.execute("DELETE FROM vdd_meta")
                # Keep only this non-user marker so deleted legacy rows cannot be migrated back.
                db.execute("INSERT INTO vdd_meta(key,value) VALUES('legacy_migrated',?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("reset-history",))

        if not self.cloud:
            # Delete actual document and original ZIP bytes. Do not silently ignore failures:
            # the UI must never report success while a file remains on disk.
            for folder in (self.files_dir, self.uploads_dir):
                if folder.exists():
                    shutil.rmtree(folder)
                folder.mkdir(parents=True, exist_ok=True)

            # Delete generated reports, old migration backups and any other runtime artifacts.
            for child in list(self.data_dir.iterdir()):
                if child.name in {self.db_path.name, self.uploads_dir.name, self.files_dir.name}:
                    continue
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink(missing_ok=True)

            # Compact SQLite so deleted rows are not retained in unused database pages.
            with sqlite3.connect(self.db_path) as db:
                db.execute("VACUUM")

        status = self.reset_status()
        if not status["clean"]:
            remaining = ", ".join(f"{k}={v}" for k, v in status.items() if k != "clean" and v)
            raise RuntimeError(f"Delete All Data did not finish completely ({remaining}). Please close any open exported files and retry.")

        # Preserve the historical return shape for callers; a destructive reset creates no backup.
        return "", b""

    def backup_list(self) -> list[dict]:
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT id,created_at FROM vdd_backups ORDER BY created_at DESC").fetchall()]

    def read_backup(self, backup_id: str) -> bytes:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM vdd_backups WHERE id=?", (backup_id,)).fetchone()
        if row is None:
            raise ValueError("Backup not found.")
        return bytes(row["payload"])

    def restore_backup(self, content: bytes) -> dict:
        """Merge this app's backup after validating all members, before writes."""
        from vendor_core import DOCUMENT_TYPES
        with zipfile.ZipFile(BytesIO(content)) as archive:
            if len(archive.infolist()) > 10000 or sum(i.file_size for i in archive.infolist()) > 1024**3:
                raise ValueError("Backup exceeds the safety limits.")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("format") != "vendor-dashboard-backup-v3":
                raise ValueError("Use a backup ZIP made by this dashboard, not a vendor document ZIP.")
            validated = []
            validated_uploads = []
            for row in manifest["documents"]:
                digest = row["file_hash"]
                if not re.fullmatch(r"[0-9a-f]{64}", digest) or row["backup_member"] != "files/"+digest:
                    raise ValueError("Unsafe backup member.")
                payload = archive.read(row["backup_member"])
                if hashlib.sha256(payload).hexdigest() != digest:
                    raise ValueError("Backup file integrity check failed.")
                types = normalize_document_types(json.loads(row["types_json"]))
                if any(t not in DOCUMENT_TYPES for t in types):
                    raise ValueError("Unknown document type in backup.")
                validated.append((row, payload, types))
            for row in manifest.get("uploads", []):
                digest = row["file_hash"]
                member = row.get("backup_member", "")
                if not re.fullmatch(r"[0-9a-f]{64}", digest) or member != "uploads/" + digest + ".zip":
                    raise ValueError("Unsafe uploaded-ZIP backup member.")
                payload = archive.read(member)
                if hashlib.sha256(payload).hexdigest() != digest or not zipfile.is_zipfile(BytesIO(payload)):
                    raise ValueError("Uploaded ZIP backup integrity check failed.")
                names = json.loads(row.get("company_names_json", "[]"))
                validated_uploads.append((row, payload, names))
        if manifest["vendors"]:
            self.upsert_vendors(pd.DataFrame(manifest["vendors"]))
            with self.connection() as db:
                for row in manifest["vendors"]:
                    if row.get("canonical_id"):
                        db.execute("UPDATE vdd_vendors SET canonical_id=? WHERE company_key=?", (row["canonical_id"], row["company_key"]))
        for row in manifest.get("aliases", []):
            with self.connection() as db:
                self._record_alias(db, row["canonical_key"], row["alias_name"], row.get("source", "Backup restore"), int(row.get("confidence", 100)), row.get("status", "Confirmed"))
        restored = duplicates = 0
        for row, payload, types in validated:
            decision = Decision(row["company_name"] or None, tuple(types), row["method"], row["reason"], row.get("handover", ""))
            added = self.save_document(row["original_path"], payload, decision, row.get("source_batch", "Backup restore"))
            restored += int(added); duplicates += int(not added)
            document_id = self.document_id_for(row["original_path"], payload, row["company_name"] or "")
            if document_id is not None:
                from catalogue_extraction import extract_records
                kind, records = extract_records(row["original_path"], payload)
                if kind:
                    self.add_document_types(document_id, ["Price" if kind == "price" else "Catalogue"])
                self.save_product_records(document_id, records)
            if added:
                with self.connection() as db:
                    db.execute("UPDATE vdd_documents SET uploaded_at=?,reviewed=? WHERE company_key=? AND file_hash=?",
                               (row["uploaded_at"], int(row["reviewed"]), company_key(row["company_name"]) if row["company_name"] else "", row["file_hash"]))
        restored_uploads = 0
        for row, payload, names in validated_uploads:
            info = self.save_upload_archive(row.get("filename", "Vendor_Upload.zip"), payload, names,
                                            int(row.get("processed_files", 0)), int(row.get("issues_count", 0)))
            restored_uploads += int(info["added"])
            with self.connection() as db:
                db.execute("""UPDATE vdd_upload_archives SET first_uploaded_at=?,last_uploaded_at=?,upload_count=?
                    WHERE file_hash=?""", (row.get("first_uploaded_at", now()), row.get("last_uploaded_at", now()),
                    max(1, int(row.get("upload_count", 1))), row["file_hash"]))
        result = {"restored": restored, "duplicates": duplicates, "companies": len(manifest["vendors"]), "upload_archives": restored_uploads}
        self.log_event("Backup restored", result)
        return result
