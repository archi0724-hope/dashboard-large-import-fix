"""Extension -> reader mapping."""
from __future__ import annotations

from .base import BaseReader
from .csv_reader import CsvReader, TextReader
from .docx_reader import DocxReader
from .excel_reader import ExcelReader
from .json_reader import JsonReader
from .ocr_reader import ImageReader
from .pdf_reader import PdfReader

_READER_CLASSES: tuple[type[BaseReader], ...] = (
    ExcelReader, CsvReader, TextReader, DocxReader, PdfReader, ImageReader, JsonReader,
)
READERS: dict[str, type[BaseReader]] = {ext: cls for cls in _READER_CLASSES for ext in cls.extensions}
SUPPORTED_EXTENSIONS = frozenset(READERS)


def reader_for(ext: str) -> type[BaseReader] | None:
    return READERS.get(ext.lower())
