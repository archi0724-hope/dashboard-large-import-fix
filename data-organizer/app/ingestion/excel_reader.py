"""Excel readers (.xlsx / .xlsm via openpyxl, legacy .xls via xlrd).

* every sheet is read; the sheet name is preserved on every row
* merged cells are expanded (the merged value is repeated) when the workbook is small enough to be loaded
  with merge information; very large workbooks are streamed (constant memory) and only read values
* hidden sheets are read too unless disabled (they still contain data)
* the file is opened read-only and never saved
"""
from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterator

from .base import BaseReader, Block, FileReadError, TableBlock


def _cell_value(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, (date, time)):
        return v.isoformat()
    return v


class ExcelReader(BaseReader):
    extensions = (".xlsx", ".xlsm", ".xls")

    def read(self, path: Path) -> Iterator[Block]:
        if path.suffix.lower() == ".xls":
            yield from self._read_xls(path)
        else:
            yield from self._read_xlsx(path)

    # ------------------------------------------------------------------ xlsx
    def _read_xlsx(self, path: Path) -> Iterator[Block]:
        import openpyxl
        from zipfile import BadZipFile

        size_mb = path.stat().st_size / 1_048_576
        streaming = size_mb > self.options.excel_stream_threshold_mb
        try:
            wb = openpyxl.load_workbook(path, read_only=streaming, data_only=True, keep_links=False)
        except (BadZipFile, KeyError, OSError, ValueError) as exc:
            raise FileReadError(f"Cannot open workbook (corrupt, encrypted or not a real .xlsx): {exc}") from exc
        except Exception as exc:  # openpyxl raises several custom exceptions
            raise FileReadError(f"Cannot open workbook: {type(exc).__name__}: {exc}") from exc
        self.meta.update(sheets=len(wb.worksheets), streamed=streaming, sheet_names=[ws.title for ws in wb.worksheets])
        if streaming:
            self.issue("workbook", f"{size_mb:.0f} MB workbook read in streaming mode: merged cells are not expanded")
        try:
            for ws in wb.worksheets:
                hidden = ws.sheet_state != "visible"
                if hidden and not self.options.include_hidden_sheets:
                    self.issue(ws.title, "hidden sheet skipped (disabled in settings)")
                    continue
                try:
                    merged = {} if streaming else self._merged_lookup(ws)
                    yield TableBlock(
                        rows=self._iter_rows(ws, merged),
                        sheet=ws.title,
                        method="excel",
                        context={"hidden_sheet": hidden} if hidden else {},
                    )
                except Exception as exc:  # one broken sheet must not stop the others
                    self.issue(ws.title, f"sheet could not be read: {exc}", "error")
        finally:
            wb.close()

    @staticmethod
    def _merged_lookup(ws) -> dict[int, dict[int, Any]]:
        """row -> {col: value} for every non-anchor cell of a merged range (fill-down / fill-right)."""
        lookup: dict[int, dict[int, Any]] = {}
        for rng in list(ws.merged_cells.ranges):
            anchor = ws.cell(rng.min_row, rng.min_col).value
            if anchor is None:
                continue
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    if (r, c) != (rng.min_row, rng.min_col):
                        lookup.setdefault(r, {})[c] = anchor
        return lookup

    def _iter_rows(self, ws, merged: dict[int, dict[int, Any]]) -> Iterator[tuple[int, list[Any]]]:
        limit = self.options.max_rows_per_block
        for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
            vals = [_cell_value(v) for v in row]
            fill = merged.get(i)
            if fill:
                need = max(fill) 
                if len(vals) < need:
                    vals.extend([None] * (need - len(vals)))
                for col, v in fill.items():
                    if vals[col - 1] in (None, ""):
                        vals[col - 1] = _cell_value(v)
            yield i, vals
            if limit and i >= limit:
                return

    # ------------------------------------------------------------------- xls
    def _read_xls(self, path: Path) -> Iterator[Block]:
        import xlrd

        try:
            try:
                book = xlrd.open_workbook(str(path), formatting_info=True, on_demand=True)
            except NotImplementedError:
                book = xlrd.open_workbook(str(path), on_demand=True)
        except Exception as exc:
            raise FileReadError(f"Cannot open legacy .xls workbook: {type(exc).__name__}: {exc}") from exc
        self.meta.update(sheets=book.nsheets, sheet_names=book.sheet_names())
        try:
            for idx in range(book.nsheets):
                sh = book.sheet_by_index(idx)
                hidden = getattr(sh, "visibility", 0) != 0
                if hidden and not self.options.include_hidden_sheets:
                    self.issue(sh.name, "hidden sheet skipped (disabled in settings)")
                    continue
                try:
                    yield TableBlock(
                        rows=self._iter_xls_rows(book, sh),
                        sheet=sh.name,
                        method="excel",
                        context={"hidden_sheet": True} if hidden else {},
                    )
                except Exception as exc:
                    self.issue(sh.name, f"sheet could not be read: {exc}", "error")
        finally:
            book.release_resources()

    def _iter_xls_rows(self, book, sh) -> Iterator[tuple[int, list[Any]]]:
        import xlrd

        fill: dict[tuple[int, int], Any] = {}
        for rlo, rhi, clo, chi in getattr(sh, "merged_cells", []) or []:
            anchor = sh.cell_value(rlo, clo)
            for r in range(rlo, rhi):
                for c in range(clo, chi):
                    if (r, c) != (rlo, clo):
                        fill[(r, c)] = anchor
        limit = self.options.max_rows_per_block
        for r in range(sh.nrows):
            vals: list[Any] = []
            for c in range(sh.ncols):
                ctype = sh.cell_type(r, c)
                v = sh.cell_value(r, c)
                if ctype == xlrd.XL_CELL_DATE:
                    try:
                        v = xlrd.xldate_as_datetime(v, book.datemode).isoformat(sep=" ")
                    except Exception:
                        pass
                elif ctype == xlrd.XL_CELL_ERROR:
                    v = None
                elif ctype == xlrd.XL_CELL_BOOLEAN:
                    v = bool(v)
                if (v in ("", None)) and (r, c) in fill:
                    v = fill[(r, c)]
                vals.append(v)
            yield r + 1, vals
            if limit and r + 1 >= limit:
                return
