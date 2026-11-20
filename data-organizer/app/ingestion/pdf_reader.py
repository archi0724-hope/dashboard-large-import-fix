"""PDF reader (pdfplumber): tables + text per page, OCR fallback for scanned pages.

Tables are found with ruling lines first; a text-alignment strategy is tried only when the page has no ruled
table and the result is clearly tabular. Text that lies outside the tables is returned separately so a row is
never counted twice. Pages are streamed one at a time (memory stays flat for large PDFs).
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from .base import BaseReader, Block, FileReadError, TableBlock, TextBlock
from .ocr_reader import ocr_available, ocr_image


def _clean_cell(v) -> str:
    return "" if v is None else " ".join(str(v).split())


class PdfReader(BaseReader):
    extensions = (".pdf",)

    def read(self, path: Path) -> Iterator[Block]:
        import pdfplumber

        try:
            pdf = pdfplumber.open(path)
        except Exception as exc:
            raise FileReadError(f"Cannot open PDF (corrupt or password-protected?): {type(exc).__name__}: {exc}") from exc
        scanned: list[int] = []
        ocr_done: list[int] = []
        tables_total = 0
        with pdf:
            self.meta.update(pages=len(pdf.pages))
            for page_no, page in enumerate(pdf.pages, start=1):
                try:
                    yield from self._read_page(page, page_no, scanned, ocr_done)
                except Exception as exc:       # a bad page must not lose the rest of the document
                    self.issue(f"page {page_no}", f"page could not be read: {type(exc).__name__}: {exc}", "error")
                finally:
                    try:
                        page.flush_cache()
                    except Exception:
                        pass
        self.meta.update(scanned_pages=scanned, ocr_pages=ocr_done)
        if scanned and not ocr_done and not self.options.inspect:
            self.issue("file", f"{len(scanned)} scanned page(s) had no text layer and OCR is disabled or unavailable", "warning")

    # ------------------------------------------------------------------
    def _read_page(self, page, page_no: int, scanned: list[int], ocr_done: list[int]) -> Iterator[Block]:
        tables = page.find_tables()
        method = "pdf_table"
        if not tables:
            alt = self._text_strategy_tables(page)
            if alt:
                tables, method = alt, "pdf_table_text"
        bboxes = []
        for t_idx, table in enumerate(tables, start=1):
            rows = [[_clean_cell(c) for c in r] for r in (table.extract() or [])]
            rows = [r for r in rows if any(r)]
            if not rows:
                continue
            bboxes.append(table.bbox)
            yield TableBlock(
                rows=((i, r) for i, r in enumerate(rows, start=1)),
                page=page_no, method=method, context={"table_index": t_idx},
            )
        outside = page
        for bbox in bboxes:
            outside = outside.outside_bbox(bbox)
        text = (outside.extract_text() or "").strip()
        if text:
            lines = [(i, ln, {}) for i, ln in enumerate(text.splitlines(), start=1) if ln.strip()]
            yield TextBlock(lines=lines, page=page_no, method="pdf_text")
        elif not bboxes:
            # nothing extractable on this page: scanned image?
            scanned.append(page_no)
            if self.options.inspect or not self.options.ocr:
                return
            if not ocr_available(self.options.tesseract_cmd):
                self.issue(f"page {page_no}", "scanned page but Tesseract is not installed", "warning")
                return
            img = page.to_image(resolution=self.options.ocr_dpi).original
            lines = ocr_image(img, self.options)
            ocr_done.append(page_no)
            yield TextBlock(lines=lines, page=page_no, method="pdf_ocr")

    @staticmethod
    def _text_strategy_tables(page):
        try:
            found = page.find_tables({"vertical_strategy": "text", "horizontal_strategy": "text"})
        except Exception:
            return []
        good = []
        for t in found:
            rows = [[_clean_cell(c) for c in r] for r in (t.extract() or [])]
            rows = [r for r in rows if any(r)]
            if len(rows) >= 3 and max((len(r) for r in rows), default=0) >= 3:
                filled = sum(1 for r in rows if sum(1 for c in r if c) >= 3)
                if filled / len(rows) >= 0.7:
                    good.append(t)
        return good
