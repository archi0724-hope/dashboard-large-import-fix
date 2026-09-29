"""CSV / TSV / delimited-text reader and the plain-text reader.

Nothing is dropped: ragged rows are passed on as they are (the extractor keeps the extra cells), undecodable
bytes become U+FFFD and are reported instead of aborting the file.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Iterator

from .base import BaseReader, Block, FileReadError, TableBlock, TextBlock
from .encoding import detect_delimiter, detect_encoding, read_head

try:
    csv.field_size_limit(min(sys.maxsize, 2**27))
except OverflowError:  # pragma: no cover
    csv.field_size_limit(2**27)


class CsvReader(BaseReader):
    extensions = (".csv", ".tsv")

    def read(self, path: Path) -> Iterator[Block]:
        enc, conf = detect_encoding(path)
        head = read_head(path, enc)
        delim = "\t" if path.suffix.lower() == ".tsv" else (detect_delimiter(head) or ",")
        self.meta.update(encoding=enc, encoding_confidence=round(conf, 2), delimiter=delim, sheets=1)
        yield TableBlock(rows=self._rows(path, enc, delim), sheet=None, method="csv")

    def _rows(self, path: Path, enc: str, delim: str) -> Iterator[tuple[int, list[str]]]:
        limit = self.options.max_rows_per_block
        replaced = 0
        try:
            with open(path, "r", encoding=enc, errors="replace", newline="") as fh:
                reader = csv.reader(fh, delimiter=delim)
                n = 0
                while True:
                    try:
                        row = next(reader)
                    except StopIteration:
                        break
                    except csv.Error as exc:
                        self.issue(f"row {reader.line_num}", f"malformed CSV row: {exc}")
                        continue
                    n += 1
                    if any("\ufffd" in c for c in row):
                        replaced += 1
                    yield reader.line_num, row
                    if limit and n >= limit:
                        break
        except (OSError, LookupError) as exc:
            raise FileReadError(f"Cannot read text file: {exc}") from exc
        if replaced:
            self.issue("file", f"{replaced} row(s) contain characters that could not be decoded as {enc}")


class TextReader(BaseReader):
    """``.txt`` files: delimited tables are treated like CSV, anything else as free text lines."""

    extensions = (".txt",)
    CHUNK = 2000

    def read(self, path: Path) -> Iterator[Block]:
        enc, conf = detect_encoding(path)
        head = read_head(path, enc)
        delim = detect_delimiter(head)
        self.meta.update(encoding=enc, encoding_confidence=round(conf, 2), sheets=1)
        if delim:
            self.meta["delimiter"] = delim
            helper = CsvReader(self.options)
            helper.issues = self.issues
            yield TableBlock(rows=helper._rows(path, enc, delim), method="txt_table")
            return
        buf: list[tuple[int, str, dict]] = []
        try:
            with open(path, "r", encoding=enc, errors="replace") as fh:
                for i, line in enumerate(fh, start=1):
                    buf.append((i, line.rstrip("\r\n"), {}))
                    if len(buf) >= self.CHUNK:
                        yield TextBlock(lines=buf, method="txt_lines")
                        buf = []
        except (OSError, LookupError) as exc:
            raise FileReadError(f"Cannot read text file: {exc}") from exc
        if buf:
            yield TextBlock(lines=buf, method="txt_lines")
