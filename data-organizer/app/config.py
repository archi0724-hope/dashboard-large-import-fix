"""Configuration.

Two layers:

* ``Settings``      - static, read from environment / ``.env`` (paths, credentials, API keys).
                      Secrets live ONLY here and are never written to disk by the app,
                      never returned by the API and never logged.
* ``RuntimeConfig`` - user-tunable processing options (thresholds, toggles, folder selection).
                      Editable from the dashboard, persisted as JSON in ``data/runtime_config.json``.
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field, fields
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
RESOURCES_DIR = BASE_DIR / "config"

load_dotenv(BASE_DIR / ".env")
load_dotenv()  # also honour a .env in the current working directory


class ConfigError(ValueError):
    """Raised for invalid configuration (unsafe paths, bad thresholds, ...)."""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _path(name: str, default: Path) -> Path:
    raw = _env(name)
    p = Path(raw).expanduser() if raw else default
    return p if p.is_absolute() else (BASE_DIR / p).resolve()


@dataclass(frozen=True)
class Settings:
    """Environment-level settings. Secrets are excluded from ``public_dict``."""

    data_dir: Path
    output_dir: Path
    log_dir: Path
    db_path: Path
    secrets_dir: Path
    # Google Drive
    drive_folder_url: str
    drive_folder_id: str
    google_client_secrets_file: Path
    google_token_file: Path
    google_service_account_file: Path | None
    # Local input (alternative to Drive; also used by demo mode)
    local_input_dir: str
    # OCR
    ocr_enabled: bool
    ocr_lang: str
    tesseract_cmd: str
    ocr_dpi: int
    # Processing
    entity_type: str
    batch_size: int
    excel_stream_threshold_mb: int
    # Dashboard
    dashboard_host: str
    dashboard_port: int
    dashboard_token: str = ""

    # ---- derived paths -------------------------------------------------------
    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def runtime_config_path(self) -> Path:
        return self.data_dir / "runtime_config.json"

    @property
    def demo_input_dir(self) -> Path:
        return self.data_dir / "demo_input"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.raw_dir, self.staging_dir, self.output_dir, self.log_dir, self.secrets_dir):
            p.mkdir(parents=True, exist_ok=True)

    def public_dict(self) -> dict[str, Any]:
        """Non-secret view of the settings (safe to show in the UI / logs)."""
        return {
            "data_dir": str(self.data_dir),
            "output_dir": str(self.output_dir),
            "db_path": str(self.db_path),
            "drive_folder_configured": bool(self.drive_folder_id or self.drive_folder_url),
            "google_oauth_client_file_present": self.google_client_secrets_file.exists(),
            "google_token_present": self.google_token_file.exists(),
            "google_service_account_configured": bool(self.google_service_account_file),
            "ocr_enabled": self.ocr_enabled,
            "ocr_lang": self.ocr_lang,
            "entity_type": self.entity_type,
            "batch_size": self.batch_size,
            "dashboard_token_required": bool(self.dashboard_token),
        }


def load_settings() -> Settings:
    data_dir = _path("DATA_DIR", BASE_DIR / "data")
    secrets_dir = _path("SECRETS_DIR", BASE_DIR / "secrets")
    sa = _env("GOOGLE_SERVICE_ACCOUNT_FILE")
    s = Settings(
        data_dir=data_dir,
        output_dir=_path("OUTPUT_DIR", data_dir / "output"),
        log_dir=_path("LOG_DIR", BASE_DIR / "logs"),
        db_path=_path("DB_PATH", data_dir / "organizer.duckdb"),
        secrets_dir=secrets_dir,
        drive_folder_url=_env("GOOGLE_DRIVE_FOLDER_URL"),
        drive_folder_id=_env("GOOGLE_DRIVE_FOLDER_ID"),
        google_client_secrets_file=_path("GOOGLE_OAUTH_CLIENT_SECRETS_FILE", secrets_dir / "client_secret.json"),
        google_token_file=_path("GOOGLE_TOKEN_FILE", secrets_dir / "token.json"),
        google_service_account_file=Path(sa).expanduser() if sa else None,
        local_input_dir=_env("LOCAL_INPUT_DIR"),
        ocr_enabled=_env_bool("OCR_ENABLED", True),
        ocr_lang=_env("OCR_LANG", "eng"),
        tesseract_cmd=_env("TESSERACT_CMD"),
        ocr_dpi=_env_int("OCR_DPI", 250),
        entity_type=_env("ENTITY_TYPE", "hospital").lower(),
        batch_size=_env_int("BATCH_SIZE", 5000),
        excel_stream_threshold_mb=_env_int("EXCEL_STREAM_THRESHOLD_MB", 25),
        dashboard_host=_env("DASHBOARD_HOST", "127.0.0.1"),
        dashboard_port=_env_int("DASHBOARD_PORT", 8000),
        dashboard_token=_env("DASHBOARD_TOKEN"),
    )
    return s


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = load_settings()
    s.ensure_dirs()
    return s


def reset_settings_cache() -> None:
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Runtime configuration (editable in the dashboard)
# ---------------------------------------------------------------------------
DEFAULT_THRESHOLDS = {"very_high": 95.0, "high": 85.0, "possible": 70.0, "review": 50.0}
ENTITY_TYPES = ("hospital", "company", "person", "generic")
GROUP_BY_OPTIONS = ("state", "district", "city", "entity_type", "industry", "category", "source", "year")
EXPORT_FORMATS = ("xlsx", "csv", "docx", "pdf")
UNIVERSAL_FIELDS = (
    "name", "address", "city", "district", "state", "pincode",
    "phone", "email", "website", "registration_id", "category",
)


@dataclass
class RuntimeConfig:
    source_kind: str = "local"              # "drive" | "local"
    drive_folder: str = ""                  # Drive folder URL or ID (falls back to .env)
    local_input_dir: str = ""
    output_dir: str = ""                    # empty -> Settings.output_dir
    entity_type: str = "hospital"
    thresholds: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    auto_match_min_score: float = 85.0      # >= this (and evidence rules) => auto merge
    automatic_matching: bool = True         # False => only verified user mappings auto-apply
    manual_review_required: bool = True     # False => uncertain pairs stay separate, no review queue
    duplicate_detection: bool = True
    ocr_enabled: bool = True
    group_by: list[str] = field(default_factory=list)
    export_formats: list[str] = field(default_factory=lambda: ["xlsx", "csv", "docx", "pdf"])
    column_overrides: dict[str, str] = field(default_factory=dict)   # header (or "file::header") -> field
    dry_run_sample_size: int = 100
    ambiguity_margin: float = 5.0           # two candidates this close => review
    exact_name_no_evidence_score: float = 92.0
    include_hidden_sheets: bool = True

    # ---- validation ---------------------------------------------------------
    def validate(self) -> "RuntimeConfig":
        if self.source_kind not in ("drive", "local"):
            raise ConfigError("source_kind must be 'drive' or 'local'")
        if self.entity_type not in ENTITY_TYPES:
            raise ConfigError(f"entity_type must be one of {ENTITY_TYPES}")
        t = {k: float(v) for k, v in {**DEFAULT_THRESHOLDS, **(self.thresholds or {})}.items()}
        if not (0 <= t["review"] <= t["possible"] <= t["high"] <= t["very_high"] <= 100):
            raise ConfigError("thresholds must satisfy 0 <= review <= possible <= high <= very_high <= 100")
        self.thresholds = t
        self.auto_match_min_score = float(self.auto_match_min_score)
        if not (t["review"] <= self.auto_match_min_score <= 100):
            raise ConfigError("auto_match_min_score must be between the 'review' threshold and 100")
        bad = [g for g in self.group_by if g not in GROUP_BY_OPTIONS]
        if bad:
            raise ConfigError(f"unknown group_by option(s): {bad}; allowed: {GROUP_BY_OPTIONS}")
        badf = [f for f in self.export_formats if f not in EXPORT_FORMATS]
        if badf:
            raise ConfigError(f"unknown export format(s): {badf}; allowed: {EXPORT_FORMATS}")
        for header, fld in self.column_overrides.items():
            if fld not in UNIVERSAL_FIELDS and fld != "ignore":
                raise ConfigError(f"column override '{header}' -> unknown field '{fld}'")
        self.dry_run_sample_size = max(10, min(int(self.dry_run_sample_size), 5000))
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeConfig":
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in (data or {}).items() if k in known}
        cfg = cls(**clean)
        if "thresholds" in clean:
            cfg.thresholds = {**DEFAULT_THRESHOLDS, **clean["thresholds"]}
        return cfg.validate()


_cfg_lock = threading.RLock()


def load_runtime_config(settings: Settings | None = None) -> RuntimeConfig:
    settings = settings or get_settings()
    with _cfg_lock:
        cfg = RuntimeConfig(
            entity_type=settings.entity_type if settings.entity_type in ENTITY_TYPES else "hospital",
            ocr_enabled=settings.ocr_enabled,
            local_input_dir=settings.local_input_dir,
            drive_folder=settings.drive_folder_url or settings.drive_folder_id,
            source_kind="drive" if (settings.drive_folder_url or settings.drive_folder_id) else "local",
        )
        path = settings.runtime_config_path
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                cfg = RuntimeConfig.from_dict({**cfg.to_dict(), **data})
            except (OSError, json.JSONDecodeError, ConfigError):
                pass  # fall back to defaults; a broken file must not brick the app
        return cfg.validate()


def save_runtime_config(cfg: RuntimeConfig, settings: Settings | None = None) -> RuntimeConfig:
    settings = settings or get_settings()
    cfg.validate()
    with _cfg_lock:
        settings.runtime_config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = settings.runtime_config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(settings.runtime_config_path)
    return cfg


def effective_output_dir(cfg: RuntimeConfig, settings: Settings) -> Path:
    if cfg.output_dir:
        p = Path(cfg.output_dir).expanduser()
        return p if p.is_absolute() else (BASE_DIR / p).resolve()
    return settings.output_dir


def assert_safe_paths(input_dir: str | Path | None, output_dir: str | Path) -> None:
    """The output area must never overlap the (read-only) input folder."""
    if not input_dir:
        return
    a = Path(input_dir).expanduser().resolve()
    b = Path(output_dir).expanduser().resolve()
    if a == b or a in b.parents or b in a.parents:
        raise ConfigError(
            f"Unsafe configuration: output folder '{b}' overlaps the input folder '{a}'. "
            "The source data must stay untouched; choose a separate output folder."
        )


# ---------------------------------------------------------------------------
# JSON resource dictionaries shipped in ./config
# ---------------------------------------------------------------------------
@lru_cache(maxsize=None)
def load_resource(name: str) -> dict[str, Any]:
    path = RESOURCES_DIR / name
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


DRIVE_URL_RE = re.compile(r"(?:folders/|[?&]id=)([A-Za-z0-9_-]{10,})")


def parse_drive_folder_id(value: str) -> str:
    """Accept a Drive folder URL or a bare folder ID and return the ID."""
    value = (value or "").strip()
    if not value:
        return ""
    m = DRIVE_URL_RE.search(value)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", value):
        return value
    raise ConfigError("Could not find a Google Drive folder ID in the value provided.")
