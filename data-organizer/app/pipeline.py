"""The end-to-end pipeline: discover -> extract (RAW layer) -> clean -> resolve -> duplicates -> quality.

Every stage is incremental and resumable: it only works on records that have no result yet, so a stopped or
crashed run continues where it left off and adding new files never re-does old work. Source files are only ever
read. Errors are isolated per file / sheet / page / row, written to ``processing_errors`` and the log, and never
abort the whole run.
"""
from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from .cleaning.name_cleaner import NameCleaner
from .cleaning.normalizer import Gazetteer, is_null_like, normalize_text
from .cleaning.record_cleaner import RecordCleaner
from .config import (
    ConfigError, RuntimeConfig, Settings, assert_safe_paths, effective_output_dir, parse_drive_folder_id,
)
from .database.repository import Repository
from .ingestion.base import SourceFile, UnsupportedFileError, file_sha256
from .ingestion.discovery import scan_local_folder
from .ingestion.extractor import FileExtractor
from .logging_setup import audit, get_logger
from .matching.duplicates import detect_duplicates
from .matching.entity_resolution import EntityResolver, ResolveStats

log = get_logger("application")
STAGES = ("scan", "extract", "clean", "resolve", "duplicates", "quality")


class Cancelled(Exception):
    """Raised inside a stage when the user pressed Stop (the run can be resumed later)."""


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


# ---------------------------------------------------------------------------
# Progress (thread-safe; polled by the dashboard)
# ---------------------------------------------------------------------------
class Progress:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.running = False
            self.stages: dict[str, dict[str, Any]] = {}
            self.message = ""
            self.error = ""
            self.started_at: str | None = None
            self.finished_at: str | None = None
            self.summary: dict[str, Any] = {}
            self.cancel = threading.Event()

    def begin(self, stages: list[str]) -> None:
        with self._lock:
            self.reset()
            self.running = True
            self.started_at = _now().isoformat()
            self.stages = {s: {"status": "pending", "total": 0, "done": 0, "message": ""} for s in stages}

    def stage(self, name: str, status: str | None = None, total: int | None = None, done: int | None = None, message: str | None = None) -> None:
        with self._lock:
            st = self.stages.setdefault(name, {"status": "pending", "total": 0, "done": 0, "message": ""})
            if status is not None:
                st["status"] = status
            if total is not None:
                st["total"] = int(total)
            if done is not None:
                st["done"] = int(done)
            if message is not None:
                st["message"] = message
                self.message = f"{name}: {message}" if message else self.message

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    def finish(self, error: str = "", summary: dict | None = None) -> None:
        with self._lock:
            self.running = False
            self.error = error
            self.finished_at = _now().isoformat()
            if summary is not None:
                self.summary = summary

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            stages = [{"name": k, **v} for k, v in self.stages.items()]
            weights = []
            for s in stages:
                if s["status"] in ("done", "skipped"):
                    weights.append(1.0)
                elif s["total"]:
                    weights.append(min(1.0, s["done"] / s["total"]))
                else:
                    weights.append(0.0)
            pct = round(100 * sum(weights) / len(weights), 1) if weights else 0.0
            return {"running": self.running, "stages": stages, "percent": pct if self.running or self.finished_at else 0.0,
                    "message": self.message, "error": self.error, "started_at": self.started_at,
                    "finished_at": self.finished_at, "summary": self.summary}


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
class LocalSource:
    kind = "local"

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser()

    def scan(self) -> Iterator[SourceFile]:
        yield from scan_local_folder(self.root)

    def ensure_local(self, sf: SourceFile) -> SourceFile:
        return sf

    def describe(self) -> str:
        return str(self.root)


def make_source(settings: Settings, cfg: RuntimeConfig):
    if cfg.source_kind == "drive":
        from .drive.google_drive import DriveSource

        folder = parse_drive_folder_id(cfg.drive_folder or settings.drive_folder_id or settings.drive_folder_url)
        if not folder:
            raise ConfigError("No Google Drive folder configured. Paste the folder URL in the dashboard or set GOOGLE_DRIVE_FOLDER_URL.")
        return DriveSource(settings, folder)
    # Runtime configuration is authoritative.  An empty value means the user
    # deliberately cleared the source; never fall back to demo/environment
    # files and show unrelated data in the dashboard.
    root = cfg.local_input_dir
    if not root:
        raise ConfigError("No input folder configured. Set a local folder or a Google Drive folder in the configuration.")
    return LocalSource(root)


# ---------------------------------------------------------------------------
class Pipeline:
    def __init__(self, settings: Settings, cfg: RuntimeConfig, repo: Repository, progress: Progress | None = None,
                 source: Any = None):
        self.settings, self.cfg, self.repo = settings, cfg, repo
        self.progress = progress or Progress()
        self._source = source
        self.gaz = Gazetteer()
        self._output_check()

    def _output_check(self) -> None:
        if self.cfg.source_kind == "local" and self.cfg.local_input_dir:
            assert_safe_paths(self.cfg.local_input_dir, effective_output_dir(self.cfg, self.settings))

    @property
    def source(self):
        if self._source is None:
            self._source = make_source(self.settings, self.cfg)
        return self._source

    # ------------------------------------------------------------ discovery
    def discover(self) -> list[SourceFile]:
        """List every file (sub-folders included) and register it in ``source_files``. Reads nothing else."""
        files = list(self.source.scan())
        for sf in files:
            existing = self.repo.get_source_file(sf.file_key)
            # Resuming a large import used to issue a database update for every
            # unchanged file before extraction could begin. Keep its existing
            # status and metadata instead; changed and new files still flow
            # through the normal registration path below.
            if existing and existing["size_bytes"] == sf.size and existing["modified_time"] == sf.modified \
                    and existing.get("source_path") == sf.path and existing.get("ext") == sf.ext:
                continue
            status = existing["status"] if existing else ("discovered" if sf.supported else "unsupported")
            changed = existing and (existing["size_bytes"] != sf.size or existing["modified_time"] != sf.modified)
            if changed and existing["status"] in ("processed", "duplicate_file"):
                status = "changed"
            if not sf.supported:
                status = "unsupported"
            self.repo.upsert_source_file(
                file_key=sf.file_key, source_kind=sf.origin, file_name=sf.name, source_path=sf.path, mime_type=sf.mime,
                ext=sf.ext, size_bytes=sf.size, modified_time=sf.modified, local_path=str(sf.local_path), status=status,
                notes="Unsupported file type - listed but not read" if not sf.supported else None,
            )
        audit("discover", source=self.source.describe(), files=len(files))
        return files

    def inventory(self) -> list[dict]:
        return self.repo.dicts(
            "SELECT file_key, file_name, source_path, ext, size_bytes, modified_time, status, records_extracted, sheets, pages, "
            "notes, error, processed_at FROM source_files ORDER BY source_path")

    # ------------------------------------------------------------ inspection
    def inspect(self, files: list[SourceFile] | None = None, max_files: int | None = None) -> list[dict]:
        """Structure report per file (sheets, header rows, column mapping, sample rows) WITHOUT writing any records."""
        files = files if files is not None else self.discover()
        out: list[dict] = []
        ex = FileExtractor(self.settings, self.cfg)
        for sf in files[: max_files or len(files)]:
            entry: dict[str, Any] = {"file": sf.name, "path": sf.path, "ext": sf.ext, "size_bytes": sf.size, "supported": sf.supported}
            if not sf.supported:
                entry.update(status="unsupported", records=0, blocks=[], issues=[{"severity": "warning", "location": "file",
                             "message": "File type is not supported and will be listed only"}])
                out.append(entry)
                continue
            try:
                local = self.source.ensure_local(sf)
                n = 0
                for batch in ex.extract(local, inspect=True):
                    n += len(batch)
                rep = ex.report.to_dict()
                if rep["meta"].get("needs_ocr") or rep["meta"].get("scanned_pages"):
                    rep["issues"].append({"severity": "info", "location": "file", "message":
                                          "Scanned content: the number of records is only known after OCR during the real run"})
                entry.update(status="ok", records=rep["records"], sheets=rep["sheets"], pages=rep["pages"], blocks=rep["blocks"],
                             issues=rep["issues"], meta=rep["meta"], blank_rows=rep["blank_rows"])
            except Exception as exc:
                entry.update(status="error", records=0, blocks=[], issues=[{"severity": "error", "location": "file", "message": str(exc)}])
            out.append(entry)
        return out

    # ------------------------------------------------------------ run
    def run(self, limit_per_file: int | None = None, stages: tuple[str, ...] = STAGES) -> dict[str, Any]:
        prog = self.progress
        prog.begin(list(stages))
        run_id = self._run_start()
        summary: dict[str, Any] = {}
        try:
            files: list[SourceFile] = []
            if "scan" in stages:
                prog.stage("scan", "running", message="Listing files")
                files = self.discover()
                prog.stage("scan", "done", total=len(files), done=len(files), message=f"{len(files)} files found")
            prog.check_cancel()
            if "extract" in stages:
                summary["extract"] = self._extract(files, limit_per_file)
            prog.check_cancel()
            self._prime_gazetteer()
            if "clean" in stages:
                summary["clean"] = self._clean()
            prog.check_cancel()
            if "resolve" in stages:
                summary["resolve"] = self._resolve()
            prog.check_cancel()
            if "duplicates" in stages:
                summary["duplicates"] = self._duplicates()
            if "quality" in stages:
                summary["quality"] = self._quality()
            summary["totals"] = self.totals()
            prog.finish(summary=summary)
            self._run_end(run_id, "completed")
            audit("pipeline_completed", **{k: v for k, v in summary.get("totals", {}).items()})
        except Cancelled:
            for s in prog.stages.values():
                if s["status"] == "running":
                    s["status"] = "stopped"
            prog.finish(error="Stopped by user - progress is saved; press Start to resume")
            self._run_end(run_id, "cancelled")
        except Exception as exc:
            log.exception("pipeline failed")
            self.repo.add_error("pipeline", str(exc), stage="pipeline", exc=exc)
            for s in prog.stages.values():
                if s["status"] == "running":
                    s["status"] = "failed"
            prog.finish(error=f"{type(exc).__name__}: {exc}")
            self._run_end(run_id, "failed", str(exc))
        return summary

    def _run_start(self) -> int:
        self.repo.execute("INSERT INTO pipeline_runs (stage, status, started_at, updated_at) VALUES ('pipeline', 'running', ?, ?)",
                          [_now(), _now()])
        return int(self.repo.scalar("SELECT max(run_id) FROM pipeline_runs"))

    def _run_end(self, run_id: int, status: str, message: str = "") -> None:
        self.repo.execute("UPDATE pipeline_runs SET status=?, message=?, finished_at=?, updated_at=? WHERE run_id=?",
                          [status, message[:500], _now(), _now(), run_id])

    # ------------------------------------------------------------ extract
    def _extract(self, files: list[SourceFile], limit: int | None) -> dict[str, int]:
        prog = self.progress
        fast_pdf_limit = 100 * 1024 * 1024
        prog.stage("extract", "running", total=len(files), done=0)
        ex = FileExtractor(self.settings, self.cfg)
        counts = {"files_processed": 0, "files_skipped": 0, "files_failed": 0, "duplicate_files": 0, "unsupported": 0, "records": 0}
        for i, sf in enumerate(files):
            prog.check_cancel()
            prog.stage("extract", done=i, message=sf.path)
            if not sf.supported:
                counts["unsupported"] += 1
                self.repo.add_error(sf.name, f"Unsupported file type '{sf.ext}' - not read", file_key=sf.file_key,
                                    stage="extract", severity="warning", error_type="unsupported_file")
                continue
            existing = self.repo.get_source_file(sf.file_key)
            if existing and existing["status"] == "processed" and existing["size_bytes"] == sf.size \
                    and existing["modified_time"] == sf.modified and not limit:
                counts["files_skipped"] += 1
                continue
            # In the fast pass, retain scanned/image documents for the OCR pass
            # rather than retrying the same expensive files every time Resume is
            # pressed. Their native-text records, if any, stay available.
            if existing and existing["status"] == "deferred_ocr" and existing["size_bytes"] == sf.size \
                    and existing["modified_time"] == sf.modified and not self.cfg.ocr_enabled and not limit:
                counts["files_skipped"] += 1
                continue
            # Table detection in image-heavy catalogues can be as expensive as
            # OCR even when OCR is disabled. Fast mode defers these documents
            # to the later deep/OCR pass so one 400 MB PDF cannot block the
            # whole import.
            if not self.cfg.ocr_enabled and sf.ext == ".pdf" and sf.size > fast_pdf_limit and not limit:
                self.repo.upsert_source_file(
                    file_key=sf.file_key, status="deferred_ocr",
                    notes="deferred for OCR/deep extraction pass (large PDF)", error=None,
                )
                counts["files_skipped"] += 1
                continue
            try:
                local = self.source.ensure_local(sf)
                fp = local.fingerprint or file_sha256(local.local_path)
                other = self.repo.dicts("SELECT source_path FROM source_files WHERE fingerprint = ? AND file_key <> ? "
                                        "AND status = 'processed' LIMIT 1", [fp, sf.file_key])
                if other and not limit:
                    counts["duplicate_files"] += 1
                    self.repo.upsert_source_file(file_key=sf.file_key, fingerprint=fp, status="duplicate_file",
                                                 notes=f"Identical content to '{other[0]['source_path']}' - not extracted twice",
                                                 processed_at=_now())
                    self.repo.add_error(sf.name, f"Exact duplicate of '{other[0]['source_path']}' - skipped", file_key=sf.file_key,
                                        stage="extract", severity="warning", error_type="duplicate_file")
                    continue
                if existing and existing["status"] == "processed" and existing["fingerprint"] == fp and not limit:
                    self.repo.upsert_source_file(file_key=sf.file_key, size_bytes=sf.size, modified_time=sf.modified)
                    counts["files_skipped"] += 1
                    continue
                if existing and existing["status"] in ("processed", "changed") and existing["fingerprint"] not in (None, fp):
                    self.repo.execute("UPDATE raw_records SET is_current = FALSE WHERE file_key = ?", [sf.file_key])
                    log.warning(f"{sf.path} changed since last run: old records kept as history, new content extracted")
                else:
                    self.repo.execute("DELETE FROM raw_records WHERE file_key = ? AND is_current", [sf.file_key])   # partial earlier attempt
                self.repo.upsert_source_file(file_key=sf.file_key, fingerprint=fp, status="processing", error=None)
                n = 0
                for batch in ex.extract(local, fingerprint=fp, limit=limit, batch_size=self.settings.batch_size):
                    n += self.repo.insert_raw(batch)
                    prog.check_cancel()
                rep = ex.report
                for iss in rep.issues:
                    self.repo.add_error(sf.name, iss["message"], file_key=sf.file_key, location=iss["location"], stage="extract",
                                        severity=iss["severity"], error_type="read_issue")
                notes = f"{rep.blank_rows} blank rows skipped" if rep.blank_rows else ""
                deferred_ocr = not self.cfg.ocr_enabled and bool(
                    rep.meta.get("needs_ocr") or rep.meta.get("scanned_pages")
                )
                if deferred_ocr:
                    notes = (notes + "; " if notes else "") + "deferred for OCR pass"
                if n == 0:
                    notes = (notes + "; " if notes else "") + "no records found"
                    if not deferred_ocr:
                        self.repo.add_error(sf.name, "No records could be extracted from this file", file_key=sf.file_key, stage="extract",
                                            severity="warning", error_type="empty_file")
                status = "sampled" if limit else ("deferred_ocr" if deferred_ocr else "processed")
                self.repo.upsert_source_file(file_key=sf.file_key, status=status, records_extracted=n,
                                             sheets=rep.sheets, pages=rep.pages, notes=notes or None,
                                             inspection=json.dumps(rep.to_dict(), ensure_ascii=False, default=str)[:900_000],
                                             processed_at=_now())
                counts["files_processed"] += 1
                counts["records"] += n
                log.info(f"extracted {n} records from {sf.path}")
            except Cancelled:
                raise
            except Exception as exc:                    # isolate: one bad file never stops the run
                counts["files_failed"] += 1
                self.repo.upsert_source_file(file_key=sf.file_key, status="failed", error=f"{type(exc).__name__}: {exc}")
                self.repo.add_error(sf.name, str(exc), file_key=sf.file_key, stage="extract",
                                    error_type=type(exc).__name__, exc=None if isinstance(exc, UnsupportedFileError) else exc)
        prog.stage("extract", "done", done=len(files), message=f"{counts['records']} records extracted")
        return counts

    # ------------------------------------------------------------ gazetteer
    def _prime_gazetteer(self) -> None:
        """Learn place names from the data itself (city / district columns) - evidence only, never a fill-in."""
        vals: set[str] = set()
        for col in ("original_city", "original_district"):
            for (v,) in self.repo.fetchall(f"SELECT DISTINCT {col} FROM raw_records WHERE {col} IS NOT NULL LIMIT 100000"):
                v = str(v)
                n = self.gaz.normalize_city(v) if col == "original_city" else self.gaz.normalize_district(v)
                if n and not is_null_like(n) and n.replace(" ", "").isalpha() and 3 <= len(n) <= 30 and len(n.split()) <= 3:
                    vals.add(n)
        self.gaz.add_places(vals)

    # ------------------------------------------------------------ clean
    def _clean(self) -> dict[str, int]:
        prog = self.progress
        total = int(self.repo.scalar("SELECT count(*) FROM raw_records r LEFT JOIN cleaned_records c USING (record_id) "
                                     "WHERE r.is_current AND c.record_id IS NULL"))
        prog.stage("clean", "running", total=total, done=0, message="Normalising records")
        rc = RecordCleaner(self.cfg, self.gaz)
        after, done, flagged = 0, 0, 0
        while True:
            prog.check_cancel()
            rows = self.repo.fetch_uncleaned(after, self.settings.batch_size)
            if not rows:
                break
            cleaned, issues = [], []
            now = _now()
            for r in rows:
                year = None
                mod = r.pop("_modified", None)
                if mod and str(mod)[:4].isdigit():
                    year = int(str(mod)[:4])
                row, flags = rc.clean(r, year)
                cleaned.append(row)
                for f in flags:
                    flagged += 1
                    issues.append({"record_id": r["record_id"], "master_entity_id": None, "issue_type": f.issue_type,
                                   "severity": f.severity, "field": f.field, "value": f.value, "message": f.message,
                                   "detected_at": now})
            self.repo.insert_cleaned(cleaned, issues)
            after = rows[-1]["seq"]
            done += len(rows)
            prog.stage("clean", done=done)
        prog.stage("clean", "done", done=done, message=f"{done} records cleaned")
        return {"records": done, "flags": flagged}

    # ------------------------------------------------------------ resolve
    def _resolve(self) -> dict[str, int]:
        prog = self.progress
        total = int(self.repo.scalar("SELECT count(*) FROM raw_records r JOIN cleaned_records c USING (record_id) "
                                     "LEFT JOIN resolutions m ON m.record_id = r.record_id WHERE r.is_current AND m.record_id IS NULL"))
        prog.stage("resolve", "running", total=total, done=0, message="Matching names")
        names = NameCleaner(entity_type=self.cfg.entity_type, gazetteer=self.gaz)
        resolver = EntityResolver(self.repo, self.cfg, self.settings, names)
        resolver.load()
        agg = ResolveStats()
        after, done = 0, 0
        while True:
            prog.check_cancel()
            rows = self.repo.fetch_unresolved(after, self.settings.batch_size)
            if not rows:
                break
            st = resolver.resolve_batch(rows)
            for k in agg.__dict__:
                setattr(agg, k, getattr(agg, k) + getattr(st, k))
            after = rows[-1]["seq"]
            done += len(rows)
            prog.stage("resolve", done=done, message=f"{done}/{total} records, {len(resolver.index)} entities")
        prog.stage("resolve", "done", done=done, message=f"{len(resolver.index)} master entities")
        return dict(agg.__dict__, entities=len(resolver.index))

    # ------------------------------------------------------------ duplicates / quality
    def _duplicates(self) -> dict[str, int]:
        if not self.cfg.duplicate_detection:
            self.progress.stage("duplicates", "skipped", message="disabled in configuration")
            return {}
        self.progress.stage("duplicates", "running", message="Looking for duplicates")
        res = detect_duplicates(self.repo)
        self.progress.stage("duplicates", "done", message=f"{sum(res.values())} duplicate groups")
        return res

    def _quality(self) -> dict[str, int]:
        self.progress.stage("quality", "running", message="Entity-level checks")
        self.repo.execute("DELETE FROM quality_issues WHERE record_id IS NULL")
        rows = self.repo.dicts("SELECT master_entity_id, standard_name, data_conflicts FROM master_entities "
                               "WHERE merged_into IS NULL AND data_conflicts IS NOT NULL AND data_conflicts <> ''")
        now = _now()
        issues = [{"record_id": None, "master_entity_id": r["master_entity_id"], "issue_type": "entity_conflict", "severity": "warning",
                   "field": "entity", "value": r["standard_name"], "message": "Records merged into this entity disagree: " + r["data_conflicts"],
                   "detected_at": now} for r in rows]
        if issues:
            self.repo.insert_df("quality_issues", pd.DataFrame(issues))
        self.progress.stage("quality", "done", message=f"{len(issues)} entity conflicts")
        return {"entity_conflicts": len(issues)}

    # ------------------------------------------------------------ totals
    def totals(self) -> dict[str, int]:
        r = self.repo
        return {
            "files": int(r.scalar("SELECT count(*) FROM source_files")),
            "files_processed": int(r.scalar("SELECT count(*) FROM source_files WHERE status = 'processed'")),
            "files_failed": int(r.scalar("SELECT count(*) FROM source_files WHERE status = 'failed'")),
            "records": int(r.scalar("SELECT count(*) FROM raw_records WHERE is_current")),
            "entities": int(r.scalar("SELECT count(*) FROM master_entities WHERE merged_into IS NULL")),
            "pending_reviews": int(r.scalar("SELECT count(*) FROM review_items WHERE status = 'pending'")),
            "errors": int(r.scalar("SELECT count(*) FROM processing_errors WHERE severity = 'error'")),
        }


# ---------------------------------------------------------------------------
# Dry run: a sample through the SAME code path, in a throw-away in-memory database
# ---------------------------------------------------------------------------
def dry_run(settings: Settings, cfg: RuntimeConfig, main_repo: Repository | None, source: Any, sample_size: int | None = None) -> pd.DataFrame:
    sample = sample_size or cfg.dry_run_sample_size
    tmp = Repository(":memory:")
    if main_repo is not None:                            # reuse what the user already decided
        for d in main_repo.dicts("SELECT * FROM user_decisions"):
            tmp.execute("INSERT INTO user_decisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [d["alias_normalized"], d["context_city"], d["master_entity_id"], d["decision"], d["verified_by_user"],
                         d["review_id"], d["decided_at"]])
    pipe = Pipeline(settings, cfg, tmp, source=source)
    files = [f for f in pipe.discover() if f.supported]
    per_file = max(3, math.ceil(sample / max(1, len(files))))
    pipe._extract(files, per_file)
    pipe._prime_gazetteer()
    pipe._clean()
    pipe._resolve()
    df = tmp.fetchdf(
        "SELECT r.source_file AS file, coalesce(r.source_sheet, CASE WHEN r.source_page IS NOT NULL THEN 'page ' || r.source_page END, '') AS location, "
        "r.source_row AS row, r.original_name AS original_name, c.name_normalized AS normalized_name, "
        "e.standard_name AS suggested_standard_name, m.match_score AS confidence, m.match_band AS band, m.match_status AS status, "
        "m.match_method AS method, m.reason AS reason, c.quality_severity AS data_quality "
        "FROM raw_records r JOIN cleaned_records c USING (record_id) LEFT JOIN resolutions m USING (record_id) "
        "LEFT JOIN master_entities e ON e.master_entity_id = m.master_entity_id ORDER BY r.source_file, r.seq LIMIT ?", [sample])
    tmp.close()
    return df
