"""Application service layer shared by the API and the CLI: state, background runs, queries."""
from __future__ import annotations

import json
import hashlib
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from .cleaning.name_cleaner import NameCleaner
from .config import (
    ConfigError, RuntimeConfig, Settings, effective_output_dir, get_settings, load_runtime_config, load_resource,
    save_runtime_config,
)
from .database.repository import Repository
from .export.exporter import Exporter
from .ingestion.ocr_reader import ocr_available
from .logging_setup import LOG_NAMES, audit, get_logger, redact, setup_logging
from .matching.review import ReviewService
from .pipeline import LocalSource, Pipeline, Progress, dry_run, make_source

log = get_logger("application")


def _like(v: str) -> str:
    return "%" + v.lower().replace("%", "").replace("_", " ") + "%"


def _safe_company_folder(value: str) -> str:
    safe = "".join("_" if ch in '<>:"/\\|?*' else ch for ch in value).strip(" .")
    return (safe or "Others")[:120]


def _safe_archive_member_path(name: str) -> Path:
    """Return a Windows-safe relative ZIP member path.

    Some ZIPs contain directory names with trailing spaces. Windows silently
    removes those spaces when creating the directory, which otherwise makes a
    later file write fail with WinError 3.  This changes only the extracted
    folder/file path; never the document bytes.
    """
    raw = PurePosixPath(name.replace("\\", "/"))
    if raw.is_absolute() or ".." in raw.parts:
        raise ConfigError(f"Unsafe path in archive: {name}")
    cleaned: list[str] = []
    for part in raw.parts:
        if part in {"", "."}:
            continue
        value = "".join("_" if ch in '<>:\"/\\|?*' else ch for ch in part).strip().rstrip(".")
        if not value:
            value = "unnamed"
        cleaned.append(value)
    if not cleaned:
        raise ConfigError(f"Invalid path in archive: {name}")
    return Path(*cleaned)


def _company_folder_from_source_path(source_path: str) -> str:
    """Use a vendor ZIP's existing company folder when a file has no parsed name."""
    parts = [part for part in Path(str(source_path or "")).parts if part not in {"", ".", ".."}]
    parents = parts[:-1]
    if not parents:
        return ""
    # Archive/Company/[optional subfolder]/file: Archive is not a company.
    candidates = parents[1:] if len(parents) > 1 else parents
    generic = {"catalogue", "catalog", "catalogues", "documents", "document", "files", "price list", "prices"}
    for candidate in candidates:
        if candidate.strip().lower() not in generic:
            return candidate.strip()
    return ""


class AppState:
    """One per process. Holds the database, configuration and the (single) background run."""

    def __init__(self, settings: Settings | None = None, db_path: str | Path | None = None):
        self.settings = settings or get_settings()
        setup_logging(self.settings.log_dir)
        self.repo = Repository(db_path or self.settings.db_path)
        self.cfg = load_runtime_config(self.settings)
        self.progress = Progress()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ config
    def update_config(self, data: dict[str, Any]) -> RuntimeConfig:
        with self._lock:
            if self.progress.running:
                raise ConfigError("Cannot change the configuration while a run is in progress")
            merged = {**self.cfg.to_dict(), **{k: v for k, v in data.items() if k in self.cfg.to_dict()}}
            new = RuntimeConfig.from_dict(merged)
            if new.source_kind == "local" and new.local_input_dir:
                from .config import assert_safe_paths
                assert_safe_paths(new.local_input_dir, effective_output_dir(new, self.settings))
            save_runtime_config(new, self.settings)
            changed = {k: v for k, v in new.to_dict().items() if self.cfg.to_dict().get(k) != v}
            self.cfg = new
            audit("config_changed", **{k: (v if k != "column_overrides" else f"{len(v)} overrides") for k, v in changed.items()})
            return new

    def pipeline(self, repo: Repository | None = None) -> Pipeline:
        return Pipeline(self.settings, self.cfg, repo or self.repo, progress=self.progress)

    # ------------------------------------------------------------------ runs
    def start_run(self, limit_per_file: int | None = None) -> None:
        with self._lock:
            if self.progress.running or (self._thread and self._thread.is_alive()):
                raise ConfigError("A run is already in progress")
            pipe = self.pipeline()
            _ = pipe.source                                   # fail fast on a missing folder / credentials
            self.progress.begin(["scan", "extract", "clean", "resolve", "duplicates", "quality"])
            self._thread = threading.Thread(target=lambda: pipe.run(limit_per_file=limit_per_file), name="pipeline", daemon=True)
            self._thread.start()

    def stop_run(self) -> None:
        self.progress.cancel.set()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # ------------------------------------------------------------------ status
    def tools(self) -> dict[str, Any]:
        return {
            "tesseract": ocr_available(self.settings.tesseract_cmd),
            "libreoffice": bool(shutil.which("soffice") or shutil.which("libreoffice")),
            "drive_credentials": self.settings.google_token_file.exists() or bool(self.settings.google_service_account_file)
            or self.settings.google_client_secrets_file.exists(),
        }

    def status(self) -> dict[str, Any]:
        return {"settings": self.settings.public_dict(), "config": self.cfg.to_dict(), "progress": self.progress.snapshot(),
                "totals": self.pipeline().totals(), "tools": self.tools()}

    def reviews(self) -> ReviewService:
        return ReviewService(self.repo, NameCleaner(self.cfg.entity_type))

    def exporter(self) -> Exporter:
        return Exporter(self.settings, self.cfg, self.repo)

    def reset(self, scope: str = "derived") -> None:
        if self.progress.running:
            raise ConfigError("Stop the current run first")
        with self._lock:
            if scope == "all":
                for t in ("raw_records", "source_files", "user_decisions", "processing_errors", "pipeline_runs", "kv_state", "ai_cache"):
                    self.repo.execute(f"DELETE FROM {t}")
                # A full reset should open on an empty workspace, not silently
                # reselect the demo folder used for a previous run.
                self.update_config({"source_kind": "local", "local_input_dir": "", "drive_folder": ""})
            self.repo.reset_derived()
            self.repo.execute("DELETE FROM quality_issues")
            audit("reset", scope=scope)

    def remove_unsupported_file(self, file_key: str) -> dict[str, Any]:
        """Remove one unsupported-file inventory entry without touching extracted data."""
        if self.progress.running:
            raise ConfigError("Stop the current run first")
        row = self.repo.get_source_file(file_key)
        if row is None:
            raise KeyError(file_key)
        if row["status"] != "unsupported":
            raise ConfigError("Only unsupported-file inventory entries can be removed individually.")
        self.repo.execute("DELETE FROM source_files WHERE file_key = ?", [file_key])
        audit("remove_unsupported_file", file_key=file_key, file_name=row["file_name"])
        return {"removed": row["file_name"]}

    # ------------------------------------------------------------------ demo / upload
    def prepare_demo(self) -> dict[str, Any]:
        from scripts.generate_sample_data import generate

        made = generate(self.settings.demo_input_dir)
        self.update_config({"source_kind": "local", "local_input_dir": str(self.settings.demo_input_dir), "drive_folder": ""})
        return {"folder": str(self.settings.demo_input_dir), "files": len(made) + 3}

    def save_upload_stream(self, filename: str, source: BinaryIO, max_bytes: int) -> str:
        safe = Path(filename).name
        if not safe or safe.startswith("."):
            raise ConfigError("Invalid file name")
        self.settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        dest = self.settings.uploads_dir / safe
        written = 0
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.settings.uploads_dir, prefix=f".{safe}.", suffix=".uploading", delete=False
            ) as temp:
                temp_path = Path(temp.name)
                while chunk := source.read(8 * 1024 * 1024):
                    written += len(chunk)
                    if written > max_bytes:
                        raise ConfigError(f"{safe} is larger than {max_bytes // (1024 ** 3)} GiB")
                    temp.write(chunk)
            temp_path.replace(dest)
        except Exception:
            if temp_path:
                temp_path.unlink(missing_ok=True)
            raise
        return safe

    def extract_upload_archive(self, filename: str, max_uncompressed_bytes: int) -> dict[str, int]:
        archive = self.settings.uploads_dir / Path(filename).name
        if archive.suffix.lower() != ".zip":
            return {"bytes": 0, "skipped": 0}
        # Keep extracted archives separate from their original ZIP uploads.
        # The dashboard then scans this directory once, avoiding duplicate
        # source files from a ZIP and an earlier extraction in uploads/.
        # A short managed ID avoids Windows' 260-character path limit for
        # vendor ZIPs that contain several nested folders.
        upload_id = hashlib.sha1(archive.name.encode("utf-8")).hexdigest()[:12]
        target = self.settings.uploads_dir / "_extracted" / f"upload-{upload_id}"
        target.mkdir(parents=True, exist_ok=True)
        extracted = 0
        skipped = 0
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                if member.is_dir():
                    continue
                extracted += member.file_size
                if extracted > max_uncompressed_bytes:
                    raise ConfigError(f"{archive.name} expands beyond the 10 GiB dataset limit")
                try:
                    relative = _safe_archive_member_path(member.filename)
                except ConfigError as exc:
                    skipped += 1
                    log.warning("Skipped unsafe archive member %r: %s", member.filename, exc)
                    continue
                # The archive's top-level folder duplicates the managed upload
                # folder and only makes Windows paths longer.
                if len(relative.parts) > 1:
                    relative = Path(*relative.parts[1:])
                destination = target / relative
                temporary = destination.with_name(f".{hashlib.sha1(member.filename.encode('utf-8')).hexdigest()[:12]}.part")
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    # Atomic write prevents the scanner from seeing an
                    # incomplete document if extraction is interrupted.
                    with bundle.open(member) as input_file, temporary.open("wb") as output_file:
                        shutil.copyfileobj(input_file, output_file, length=8 * 1024 * 1024)
                    temporary.replace(destination)
                except (OSError, zipfile.BadZipFile) as exc:
                    temporary.unlink(missing_ok=True)
                    skipped += 1
                    log.warning("Skipped archive member %r: %s", member.filename, exc)
        return {"bytes": extracted, "skipped": skipped}

    def organize_source_files(self) -> dict[str, Any]:
        """Copy source files into company folders without modifying the originals."""
        root = Path(self.settings.data_dir) / "organized_by_company"
        root.mkdir(parents=True, exist_ok=True)
        input_root = Path(self.cfg.local_input_dir).expanduser()
        rows = self.repo.dicts(
            "SELECT DISTINCT sf.file_key, sf.file_name, sf.local_path, sf.status, sf.source_path, "
            "coalesce(nullif(e.standard_name, ''), '') AS company "
            "FROM source_files sf LEFT JOIN raw_records r ON r.file_key = sf.file_key AND r.is_current "
            "LEFT JOIN resolutions res ON res.record_id = r.record_id "
            "LEFT JOIN master_entities e ON e.master_entity_id = res.master_entity_id AND e.merged_into IS NULL "
            "ORDER BY sf.source_path"
        )
        files: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = files.setdefault(row["file_key"], {
                "name": row["file_name"], "path": row["local_path"], "source_path": row["source_path"],
                "status": row["status"], "folders": set(),
            })
            if row["company"]:
                item["folders"].add(row["company"])
        copied = 0
        work_related = 0
        skipped = 0
        for item in files.values():
            try:
                source = Path(item["path"]) if item["path"] else None
                if not source or not source.is_file():
                    source = input_root / str(item["source_path"] or "")
                if not source.is_file():
                    skipped += 1
                    continue
            except OSError:
                skipped += 1
                continue
            if item["folders"]:
                folders = item["folders"]
            elif inferred_company := _company_folder_from_source_path(str(item["source_path"] or "")):
                # Preserve a supplied company-folder layout if OCR or extraction
                # cannot determine the company from the file itself.
                folders = {inferred_company}
            elif item["status"] == "unsupported":
                folders = {"Work Related Data/Unsupported"}
            elif item["status"] == "failed":
                folders = {"Work Related Data/Failed to Fetch"}
            else:
                folders = {"Work Related Data/Unmatched"}
            for company in folders:
                folder = root.joinpath(*[_safe_company_folder(part) for part in company.split("/")])
                try:
                    folder.mkdir(parents=True, exist_ok=True)
                    destination = folder / source.name
                    if destination.exists() and destination.stat().st_size == source.stat().st_size:
                        continue
                    shutil.copy2(source, destination)
                    copied += 1
                    if company.startswith("Work Related Data"):
                        work_related += 1
                except OSError:
                    skipped += 1
        return {"folder": str(root), "files_copied": copied, "work_related": work_related, "skipped": skipped,
                "company_folders": len([p for p in root.iterdir() if p.is_dir()])}

    def create_structured_zip(self) -> dict[str, Any]:
        """Package company folders in the same root-folder shape as the example ZIP."""
        organized = Path(self.settings.data_dir) / "organized_by_company"
        if not organized.exists():
            self.organize_source_files()
        out = effective_output_dir(self.cfg, self.settings)
        out.mkdir(parents=True, exist_ok=True)
        archive = out / "Organized Vendor Data.zip"
        # Building this archive can take hours for a large collection.  A ZIP is
        # written atomically below, so an existing final archive is safe to
        # reuse instead of making the dashboard wait to rebuild it each time.
        if archive.exists() and zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as bundle:
                return {"file": archive.name, "bytes": archive.stat().st_size, "files": len(bundle.infolist()), "cached": True}
        temp = archive.with_suffix(".zip.tmp")
        count = 0
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
            for path in sorted(organized.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(organized).as_posix()
                bundle.write(path, f"Organized Vendor Data/{relative}")
                count += 1
        temp.replace(archive)
        return {"file": archive.name, "bytes": archive.stat().st_size, "files": count}

    # ------------------------------------------------------------------ queries
    def summary(self) -> dict[str, Any]:
        r = self.repo
        return {
            "totals": self.pipeline().totals(),
            "bands": r.dicts("SELECT coalesce(nullif(match_band, ''), 'n/a') AS label, count(*) AS n FROM resolutions "
                             "WHERE match_status IN ('auto_matched', 'verified_mapping', 'needs_review', 'user_accepted') GROUP BY 1"),
            "status": r.dicts("SELECT match_status AS label, count(*) AS n FROM resolutions GROUP BY 1 ORDER BY 2 DESC"),
            "by_state": r.dicts("SELECT coalesce(nullif(state, ''), 'Unknown') AS label, count(*) AS n FROM master_entities "
                                "WHERE merged_into IS NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 12"),
            "files": r.dicts("SELECT status AS label, count(*) AS n FROM source_files GROUP BY 1 ORDER BY 2 DESC"),
            "quality": r.dicts("SELECT issue_type AS label, severity, count(*) AS n FROM quality_issues GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 10"),
            "entity_status": r.dicts("SELECT verification_status AS label, count(*) AS n FROM master_entities WHERE merged_into IS NULL GROUP BY 1"),
            "duplicates": r.dicts("SELECT dup_type AS label, count(DISTINCT group_id) AS n FROM duplicate_groups GROUP BY 1"),
        }

    def entities(self, search: str = "", state: str = "", city: str = "", status: str = "", sort: str = "standard_name",
                 direction: str = "asc", limit: int = 50, offset: int = 0) -> dict[str, Any]:
        where, p = ["merged_into IS NULL"], []
        if search:
            where.append("(lower(standard_name) LIKE ? OR lower(aliases) LIKE ? OR lower(registration_id) LIKE ? OR phone LIKE ? OR lower(master_entity_id) LIKE ?)")
            p += [_like(search)] * 3 + [f"%{search}%", _like(search)]
        for col, val in (("state", state), ("city", city), ("verification_status", status)):
            if val:
                where.append(f"lower({col}) = ?")
                p.append(val.lower())
        order = sort if sort in {"standard_name", "city", "state", "record_count", "confidence", "source_file_count", "master_entity_id"} else "standard_name"
        d = "DESC" if direction.lower() == "desc" else "ASC"
        w = " AND ".join(where)
        total = int(self.repo.scalar(f"SELECT count(*) FROM master_entities WHERE {w}", p))
        rows = self.repo.dicts(
            f"SELECT master_entity_id, standard_name, city, district, state, phone, email, registration_id, entity_type, category, "
            f"confidence, verification_status, record_count, source_file_count, data_conflicts, aliases FROM master_entities WHERE {w} "
            f"ORDER BY {order} {d}, master_entity_id LIMIT ? OFFSET ?", p + [max(1, min(limit, 500)), max(0, offset)])
        for r in rows:
            r["aliases"] = json.loads(r["aliases"] or "[]")
        return {"total": total, "items": rows}

    def entity(self, eid: str) -> dict[str, Any]:
        rows = self.repo.dicts("SELECT * EXCLUDE (state_json) FROM master_entities WHERE master_entity_id = ?", [eid])
        if not rows:
            raise KeyError(eid)
        e = rows[0]
        for k in ("aliases", "all_phones", "all_emails", "all_websites"):
            e[k] = json.loads(e[k] or "[]")
        e["data_conflicts"] = json.loads(e["data_conflicts"]) if e.get("data_conflicts") else {}
        e["alias_details"] = self.repo.dicts("SELECT alias, alias_normalized, occurrences, match_score, match_method, verified, original_source "
                                             "FROM aliases WHERE master_entity_id = ? ORDER BY occurrences DESC", [eid])
        e["records"] = self.repo.dicts(
            "SELECT r.record_id, r.source_file, r.source_path, r.source_sheet, r.source_page, r.source_row, r.original_name, r.original_address, "
            "r.original_city, r.original_phone, m.match_score, m.match_band, m.match_method, m.match_status, m.reason, m.verified_by_user "
            "FROM resolutions m JOIN raw_records r USING (record_id) WHERE m.master_entity_id = ? AND r.is_current ORDER BY r.seq LIMIT 500", [eid])
        e["merged_from"] = self.repo.dicts("SELECT master_entity_id, standard_name FROM master_entities WHERE merged_into = ?", [eid])
        return e

    def records(self, search: str = "", status: str = "", file: str = "", limit: int = 50, offset: int = 0) -> dict[str, Any]:
        where, p = ["r.is_current"], []
        if search:
            where.append("(lower(r.original_name) LIKE ? OR lower(e.standard_name) LIKE ?)")
            p += [_like(search)] * 2
        if status:
            where.append("m.match_status = ?")
            p.append(status)
        if file:
            where.append("r.source_file = ?")
            p.append(file)
        w = " AND ".join(where)
        base = ("FROM raw_records r LEFT JOIN resolutions m USING (record_id) LEFT JOIN master_entities e ON e.master_entity_id = m.master_entity_id "
                "LEFT JOIN cleaned_records c USING (record_id)")
        total = int(self.repo.scalar(f"SELECT count(*) {base} WHERE {w}", p))
        rows = self.repo.dicts(
            f"SELECT r.record_id, r.source_file, r.source_sheet, r.source_page, r.source_row, r.original_name, c.name_normalized, "
            f"e.standard_name, m.master_entity_id, m.match_score, m.match_band, m.match_status, m.reason, c.quality_severity "
            f"{base} WHERE {w} ORDER BY r.seq LIMIT ? OFFSET ?", p + [max(1, min(limit, 500)), max(0, offset)])
        return {"total": total, "items": rows}

    def duplicates(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        total = int(self.repo.scalar("SELECT count(DISTINCT group_id) FROM duplicate_groups"))
        ids = [r[0] for r in self.repo.fetchall("SELECT DISTINCT group_id FROM duplicate_groups ORDER BY group_id LIMIT ? OFFSET ?", [limit, offset])]
        groups = []
        for gid in ids:
            rows = self.repo.dicts(
                "SELECT g.dup_type, g.dup_key, g.similarity, g.is_suggested_primary, g.record_id, g.master_entity_id, r.source_file, r.source_row, "
                "r.original_name, e.standard_name FROM duplicate_groups g LEFT JOIN raw_records r USING (record_id) "
                "LEFT JOIN master_entities e ON e.master_entity_id = g.master_entity_id WHERE g.group_id = ? ORDER BY g.is_suggested_primary DESC", [gid])
            groups.append({"group_id": gid, "type": rows[0]["dup_type"], "key": rows[0]["dup_key"], "members": rows})
        return {"total": total, "groups": groups}

    def quality(self, severity: str = "", issue: str = "", limit: int = 100, offset: int = 0) -> dict[str, Any]:
        where, p = ["1=1"], []
        if severity:
            where.append("qi.severity = ?")
            p.append(severity)
        if issue:
            where.append("qi.issue_type = ?")
            p.append(issue)
        w = " AND ".join(where)
        total = int(self.repo.scalar(f"SELECT count(*) FROM quality_issues qi WHERE {w}", p))
        rows = self.repo.dicts(
            f"SELECT qi.record_id, qi.master_entity_id, qi.issue_type, qi.severity, qi.field, qi.value, qi.message, r.source_file, r.source_sheet, "
            f"r.source_page, r.source_row, r.original_name FROM quality_issues qi LEFT JOIN raw_records r USING (record_id) WHERE {w} "
            f"ORDER BY CASE qi.severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, qi.issue_type LIMIT ? OFFSET ?", p + [limit, offset])
        counts = self.repo.dicts("SELECT issue_type, severity, count(*) AS n FROM quality_issues GROUP BY 1, 2 ORDER BY 3 DESC")
        return {"total": total, "items": rows, "counts": counts}

    def errors(self, limit: int = 200, offset: int = 0) -> dict[str, Any]:
        return {"total": int(self.repo.scalar("SELECT count(*) FROM processing_errors")),
                "items": self.repo.dicts("SELECT occurred_at, file_name, location, stage, severity, error_type, message FROM processing_errors "
                                         "ORDER BY error_id DESC LIMIT ? OFFSET ?", [limit, offset])}

    def file_inspection(self, file_key: str) -> dict[str, Any] | None:
        row = self.repo.get_source_file(file_key)
        if not row:
            return None
        row["inspection"] = json.loads(row["inspection"]) if row.get("inspection") else None
        return row

    def schema_info(self) -> dict[str, Any]:
        res = load_resource("schema_aliases.json")
        from .config import UNIVERSAL_FIELDS
        return {"fields": list(UNIVERSAL_FIELDS), "aliases": res["fields"], "overrides": self.cfg.column_overrides}

    def tail_log(self, name: str, lines: int = 200) -> list[str]:
        if name not in LOG_NAMES:
            raise KeyError(name)
        path = self.settings.log_dir / f"{name}.log"
        if not path.exists():
            return []
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 400_000))
            data = fh.read().decode("utf-8", errors="replace").splitlines()
        return [redact(x) for x in data[-max(1, min(lines, 2000)):]]

    def list_outputs(self) -> list[dict[str, Any]]:
        out = effective_output_dir(self.cfg, self.settings)
        if not out.exists():
            return []
        files = []
        for p in sorted(out.rglob("*")):
            if p.is_file() and not p.name.endswith(".tmp") and p.name != ".gitkeep":
                files.append({"file": p.relative_to(out).as_posix(), "bytes": p.stat().st_size,
                              "modified": p.stat().st_mtime})
        return files

    def resolve_output(self, rel: str) -> Path:
        out = effective_output_dir(self.cfg, self.settings).resolve()
        p = (out / rel).resolve()
        if out not in p.parents or not p.is_file():
            raise FileNotFoundError(rel)
        return p

    def dry_run(self, sample: int | None = None):
        src = make_source(self.settings, self.cfg)
        return dry_run(self.settings, self.cfg, self.repo, src, sample)


_state: AppState | None = None
_state_lock = threading.Lock()


def get_state() -> AppState:
    global _state
    with _state_lock:
        if _state is None:
            _state = AppState()
        return _state


def set_state(state: AppState | None) -> None:
    global _state
    _state = state
