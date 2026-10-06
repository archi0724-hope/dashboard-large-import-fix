"""Output layer: master data, name mapping, aliases, duplicates, review queue, invalid records, errors, report.

* xlsx (formatted, filterable, frozen header) / csv (UTF-8 with BOM for Excel) / Word / PDF, as configured
* files are written to a temp name and renamed, so a half-written file is never left behind
* text cells are never interpreted as formulas (formula-injection safe); CSV cells starting with ``=``/``@`` are quoted
* a sheet larger than Excel's row limit falls back to CSV and says so in the manifest
* optional grouped copies (by state / district / city / entity type / category / source / year) in sub-folders
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ..config import ConfigError, RuntimeConfig, Settings, assert_safe_paths, effective_output_dir
from ..database.repository import Repository
from ..logging_setup import audit, get_logger

log = get_logger("application")
EXCEL_MAX_ROWS = 1_048_000
STATUS_LABEL = {"auto": "Auto-matched", "verified": "Verified by user", "provisional": "Pending review"}
ENTITY_LEVEL_KEYS = {"state": "State", "district": "District", "city": "City", "entity_type": "Entity Type", "industry": "Category",
                     "category": "Category"}


def _safe_name(value: Any) -> str:
    s = re.sub(r"[^\w\- ]+", "_", str(value or "").strip()).strip(" _.")
    return (s or "Unknown")[:60]


def _csv_safe(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in out.columns:
        if out[c].dtype == object:
            out[c] = out[c].map(lambda v: "'" + v if isinstance(v, str) and v[:1] in ("=", "@") else v)
    return out


def _json_list(v: Any) -> str:
    try:
        return "; ".join(json.loads(v)) if v else ""
    except (TypeError, ValueError):
        return str(v or "")


class Exporter:
    def __init__(self, settings: Settings, cfg: RuntimeConfig, repo: Repository):
        self.settings, self.cfg, self.repo = settings, cfg, repo
        self.out = effective_output_dir(cfg, settings)
        src = cfg.local_input_dir or settings.local_input_dir
        if cfg.source_kind == "local" and src:
            assert_safe_paths(src, self.out)

    # ------------------------------------------------------------------ frames
    def frames(self) -> dict[str, pd.DataFrame]:
        q = self.repo.fetchdf
        master = q(
            "SELECT master_entity_id AS \"Master Entity ID\", standard_name AS \"Standard Name\", aliases AS \"Aliases\", city AS \"City\", "
            "district AS \"District\", state AS \"State\", address AS \"Address\", pincode AS \"PIN Code\", phone AS \"Phone\", "
            "email AS \"Email\", website AS \"Website\", registration_id AS \"Registration ID\", entity_type AS \"Entity Type\", "
            "category AS \"Category\", record_count AS \"Records\", source_file_count AS \"Source Files\", confidence AS \"Confidence\", "
            "verification_status AS \"Status\", data_conflicts AS \"Data Conflicts\", all_phones AS \"All Phones\", all_emails AS \"All Emails\" "
            "FROM master_entities WHERE merged_into IS NULL ORDER BY state, city, standard_name")
        for c in ("Aliases", "All Phones", "All Emails"):
            master[c] = master[c].map(_json_list)
        master["Status"] = master["Status"].map(lambda s: STATUS_LABEL.get(s, s))

        mapping = q(
            "SELECT r.record_id AS \"Record ID\", r.source_file AS \"Source File\", r.source_path AS \"Source Path\", "
            "coalesce(r.source_sheet, '') AS \"Sheet\", r.source_page AS \"Page\", r.source_row AS \"Row\", "
            "r.original_name AS \"Original Name\", c.name_normalized AS \"Normalized Name\", e.standard_name AS \"Standard Name\", "
            "m.master_entity_id AS \"Master Entity ID\", m.match_score AS \"Confidence\", m.match_band AS \"Band\", "
            "m.match_method AS \"Method\", m.match_status AS \"Status\", m.reason AS \"Reason\", m.verified_by_user AS \"Verified By User\", "
            "r.original_address AS \"Original Address\", r.original_city AS \"Original City\", r.original_state AS \"Original State\", "
            "r.original_phone AS \"Original Phone\", r.original_email AS \"Original Email\", "
            "r.original_registration_id AS \"Original Registration ID\", c.quality_severity AS \"Data Quality\", "
            "e.state AS \"State\", e.district AS \"District\", e.city AS \"City\", e.entity_type AS \"Entity Type\", "
            "e.category AS \"Category\", c.source_year AS \"Source Year\" "
            "FROM raw_records r LEFT JOIN cleaned_records c USING (record_id) LEFT JOIN resolutions m USING (record_id) "
            "LEFT JOIN master_entities e ON e.master_entity_id = m.master_entity_id WHERE r.is_current ORDER BY r.seq")

        aliases = q(
            "SELECT e.standard_name AS \"Standard Name\", a.alias AS \"Alias (as found)\", a.alias_normalized AS \"Alias (normalized)\", "
            "a.master_entity_id AS \"Master Entity ID\", a.occurrences AS \"Occurrences\", a.match_score AS \"Match Score\", "
            "a.match_method AS \"Method\", a.verified AS \"Verified\", a.original_source AS \"First Seen In\" "
            "FROM aliases a JOIN master_entities e ON e.master_entity_id = a.master_entity_id WHERE e.merged_into IS NULL "
            "ORDER BY e.standard_name, a.occurrences DESC")

        dups = q(
            "SELECT g.group_id AS \"Group\", g.dup_type AS \"Type\", g.dup_key AS \"Matching Key\", g.similarity AS \"Similarity\", "
            "g.is_suggested_primary AS \"Suggested Primary\", g.record_id AS \"Record ID\", r.source_file AS \"Source File\", "
            "r.source_row AS \"Row\", r.original_name AS \"Original Name\", g.master_entity_id AS \"Master Entity ID\", "
            "e.standard_name AS \"Standard Name\" FROM duplicate_groups g LEFT JOIN raw_records r USING (record_id) "
            "LEFT JOIN master_entities e ON e.master_entity_id = g.master_entity_id ORDER BY g.group_id, g.is_suggested_primary DESC")

        review = q(
            "SELECT v.review_id AS \"Review ID\", v.original_name AS \"Original Name\", v.context_city AS \"City Context\", "
            "v.candidate_master_id AS \"Suggested Master ID\", e.standard_name AS \"Suggested Standard Name\", v.match_score AS \"Confidence\", "
            "v.match_band AS \"Band\", v.reason AS \"Reason\", v.record_count AS \"Records\", v.source_file AS \"Source File\", "
            "v.status AS \"Status\" FROM review_items v "
            "LEFT JOIN master_entities e ON e.master_entity_id = v.candidate_master_id WHERE v.status = 'pending' "
            "ORDER BY v.record_count DESC, v.match_score DESC")

        invalid = q(
            "SELECT r.record_id AS \"Record ID\", r.source_file AS \"Source File\", coalesce(r.source_sheet, '') AS \"Sheet\", "
            "r.source_page AS \"Page\", r.source_row AS \"Row\", r.original_name AS \"Original Name\", c.quality_severity AS \"Severity\", "
            "string_agg(DISTINCT qi.message, ' | ') AS \"Problem\", r.original_data AS \"Original Data\" "
            "FROM cleaned_records c JOIN raw_records r USING (record_id) LEFT JOIN quality_issues qi USING (record_id) "
            "WHERE r.is_current AND (c.quality_severity = 'error' OR NOT c.is_resolvable) "
            "GROUP BY r.record_id, r.source_file, r.source_sheet, r.source_page, r.source_row, r.original_name, c.quality_severity, "
            "r.original_data, r.seq ORDER BY r.seq")

        quality = q(
            "SELECT qi.record_id AS \"Record ID\", qi.master_entity_id AS \"Master Entity ID\", r.source_file AS \"Source File\", "
            "r.source_row AS \"Row\", r.original_name AS \"Original Name\", qi.issue_type AS \"Issue\", qi.severity AS \"Severity\", "
            "qi.field AS \"Field\", qi.value AS \"Value\", qi.message AS \"Message\" FROM quality_issues qi "
            "LEFT JOIN raw_records r USING (record_id) ORDER BY qi.severity, qi.issue_type, r.source_file, r.source_row")

        errors = q(
            "SELECT occurred_at AS \"When\", file_name AS \"File\", location AS \"Location\", stage AS \"Stage\", severity AS \"Severity\", "
            "error_type AS \"Type\", message AS \"Message\" FROM processing_errors ORDER BY occurred_at")
        return {"master_data": master, "name_mapping": mapping, "aliases": aliases, "duplicates": dups,
                "needs_manual_review": review, "invalid_records": invalid, "data_quality_issues": quality, "processing_errors": errors}

    def report_frames(self, manifest: list[dict]) -> dict[str, pd.DataFrame]:
        r, q = self.repo, self.repo.fetchdf
        t = {
            "Files found": r.scalar("SELECT count(*) FROM source_files"),
            "Files processed": r.scalar("SELECT count(*) FROM source_files WHERE status = 'processed'"),
            "Files failed": r.scalar("SELECT count(*) FROM source_files WHERE status = 'failed'"),
            "Files unsupported": r.scalar("SELECT count(*) FROM source_files WHERE status = 'unsupported'"),
            "Duplicate files skipped": r.scalar("SELECT count(*) FROM source_files WHERE status = 'duplicate_file'"),
            "Records extracted": r.scalar("SELECT count(*) FROM raw_records WHERE is_current"),
            "Records with a name": r.scalar("SELECT count(*) FROM cleaned_records c JOIN raw_records r USING (record_id) WHERE r.is_current AND c.is_resolvable"),
            "Master entities": r.scalar("SELECT count(*) FROM master_entities WHERE merged_into IS NULL"),
            "Auto-matched records": r.scalar("SELECT count(*) FROM resolutions WHERE match_status IN ('auto_matched', 'verified_mapping')"),
            "Records confirmed by user": r.scalar("SELECT count(*) FROM resolutions WHERE verified_by_user"),
            "Pending manual reviews": r.scalar("SELECT count(*) FROM review_items WHERE status = 'pending'"),
            "Duplicate groups": r.scalar("SELECT count(DISTINCT group_id) FROM duplicate_groups"),
            "Data-quality issues": r.scalar("SELECT count(*) FROM quality_issues"),
            "Processing errors": r.scalar("SELECT count(*) FROM processing_errors WHERE severity = 'error'"),
            "Generated at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        summary = pd.DataFrame({"Metric": list(t), "Value": [str(v) if isinstance(v, str) else int(v) if not isinstance(v, datetime) else str(v) for v in t.values()]})
        files = q("SELECT source_path AS \"File\", ext AS \"Type\", size_bytes AS \"Size (bytes)\", status AS \"Status\", records_extracted AS \"Records\", "
                  "sheets AS \"Sheets\", pages AS \"Pages\", notes AS \"Notes\", error AS \"Error\" FROM source_files ORDER BY source_path")
        cols = []
        for (path, insp) in r.fetchall("SELECT source_path, inspection FROM source_files WHERE inspection IS NOT NULL"):
            try:
                rep = json.loads(insp)
            except ValueError:
                continue
            for b in rep.get("blocks", []):
                for c in b.get("columns", []):
                    cols.append({"File": path, "Sheet / Page": b.get("sheet") or (f"page {b['page']}" if b.get("page") else ""),
                                 "Header Row": b.get("header_row"), "Original Column": c.get("original"),
                                 "Mapped To": c.get("field") or "(kept, not mapped)", "How": c.get("method"), "Confidence": c.get("confidence")})
        colmap = pd.DataFrame(cols, columns=["File", "Sheet / Page", "Header Row", "Original Column", "Mapped To", "How", "Confidence"])
        bands = q("SELECT coalesce(nullif(match_band, ''), '-') AS \"Band\", match_status AS \"Status\", count(*) AS \"Records\" "
                  "FROM resolutions GROUP BY 1, 2 ORDER BY 3 DESC")
        issues = q("SELECT issue_type AS \"Issue\", severity AS \"Severity\", count(*) AS \"Count\" FROM quality_issues GROUP BY 1, 2 ORDER BY 3 DESC")
        conf = pd.DataFrame({"Setting": ["Entity type", "Auto-match minimum score", "Automatic matching", "Manual review required",
                                         "Duplicate detection", "Thresholds (very high / high / possible / review)", "OCR",
                                         "Export formats", "Group by"],
                             "Value": [self.cfg.entity_type, self.cfg.auto_match_min_score, self.cfg.automatic_matching, self.cfg.manual_review_required,
                                       self.cfg.duplicate_detection, " / ".join(str(self.cfg.thresholds[k]) for k in ("very_high", "high", "possible", "review")),
                                       self.cfg.ocr_enabled, ", ".join(self.cfg.export_formats), ", ".join(self.cfg.group_by) or "-"]})
        conf["Value"] = conf["Value"].astype(str)
        return {"Summary": summary, "Files": files, "Column mapping": colmap, "Match bands": bands, "Data quality": issues,
                "Configuration": conf, "Output files": pd.DataFrame(manifest)}

    # ------------------------------------------------------------------ writing
    def export_all(self) -> list[dict]:
        self.out.mkdir(parents=True, exist_ok=True)
        manifest: list[dict] = []
        frames = self.frames()
        for name, df in frames.items():
            for fmt in self.cfg.export_formats:
                manifest.append(self._write(df, self.out / name, fmt))
        if self.cfg.group_by:
            manifest += self._grouped(frames)
        report = self.report_frames(manifest)
        manifest.append(self._write_report(report))
        audit("export", folder=str(self.out), files=len(manifest))
        return manifest

    def _write(self, df: pd.DataFrame, base: Path, fmt: str) -> dict:
        base.parent.mkdir(parents=True, exist_ok=True)
        note = ""
        if fmt == "xlsx" and len(df) > EXCEL_MAX_ROWS:
            fmt, note = "csv", f"{len(df):,} rows exceed Excel's limit - written as CSV instead"
        path = base.with_suffix("." + fmt)
        tmp = path.with_name(path.name + ".tmp")
        if fmt == "xlsx":
            self._xlsx({"Data": df}, tmp)
        elif fmt == "csv":
            _csv_safe(df).to_csv(tmp, index=False, encoding="utf-8-sig")
        elif fmt == "docx":
            self._docx(df, tmp, path.stem)
        elif fmt == "pdf":
            self._pdf(df, tmp, path.stem)
        else:
            raise ConfigError(f"Unknown export format '{fmt}'")
        tmp.replace(path)
        return {"file": str(path.relative_to(self.out)), "rows": int(len(df)), "format": fmt, "bytes": path.stat().st_size, "note": note}

    @staticmethod
    def _docx(df: pd.DataFrame, path: Path, title: str) -> None:
        """Write a portable Word table without changing any source values."""
        from docx import Document

        document = Document()
        document.add_heading(title.replace("_", " ").title(), level=1)
        document.add_paragraph(f"Records: {len(df):,}")
        table = document.add_table(rows=1, cols=len(df.columns))
        table.style = "Table Grid"
        for cell, column in zip(table.rows[0].cells, df.columns):
            cell.text = str(column)
        for row in df.itertuples(index=False, name=None):
            cells = table.add_row().cells
            for cell, value in zip(cells, row):
                cell.text = "" if pd.isna(value) else str(value)
        document.save(path)

    @staticmethod
    def _pdf(df: pd.DataFrame, path: Path, title: str) -> None:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import landscape, letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

        styles = getSampleStyleSheet()
        style = styles["BodyText"]
        style.fontSize = 6
        style.leading = 7
        heading = styles["Heading1"]
        heading.fontSize = 14
        heading.leading = 16
        columns = [str(c) for c in df.columns]
        data = [[Paragraph(c, style) for c in columns]]
        for row in df.itertuples(index=False, name=None):
            data.append([Paragraph(str(value if value is not None else ""), style) for value in row])
        doc = SimpleDocTemplate(str(path), pagesize=landscape(letter), rightMargin=0.25 * inch, leftMargin=0.25 * inch,
                                topMargin=0.3 * inch, bottomMargin=0.3 * inch)
        table = LongTable(data, repeatRows=1, splitByRow=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3A5F")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#B8C0CC")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F6F8")]),
        ]))
        doc.build([Paragraph(title.replace("_", " "), heading), Spacer(1, 8), table])

    def _write_report(self, sheets: dict[str, pd.DataFrame]) -> dict:
        path = self.out / "processing_report.xlsx"
        tmp = path.with_name(path.name + ".tmp")
        self._xlsx(sheets, tmp)
        tmp.replace(path)
        return {"file": path.name, "rows": int(sum(len(d) for d in sheets.values())), "format": "xlsx", "bytes": path.stat().st_size, "note": ""}

    @staticmethod
    def _xlsx(sheets: dict[str, pd.DataFrame], path: Path) -> None:
        with pd.ExcelWriter(path, engine="xlsxwriter", engine_kwargs={"options": {"strings_to_formulas": False, "strings_to_urls": False,
                                                                                   "nan_inf_to_errors": True}}) as xw:
            book = xw.book
            head = book.add_format({"bold": True, "bg_color": "#1F3A5F", "font_color": "#FFFFFF", "border": 1, "valign": "top", "text_wrap": True})
            for name, df in sheets.items():
                df = df.copy()
                for c in df.columns:
                    if str(df[c].dtype).startswith("datetime"):
                        df[c] = df[c].astype(str)
                sheet = name[:31]
                df.to_excel(xw, sheet_name=sheet, index=False, header=False, startrow=1)
                ws = xw.sheets[sheet]
                for i, col in enumerate(df.columns):
                    ws.write(0, i, col, head)
                    sample = df[col].head(500).astype(str)
                    width = min(60, max(len(str(col)) + 2, int(sample.str.len().quantile(0.95)) + 2 if len(sample) else 10))
                    ws.set_column(i, i, max(8, width))
                ws.freeze_panes(1, 0)
                if len(df.columns):
                    ws.autofilter(0, 0, max(len(df), 1), len(df.columns) - 1)

    def _grouped(self, frames: dict[str, pd.DataFrame]) -> list[dict]:
        manifest: list[dict] = []
        keys = list(self.cfg.group_by)
        targets = {"master_data": frames["master_data"], "name_mapping": frames["name_mapping"]}
        for name, df in targets.items():
            cols: list[str] = []
            for k in keys:
                if k in ENTITY_LEVEL_KEYS and ENTITY_LEVEL_KEYS[k] in df.columns:
                    cols.append(ENTITY_LEVEL_KEYS[k])
                elif name == "name_mapping" and k == "source":
                    cols.append("Source File")
                elif name == "name_mapping" and k == "year":
                    cols.append("Source Year")
            cols = list(dict.fromkeys(cols))
            if not cols:
                continue
            groups = df.fillna({c: "" for c in cols}).groupby(cols, sort=True)
            if groups.ngroups > 2000:
                log.warning(f"grouping {name} by {cols} would create {groups.ngroups} folders - skipped")
                manifest.append({"file": f"by_group/{name}", "rows": 0, "format": "-", "bytes": 0,
                                 "note": f"skipped: {groups.ngroups} groups is too many (choose coarser grouping)"})
                continue
            for values, part in groups:
                values = values if isinstance(values, tuple) else (values,)
                folder = Path("by_group").joinpath(*[_safe_name(v if str(v) != "nan" else "") for v in values])
                for fmt in self.cfg.export_formats:
                    manifest.append(self._write(part, self.out / folder / name, fmt))
        return manifest
