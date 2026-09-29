"""Central logging.

Creates the log files requested in the spec::

    logs/application.log   general lifecycle messages
    logs/extraction.log    file reading / extraction details
    logs/matching.log      entity-resolution decisions
    logs/errors.log        every error (file, page/sheet/row, error, timestamp)
    logs/audit.log         user decisions, configuration changes, exports

Secrets are never logged: callers must not pass credentials, and ``redact`` is applied
to every message as a second line of defence.
"""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import get_settings

LOG_NAMES = ("application", "extraction", "matching", "errors", "audit")
_FMT = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|authorization|bearer)(['\"\s:=]+)([A-Za-z0-9._\-~+/]{8,})"
)
_configured_dir: Path | None = None


def redact(text: str) -> str:
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***REDACTED***", text)


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # pragma: no cover - trivial
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


def setup_logging(log_dir: Path | None = None, level: int = logging.INFO, console: bool = True) -> None:
    global _configured_dir
    log_dir = Path(log_dir or get_settings().log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    if _configured_dir == log_dir:
        return
    for name in LOG_NAMES:
        lg = logging.getLogger(f"organizer.{name}")
        lg.setLevel(level)
        lg.propagate = False
        for h in list(lg.handlers):
            lg.removeHandler(h)
            h.close()
        fh = RotatingFileHandler(log_dir / f"{name}.log", maxBytes=10_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(_FMT)
        fh.addFilter(_RedactFilter())
        lg.addHandler(fh)
        if console and name in ("application", "errors"):
            ch = logging.StreamHandler()
            ch.setFormatter(_FMT)
            ch.addFilter(_RedactFilter())
            lg.addHandler(ch)
    _configured_dir = log_dir


def get_logger(name: str) -> logging.Logger:
    if name not in LOG_NAMES:
        raise ValueError(f"unknown logger {name!r}; expected one of {LOG_NAMES}")
    lg = logging.getLogger(f"organizer.{name}")
    if not lg.handlers:
        setup_logging()
    return lg


def log_error(file: str, location: str, error: str | BaseException, stage: str = "") -> None:
    """Uniform error line: file | page/sheet/row | error | (timestamp added by formatter)."""
    msg = f"file={file!r} | location={location or '-'} | stage={stage or '-'} | error={error}"
    get_logger("errors").error(msg)


def audit(event: str, **details) -> None:
    kv = " ".join(f"{k}={v!r}" for k, v in details.items())
    get_logger("audit").info(f"{event} {kv}".strip())
