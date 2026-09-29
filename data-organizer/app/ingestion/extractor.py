"""Blocks -> RawRecords.

* header row detection (also below title rows), duplicate/blank header repair, header carry-over for tables
  that continue on the next PDF page, repeated header rows skipped
* headers -> universal schema through ``SchemaMapper`` (original header names preserved on every record)
* every source cell is kept in ``original_data`` (JSON); unmapped columns are never discarded
* rows that are completely blank are counted, not stored (there is nothing to store)
"""
from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterator

from ..cleaning.name_cleaner import NameCleaner
from ..cleaning.normalizer import Gazetteer, basic_clean, normalize_text, to_text
from ..config import RuntimeConfig, Settings
from ..schema_mapping import SINGLE_VALUED, ColumnMap, SchemaMapper, join_values
from .base import (
    Block, FileReadError, RawRecord, ReadOptions, SourceFile, TableBlock, TextBlock, UnsupportedFileError,
    make_record_id,
)
from .registry import reader_for
from .text_parser import parse_text_block

TITLE_WORDS = {"list", "directory", "report", "summary", "annex", "annexure", "index", "contents", "survey", "notes",
               "page", "table", "visit", "appendix", "chapter", "note"}
_FUNCTION_WORDS = {"and", "of", "the"}
_SENTENCE_END = re.compile(r"[.!?]$")
HEADER_SCAN_ROWS = 40
SAMPLE_ROWS = 60
FIELD_TO_ATTR = {
    "name": "original_name", "address": "original_address", "city": "original_city", "district": "original_district",
    "state": "original_state", "pincode": "original_pincode", "phone": "original_phone", "email": "original_email",
    "website": "original_website", "registration_id": "original_registration_id", "category": "original_category",
}


@dataclass
class BlockReport:
    sheet: str | None
    page: int | None
    method: str
    kind: str
    header_row: int | None = None
    header_source: str = ""
    columns: list[dict] = field(default_factory=list)
    mode: str = ""
    records: int = 0
    blank_rows: int = 0
    repeated_headers: int = 0
    preamble: list[str] = field(default_factory=list)
    samples: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class ExtractionReport:
    file_name: str
    records: int = 0
    blank_rows: int = 0
    sheets: int | None = None
    pages: int | None = None
    blocks: list[BlockReport] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["blocks"] = [b.to_dict() for b in self.blocks]
        return d


def _dedupe_headers(cells: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for i, c in enumerate(cells):
        h = basic_clean(c) or f"column_{i + 1}"
        key = h.lower()
        if key in seen:
            seen[key] += 1
            h = f"{h}__{seen[key]}"
        else:
            seen[key] = 1
        out.append(h)
    return out


def _looks_like_header_row(cells: list[str]) -> bool:
    filled = [c for c in cells if c.strip()]
    if len(filled) < 2:
        return False
    texty = sum(1 for c in filled if re.search(r"[^\W\d_]{2,}", c) and len(c) <= 45)
    return texty / len(filled) >= 0.8


class FileExtractor:
    def __init__(self, settings: Settings, cfg: RuntimeConfig, mapper: SchemaMapper | None = None):
        self.settings = settings
        self.cfg = cfg
        self.mapper = mapper or SchemaMapper(overrides=cfg.column_overrides)
        self.gaz = Gazetteer()
        self.cleaner = NameCleaner(entity_type=cfg.entity_type, gazetteer=self.gaz)
        self.report = ExtractionReport(file_name="")
        self._carry: tuple[list[str], ColumnMap] | None = None

    # ------------------------------------------------------------------ API
    def reader_options(self, inspect: bool = False, max_rows: int | None = None) -> ReadOptions:
        s = self.settings
        return ReadOptions(
            ocr=self.cfg.ocr_enabled and not inspect, inspect=inspect,
            include_hidden_sheets=self.cfg.include_hidden_sheets, ocr_lang=s.ocr_lang, ocr_dpi=s.ocr_dpi,
            tesseract_cmd=s.tesseract_cmd, excel_stream_threshold_mb=s.excel_stream_threshold_mb,
            max_rows_per_block=max_rows,
        )

    def extract(
        self, sf: SourceFile, fingerprint: str = "", limit: int | None = None, batch_size: int = 2000,
        inspect: bool = False,
    ) -> Iterator[list[RawRecord]]:
        """Yield batches of RawRecord. ``self.report`` is complete once the generator is exhausted."""
        cls = reader_for(sf.ext)
        self.report = ExtractionReport(file_name=sf.name)
        self._carry = None
        if cls is None:
            raise UnsupportedFileError(f"Unsupported file type '{sf.ext}'")
        opts = self.reader_options(inspect=inspect, max_rows=limit)
        reader = cls(opts)
        if hasattr(reader, "staging_dir"):
            reader.staging_dir = self.settings.staging_dir / "converted"
        source_type = sf.ext.lstrip(".")
        batch: list[RawRecord] = []
        total = 0
        try:
            for block in reader.read(sf.local_path):
                brep = BlockReport(sheet=block.sheet, page=block.page, method=block.method, kind=block.kind)
                self.report.blocks.append(brep)
                gen = self._table(block, sf, fingerprint, source_type, brep) if isinstance(block, TableBlock) \
                    else self._text(block, sf, fingerprint, source_type, brep)
                for rec in gen:
                    batch.append(rec)
                    total += 1
                    brep.records += 1
                    if len(batch) >= batch_size:
                        yield batch
                        batch = []
                    if limit and total >= limit:
                        break
                if limit and total >= limit:
                    break
        finally:
            self.report.records = total
            self.report.blank_rows = sum(b.blank_rows for b in self.report.blocks)
            self.report.meta = dict(reader.meta)
            self.report.sheets = reader.meta.get("sheets")
            self.report.pages = reader.meta.get("pages")
            self.report.issues = [{"location": i.location, "message": i.message, "severity": i.severity} for i in reader.issues]
        if batch:
            yield batch

    # ---------------------------------------------------------------- tables
    def _table(self, block: TableBlock, sf: SourceFile, fp: str, source_type: str, brep: BlockReport) -> Iterator[RawRecord]:
        it = iter(block.rows)
        head = list(itertools.islice(it, HEADER_SCAN_ROWS + SAMPLE_ROWS))
        if not head:
            return
        text_rows = [[to_text(v) for v in row] for _, row in head]
        hdr_idx = self.mapper.detect_header_row(text_rows, HEADER_SCAN_ROWS)
        headers: list[str]
        source = "detected"
        cmap: ColumnMap
        if hdr_idx is None:
            first = next((i for i, r in enumerate(text_rows) if any(c.strip() for c in r)), None)
            if first is None:
                brep.blank_rows += len(head) + sum(1 for _ in it)
                return
            if self._carry and abs(len(text_rows[first]) - len(self._carry[0])) <= 1 and not _looks_like_header_row(text_rows[first]):
                headers, cmap = self._carry
                hdr_idx, source = first - 1, "carried_from_previous_table"
            elif _looks_like_header_row(text_rows[first]):
                hdr_idx, source = first, "first_row_assumed"
                headers = None  # type: ignore[assignment]
            else:
                hdr_idx, source = first - 1, "none_generic_columns"
                headers = None  # type: ignore[assignment]
        else:
            headers = None  # type: ignore[assignment]

        data_rows = head[hdr_idx + 1:] if hdr_idx >= 0 else head
        if headers is None:
            if source == "none_generic_columns":
                width = max(len(r) for r in text_rows)
                headers = [f"column_{i + 1}" for i in range(width)]
            else:
                headers = _dedupe_headers(text_rows[hdr_idx])
            width = max([len(headers)] + [len(r) for _, r in data_rows])
            headers += [f"column_{i + 1}" for i in range(len(headers), width)]
            samples = [[to_text(v) for v in row] for _, row in data_rows[:SAMPLE_ROWS]]
            cols = [[(r[i] if i < len(r) else "") for r in samples] for i in range(len(headers))]
            cmap = self.mapper.map_columns(headers, cols, file_name=sf.name)
            self._carry = (headers, cmap) if cmap.indices("name") else self._carry
        brep.header_row = head[hdr_idx][0] if 0 <= hdr_idx < len(head) else None
        brep.header_source = source
        brep.columns = cmap.to_dict()["columns"]
        brep.preamble = [" | ".join(dict.fromkeys(c for c in r if c.strip()))[:200]
                         for r in text_rows[: max(hdr_idx, 0)] if any(c.strip() for c in r)][:10]
        hdr_norm = [normalize_text(h) for h in headers]
        colmap_json = json.dumps(self._colmap_summary(cmap), ensure_ascii=False)

        for src_row, values in itertools.chain(data_rows, it):
            cells = [to_text(v) for v in values]
            if not any(c.strip() for c in cells):
                brep.blank_rows += 1
                continue
            if [normalize_text(c) for c in cells[: len(headers)]] == hdr_norm[: len(cells)] and len(cells) >= 2:
                brep.repeated_headers += 1          # the same header repeated on a new page
                continue
            yield self._record_from_cells(headers, cells, cmap, colmap_json, sf, fp, source_type, block, src_row)
            if len(brep.samples) < 5:
                brep.samples.append({h: c for h, c in zip(headers, cells) if c.strip()})

    @staticmethod
    def _colmap_summary(cmap: ColumnMap) -> dict:
        d = cmap.to_dict()
        summary: dict[str, Any] = {k: v for k, v in d["fields"].items()}
        summary["_methods"] = {c["original"]: f'{c["field"]}:{c["method"]}' for c in d["columns"] if c["field"]}
        unmapped = [c["original"] for c in d["columns"] if not c["field"]]
        if unmapped:
            summary["_unmapped"] = unmapped
        return summary

    def _record_from_cells(self, headers, cells, cmap, colmap_json, sf, fp, source_type, block: TableBlock, src_row) -> RawRecord:
        data: dict[str, str] = {}
        for i, c in enumerate(cells):
            if c.strip():
                key = headers[i] if i < len(headers) else f"extra_{i + 1}"
                data[key] = basic_clean(c) if len(c) < 5000 else c
        for k, v in block.context.items():
            data[f"_{k}"] = str(v)
        attrs: dict[str, str | None] = {}
        for fld, attr in FIELD_TO_ATTR.items():
            idxs = cmap.indices(fld)
            vals = [cells[i] for i in idxs if i < len(cells) and cells[i].strip()]
            attrs[attr] = (vals[0].strip() if fld in SINGLE_VALUED and vals else join_values(vals, fld)) or None
        return RawRecord(
            record_id=make_record_id(sf.file_key, block.sheet, block.page, src_row,
                                     part=block.context.get("table_index", 0)),
            file_key=sf.file_key, file_fingerprint=fp, source_file=sf.name, source_path=sf.path,
            source_type=source_type, source_sheet=block.sheet, source_page=block.page, source_row=src_row,
            original_data=json.dumps(data, ensure_ascii=False), column_map=colmap_json,
            extraction_method=block.method, **attrs,
        )

    # ------------------------------------------------------------------ text
    def _text(self, block: TextBlock, sf: SourceFile, fp: str, source_type: str, brep: BlockReport) -> Iterator[RawRecord]:
        parsed, mode = parse_text_block(block.lines, self.mapper, self.gaz)
        brep.mode = mode
        brep.blank_rows = sum(1 for _, t, _ in block.lines if not t.strip())
        colmap = json.dumps({"_mode": mode}, ensure_ascii=False)
        for rec in parsed:
            attrs: dict[str, str | None] = {a: None for a in FIELD_TO_ATTR.values()}
            attrs.update({FIELD_TO_ATTR[k]: (v or None) for k, v in rec.fields.items() if k in FIELD_TO_ATTR})
            role = self._text_role(rec, mode)
            data = dict(rec.data)
            data["text_role"] = role
            if role != "entity" and attrs["original_name"]:
                data["rejected_name_candidate"] = attrs["original_name"]   # kept for audit, not used as an entity
                attrs["original_name"] = None
            yield RawRecord(
                record_id=make_record_id(sf.file_key, block.sheet, block.page, rec.row, part=0),
                file_key=sf.file_key, file_fingerprint=fp, source_file=sf.name, source_path=sf.path,
                source_type=source_type, source_sheet=block.sheet, source_page=block.page, source_row=rec.row,
                original_data=json.dumps(data, ensure_ascii=False), column_map=colmap,
                extraction_method=block.method, **attrs,
            )

    # ------------------------------------------------------- text line role
    def _text_role(self, rec, mode: str) -> str:
        """entity | heading | prose | title_or_note.  Only 'entity' lines contribute a name to matching."""
        if mode == "label":
            return "entity"
        text = rec.data.get("text", "")
        name = rec.fields.get("name")
        # an address alone is weak evidence (a dash in a title also produces one)
        other = any(k in rec.fields for k in ("city", "state", "pincode", "phone", "email", "website", "registration_id"))
        words = re.findall(r"\w+", text)
        low = {w.lower() for w in words}
        if rec.data.get("is_heading") == "true" and not other:
            return "heading"
        if len(words) >= 14 or (len(words) >= 6 and _SENTENCE_END.search(text.strip()) and not other):
            return "prose"
        if not name:
            return "title_or_note"
        if not other:
            if low & TITLE_WORDS:
                return "title_or_note"
            forms = self.cleaner.clean(name)
            keywords = set(forms.tokens) & (self.cleaner.generic - _FUNCTION_WORDS)
            if not keywords and not self.cleaner.type_hint(forms.normalized):
                if self.cfg.entity_type in ("generic", "person") and len(words) <= 6:
                    return "entity"
                return "title_or_note"
        return "entity"
