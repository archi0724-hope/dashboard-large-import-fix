"""Shared types for the ingestion layer.

Readers know only about *structure* (sheets, pages, tables, text lines). They never interpret business
meaning and never modify anything: files are opened read-only. The ``FileExtractor`` turns the blocks a
reader yields into ``RawRecord`` objects using the schema mapper.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator


class UnsupportedFileError(Exception):
    """File type (or a required external tool such as LibreOffice / Tesseract) is not available."""


class FileReadError(Exception):
    """The file is corrupt, encrypted or otherwise unreadable."""


@dataclass
class SourceFile:
    """One file discovered in the input folder (local disk or a Drive folder)."""

    file_key: str                 # stable identity (path- or Drive-id based), independent of content
    name: str
    path: str                     # display path relative to the source root (folders preserved)
    local_path: Path              # readable local copy (for Drive: the download cache, never the original)
    origin: str = "local"         # 'local' | 'drive'
    ext: str = ""
    size: int = 0
    modified: str = ""
    mime: str = ""
    fingerprint: str = ""         # sha256 (local) or md5/version (Drive); filled lazily
    drive_id: str = ""
    supported: bool = True


@dataclass
class ReadOptions:
    ocr: bool = True
    inspect: bool = False          # cheap mode: no OCR, used for the inventory report
    include_hidden_sheets: bool = True
    ocr_lang: str = "eng"
    ocr_dpi: int = 250
    tesseract_cmd: str = ""
    excel_stream_threshold_mb: int = 25
    max_rows_per_block: int | None = None
    min_pdf_text_chars: int = 25


@dataclass
class TableBlock:
    """Rows of cells; header detection happens later, in the extractor."""

    rows: Iterable[tuple[int, list[Any]]]      # (source row number, cell values)
    sheet: str | None = None
    page: int | None = None
    method: str = "table"
    context: dict[str, Any] = field(default_factory=dict)
    kind: str = "table"


@dataclass
class TextBlock:
    """Free text: (line number, text, context) triples."""

    lines: list[tuple[int, str, dict[str, Any]]]
    page: int | None = None
    sheet: str | None = None
    method: str = "text"
    context: dict[str, Any] = field(default_factory=dict)
    kind: str = "text"


Block = TableBlock | TextBlock


@dataclass
class ReadIssue:
    location: str
    message: str
    severity: str = "warning"     # 'warning' | 'error'


class BaseReader:
    """Readers yield blocks and collect structural metadata in ``meta`` and problems in ``issues``."""

    extensions: tuple[str, ...] = ()

    def __init__(self, options: ReadOptions | None = None):
        self.options = options or ReadOptions()
        self.meta: dict[str, Any] = {}
        self.issues: list[ReadIssue] = []

    def read(self, path: Path) -> Iterator[Block]:  # pragma: no cover - interface
        raise NotImplementedError

    def issue(self, location: str, message: str, severity: str = "warning") -> None:
        self.issues.append(ReadIssue(location, message, severity))


@dataclass
class RawRecord:
    """One record exactly as found in the source (the RAW / staging layer)."""

    record_id: str
    file_key: str
    file_fingerprint: str
    source_file: str
    source_path: str
    source_type: str
    source_sheet: str | None
    source_page: int | None
    source_row: int | None
    original_name: str | None
    original_address: str | None = None
    original_city: str | None = None
    original_district: str | None = None
    original_state: str | None = None
    original_pincode: str | None = None
    original_phone: str | None = None
    original_email: str | None = None
    original_website: str | None = None
    original_registration_id: str | None = None
    original_category: str | None = None
    original_data: str = "{}"
    column_map: str = "{}"
    extraction_method: str = ""

    COLUMNS = (
        "record_id", "file_key", "file_fingerprint", "source_file", "source_path", "source_type", "source_sheet",
        "source_page", "source_row", "original_name", "original_address", "original_city", "original_district",
        "original_state", "original_pincode", "original_phone", "original_email", "original_website",
        "original_registration_id", "original_category", "original_data", "column_map", "extraction_method",
    )

    def as_row(self) -> list[Any]:
        return [getattr(self, c) for c in self.COLUMNS]


def make_record_id(file_key: str, sheet: str | None, page: int | None, row: int | None, part: int = 0) -> str:
    """Deterministic ID => re-running extraction can never create duplicate raw records."""
    raw = f"{file_key}|{sheet or ''}|{page if page is not None else ''}|{row if row is not None else ''}|{part}"
    return "R" + hashlib.blake2b(raw.encode("utf-8"), digest_size=8).hexdigest()


def file_sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:              # read-only, always
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()
