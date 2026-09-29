"""Word documents: paragraphs (with headings) and tables, in document order.

Legacy ``.doc`` files are converted to ``.docx`` by LibreOffice into the staging folder (a copy - the original
is never touched). Vertically merged cells repeat the merged value; horizontally merged cells are not duplicated.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator

from .base import BaseReader, Block, FileReadError, TableBlock, TextBlock, UnsupportedFileError


def convert_doc_to_docx(path: Path, out_dir: Path) -> Path:
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        raise UnsupportedFileError("Legacy .doc needs LibreOffice (soffice) to convert it; install it or save the file as .docx")
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as profile:
        cmd = [exe, f"-env:UserInstallation=file://{profile}", "--headless", "--convert-to", "docx", "--outdir", str(out_dir), str(path)]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=180)
        except (subprocess.SubprocessError, OSError) as exc:
            raise FileReadError(f"LibreOffice could not convert the .doc file: {exc}") from exc
    out = out_dir / (path.stem + ".docx")
    if not out.exists():
        raise FileReadError("LibreOffice produced no output for the .doc file")
    return out


class DocxReader(BaseReader):
    extensions = (".docx", ".doc")

    def __init__(self, options=None, staging_dir: Path | None = None):
        super().__init__(options)
        self.staging_dir = staging_dir or Path(tempfile.gettempdir()) / "data-organizer-converted"

    def read(self, path: Path) -> Iterator[Block]:
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        src = path
        if path.suffix.lower() == ".doc":
            src = convert_doc_to_docx(path, self.staging_dir)
            self.meta["converted_from"] = ".doc"
        try:
            document = docx.Document(str(src))
        except Exception as exc:
            raise FileReadError(f"Cannot open Word document: {type(exc).__name__}: {exc}") from exc

        props = document.core_properties
        self.meta.update(
            title=props.title or "", author=props.author or "",
            created=props.created.isoformat() if props.created else "",
            modified=props.modified.isoformat() if props.modified else "",
            tables=len(document.tables), sheets=1,
        )

        lines: list[tuple[int, str, dict]] = []
        heading = ""
        table_index = 0
        for idx, child in enumerate(document.element.body.iterchildren(), start=1):
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                para = Paragraph(child, document)
                text = para.text.strip()
                if not text:
                    continue
                style = (para.style.name if para.style is not None else "") or ""
                is_heading = style.lower().startswith(("heading", "title"))
                ctx = {"style": style} if style and style != "Normal" else {}
                if is_heading:
                    heading = text
                    ctx["is_heading"] = True
                elif heading:
                    ctx["heading"] = heading
                lines.append((idx, text, ctx))
            elif tag == "tbl":
                if lines:
                    yield TextBlock(lines=lines, method="docx_text")
                    lines = []
                table_index += 1
                table = Table(child, document)
                yield TableBlock(
                    rows=self._table_rows(table),
                    method="docx_table",
                    context={"table_index": table_index, **({"heading": heading} if heading else {})},
                )
        if lines:
            yield TextBlock(lines=lines, method="docx_text")

    @staticmethod
    def _table_rows(table) -> Iterator[tuple[int, list[str]]]:
        for r_idx, row in enumerate(table.rows, start=1):
            seen: set[int] = set()
            vals: list[str] = []
            for cell in row.cells:
                key = id(cell._tc)
                if key in seen:          # horizontally merged: keep the value once
                    vals.append("")
                    continue
                seen.add(key)
                vals.append(" ".join(cell.text.split()))
            yield r_idx, vals
