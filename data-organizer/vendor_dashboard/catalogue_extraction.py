"""Extract catalogue and price-list rows from vendor documents."""
from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO
from pathlib import Path

import pandas as pd

HEADER_ALIASES = {
    "product_name": ("product", "item", "description", "name", "particular"),
    "sku": ("sku", "code", "model", "item code", "product code"),
    "price": ("price", "rate", "mrp", "amount", "cost"),
    "mrp": ("mrp", "maximum retail", "retail price"),
    "unit": ("unit", "uom", "pack", "per"),
    "brand": ("brand", "make", "manufacturer"),
    "category": ("category", "group", "type", "class"),
    "specification": ("specification", "spec", "features", "details"),
    "pack_size": ("pack size", "packing", "pack", "package"),
    "moq": ("moq", "minimum order", "min order"),
    "gst": ("gst", "tax", "igst", "cgst", "sgst"),
    "availability": ("availability", "stock", "available"),
    "discount": ("discount", "disc"),
}


def _text(value) -> str:
    if isinstance(value, pd.Series):
        value = next((item for item in value.tolist() if _text(item)), "")
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return ""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _unique_headers(values) -> list[str]:
    seen = {}
    headers = []
    for index, value in enumerate(values, 1):
        base = _text(value) or f"column_{index}"
        seen[base] = seen.get(base, 0) + 1
        headers.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return headers


def _kind(filename: str, text: str) -> str | None:
    value = f"{Path(filename).stem} {text}".casefold()
    has_price = bool(re.search(r"\b(?:price|pricing|rate|mrp|amount|cost)\b", value))
    has_catalogue = bool(re.search(r"\b(?:catalogue|catalog|product list|product)\b", value))
    return "price" if has_price else "catalogue" if has_catalogue else None


def _header_row(frame: pd.DataFrame) -> int:
    best = (0, -1)
    for index in range(min(len(frame), 12)):
        values = [_text(value).casefold() for value in frame.iloc[index].tolist()]
        score = sum(any(alias in value for alias in aliases) for value in values for aliases in HEADER_ALIASES.values())
        if score > best[1]:
            best = (index, score)
    return best[0] if best[1] >= 1 else 0


def _frame_records(frame: pd.DataFrame, filename: str, sheet: str, kind: str, page: str = "") -> list[dict]:
    frame = frame.dropna(how="all").dropna(axis=1, how="all")
    if frame.empty:
        return []
    header = _header_row(frame)
    frame = frame.iloc[header:].copy()
    headers = _unique_headers(frame.iloc[0].tolist())
    frame = frame.iloc[1:].copy()
    frame.columns = headers
    columns = {column.casefold(): column for column in frame.columns}

    def find_column(names):
        for column in frame.columns:
            normalized = column.casefold()
            if any(alias in normalized for alias in names):
                return column
        return None

    product_column = next(
        (column for column in frame.columns
         if re.search(r"\b(?:product|item)\s*(?:name|description)\b|\b(?:description|particular)\b", column, re.I)),
        None,
    ) or next(
        (column for column in frame.columns
         if re.search(r"\b(?:product|item)\b", column, re.I) and not re.search(r"\b(?:code|sku|model)\b", column, re.I)),
        None,
    ) or find_column(HEADER_ALIASES["product_name"])
    sku_column = find_column(HEADER_ALIASES["sku"])
    price_column = find_column(HEADER_ALIASES["price"])
    unit_column = find_column(HEADER_ALIASES["unit"])
    field_columns = {field: find_column(aliases) for field, aliases in HEADER_ALIASES.items()}
    records = []
    for row_number, (_, row) in enumerate(frame.iterrows(), header + 2):
        values = {_text(key): _text(value) for key, value in row.items() if _text(value)}
        product = _text(row.get(product_column, "")) if product_column else ""
        if not product:
            non_price = [value for key, value in values.items() if not re.search(r"price|rate|amount|cost|mrp", key, re.I)]
            product = non_price[0] if non_price else ""
        if not product:
            continue
        raw_price = _text(row.get(price_column, "")) if price_column else ""
        number = re.search(r"-?\d[\d,]*(?:\.\d+)?", raw_price)
        price = float(number.group(0).replace(",", "")) if number else None
        raw_mrp = _text(row.get(field_columns["mrp"], "")) if field_columns["mrp"] else ""
        raw_discount = _text(row.get(field_columns["discount"], "")) if field_columns["discount"] else ""
        raw_gst = _text(row.get(field_columns["gst"], "")) if field_columns["gst"] else ""
        mrp_match = re.search(r"-?\d[\d,]*(?:\.\d+)?", raw_mrp)
        discount_match = re.search(r"\d+(?:\.\d+)?", raw_discount)
        gst_match = re.search(r"\d+(?:\.\d+)?", raw_gst)
        if kind == "catalogue" and price is not None:
            kind_for_row = "price"
        else:
            kind_for_row = kind
        records.append({
            "record_type": kind_for_row,
            "product_name": product[:500],
            "sku": (_text(row.get(sku_column, "")) if sku_column else "")[:200],
            "description": json.dumps(values, ensure_ascii=False)[:8000],
            "unit": (_text(row.get(unit_column, "")) if unit_column else "")[:100],
            "price": price,
            "mrp": float(mrp_match.group(0).replace(",", "")) if mrp_match else None,
            "discount": float(discount_match.group(0)) if discount_match else None,
            "gst": float(gst_match.group(0)) if gst_match else None,
            "currency": "INR" if "₹" in raw_price or "inr" in raw_price.casefold() else "",
            "brand": (_text(row.get(field_columns["brand"], "")) if field_columns["brand"] else "")[:200],
            "product_category": (_text(row.get(field_columns["category"], "")) if field_columns["category"] else "")[:200],
            "specification": (_text(row.get(field_columns["specification"], "")) if field_columns["specification"] else "")[:2000],
            "pack_size": (_text(row.get(field_columns["pack_size"], "")) if field_columns["pack_size"] else "")[:200],
            "moq": (_text(row.get(field_columns["moq"], "")) if field_columns["moq"] else "")[:100],
            "availability": (_text(row.get(field_columns["availability"], "")) if field_columns["availability"] else "")[:100],
            "source_sheet": sheet[:200],
            "source_page": page[:50],
            "source_row": row_number,
            "image_reference": "",
            "extraction_confidence": "high",
        })
    return records


def _docx_rows(content: bytes) -> tuple[str, list[list[str]]]:
    with zipfile.ZipFile(BytesIO(content)) as archive:
        xml = archive.read("word/document.xml")
    root = ET.fromstring(xml)
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    rows = []
    for row in root.iter(namespace + "tr"):
        cells = []
        for cell in row.iter(namespace + "tc"):
            cells.append(_text(" ".join(node.text or "" for node in cell.iter(namespace + "t"))))
        if cells:
            rows.append(cells)
    text = " ".join(_text(node.text) for node in root.iter() if node.text)
    return text, rows


def _pdf_text(content: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(content), strict=False)
    return "\n".join(page.extract_text() or "" for page in reader.pages[:10])


def extract_document_text(filename: str, content: bytes, max_chars: int = 50_000) -> str:
    """Extract bounded searchable text from an uploaded vendor document."""
    suffix = Path(filename).suffix.casefold()
    if suffix in {".txt", ".log", ".csv"}:
        text = content[:500_000].decode("utf-8-sig", errors="replace")
    elif suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content), strict=False)
        text = "\n".join(
            f"[Page {page_number}]\n{page.extract_text() or ''}"
            for page_number, page in enumerate(reader.pages[:20], 1)
        )
    elif suffix == ".docx":
        text, _ = _docx_rows(content)
    elif suffix in {".xlsx", ".xls"}:
        sheets = pd.read_excel(
            BytesIO(content), sheet_name=None, header=None, dtype=str, nrows=1000
        )
        text = "\n".join(
            f"[Sheet {sheet_name}]\n{frame.fillna('').to_csv(index=False, header=False)}"
            for sheet_name, frame in sheets.items()
        )
    else:
        return ""
    return re.sub(r"[ \t]+", " ", text)[:max_chars].strip()


def _image_text(content: bytes) -> str:
    """Use optional local OCR when Pillow and pytesseract are installed."""
    try:
        import pytesseract
        from PIL import Image
        return pytesseract.image_to_string(Image.open(BytesIO(content)))[:20000]
    except (ImportError, OSError, ValueError):
        return ""


_CHAT_TIMESTAMP = re.compile(
    r"^\[?(?P<date>\d{1,4}[/-]\d{1,2}[/-]\d{2,4},?\s+\d{1,2}:\d{2}(?:\s*[ap]m)?)\]?\s*[-,]?\s*",
    re.IGNORECASE,
)
_CHAT_PRICE = re.compile(
    r"(?:₹|INR|Rs\.?)\s*([\d,]+(?:\.\d+)?)"
    r"|([\d,]+(?:\.\d+)?)\s*(?:INR\b|Rs\.?|₹)"
    r"|\b(?:price|rate)\s*(?:is|:|-)?\s*(?:₹|INR|Rs\.?)?\s*([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)


def extract_whatsapp_records(filename: str, content: bytes) -> tuple[list[dict], int]:
    """Extract only same-message product/price pairs; leave ambiguous chat context for review."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1252", errors="replace")

    records = []
    skipped = 0
    for line_number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        timestamp = _CHAT_TIMESTAMP.match(line)
        if not timestamp:
            continue
        message = line[timestamp.end():].strip()
        sender, separator, message_body = message.partition(":")
        if separator:
            sender = sender.strip()
            message = message_body.strip()
        else:
            sender = ""

        matches = list(_CHAT_PRICE.finditer(message))
        if not matches:
            continue
        if len(matches) != 1:
            skipped += 1
            continue

        price_match = matches[0]
        raw_price = next(value for value in price_match.groups() if value is not None)
        product_name = (message[:price_match.start()] + " " + message[price_match.end():]).strip(" \t-,:;.")
        product_name = re.sub(r"\b(?:price|rate|is|for)\b", " ", product_name, flags=re.IGNORECASE)
        product_name = re.sub(r"\s+", " ", product_name).strip(" \t-,:;.")
        if len(product_name) < 2 or not re.search(r"[^\W\d_]", product_name, re.UNICODE):
            skipped += 1
            continue

        currency = "INR" if re.search(r"₹|\bINR\b|\bRs\.?", message, re.IGNORECASE) else ""
        records.append({
            "record_type": "price",
            "product_name": product_name[:500],
            "sku": "",
            "description": message[:8000],
            "unit": "",
            "price": float(raw_price.replace(",", "")),
            "mrp": None,
            "discount": None,
            "gst": None,
            "currency": currency,
            "brand": "",
            "product_category": "",
            "specification": "",
            "pack_size": "",
            "moq": "",
            "availability": "",
            "source_sheet": sender[:200] or Path(filename).name[:200],
            "source_page": timestamp.group("date")[:50],
            "source_row": line_number,
            "image_reference": "",
            "extraction_confidence": "medium",
        })
    return records, skipped


def extract_records(filename: str, content: bytes) -> tuple[str | None, list[dict]]:
    """Return document kind and normalized records; unsupported files return no rows."""
    suffix = Path(filename).suffix.casefold()
    text = ""
    frames: list[tuple[str, pd.DataFrame]] = []
    try:
        if suffix in {".xlsx", ".xls"}:
            sheets = pd.read_excel(BytesIO(content), sheet_name=None, header=None, dtype=str)
            frames = [(str(sheet), frame) for sheet, frame in sheets.items()]
            text = " ".join(" ".join(map(str, frame.head(12).fillna("").values.flatten())) for _, frame in frames)
        elif suffix == ".csv":
            frame = pd.read_csv(BytesIO(content), header=None, dtype=str)
            frames = [("", frame)]
            text = content[:12000].decode("utf-8", errors="replace")
        elif suffix in {".txt", ".log"}:
            text = content[:12000].decode("utf-8", errors="replace")
            rows = [line.split("|") if "|" in line else re.split(r"\s{2,}", line) for line in text.splitlines() if line.strip()]
            frames = [("", pd.DataFrame(rows))]
        elif suffix == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(content), strict=False)
            pages = []
            for page_number, page in enumerate(reader.pages, 1):
                page_text = page.extract_text() or ""
                pages.append(page_text)
                rows = [re.split(r"\s{2,}|\t", line) for line in page_text.splitlines() if line.strip()]
                frames.append((f"Page {page_number}", pd.DataFrame(rows)))
            text = "\n".join(pages)
        elif suffix == ".docx":
            text, table_rows = _docx_rows(content)
            rows = table_rows or [[line] for line in text.splitlines() if line.strip()]
            frames = [("", pd.DataFrame(rows))]
        elif suffix in {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}:
            text = _image_text(content)
            rows = [re.split(r"\s{2,}|\t|\|", line) for line in text.splitlines() if line.strip()]
            frames = [("Image", pd.DataFrame(rows))]
        else:
            return None, []
    except Exception:
        return None, []
    kind = _kind(filename, text)
    if not kind:
        return None, []
    records = [record for sheet, frame in frames for record in _frame_records(frame, filename, sheet, kind, sheet.removeprefix("Page ") if sheet.startswith("Page ") else "")]
    return kind, records


def record_hash(record: dict) -> str:
    payload = json.dumps(record, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
