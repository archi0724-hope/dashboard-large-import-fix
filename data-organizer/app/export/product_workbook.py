"""Read original documents into a company-sorted, traceable product workbook."""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import math
import re
import shutil

import xlsxwriter

from ..ingestion.base import ReadOptions, TableBlock, file_sha256
from ..ingestion.registry import reader_for

PRODUCT_COLUMNS = ["Company Name", "Record Type", "Product Name", "SKU", "Brand", "Specification",
                   "Pack Size", "Unit", "Price", "MRP", "GST", "Currency", "HSN Code", "MOQ", "Carton Size",
                   "Price Basis", "Price Status", "Source File", "Source Sheet", "Source Page", "Source Row",
                   "Source Price Text", "Extraction Confidence", "Review Flag", "Notes", "Merge Source Workbook"]
CONTACT_COLUMNS = ["Company Name", "Contact Person", "Phone", "Email", "Website", "Address", "City",
                   "State", "PIN Code", "Country", "Source File", "Source Page"]
REVIEW_COLUMNS = ["Company Name", "Product Name", "SKU", "Issue", "Original Value", "Source File",
                  "Source Sheet", "Source Page", "Source Row"]
EXCEL_DATA_ROWS_PER_SHEET = 1048575  # XLSX format limit, excluding the header.
ALIASES = {
    "Company Name": ["company", "company name", "vendor", "vendor name", "supplier", "supplier name", "hospital name", "institution name", "organisation name", "organization name", "firm name", "enterprise name", "name of enterprise"],
    "Product Name": ["product", "product name", "item", "item name", "description", "item description", "particulars"],
    "SKU": ["sku", "code", "product code", "item code", "model", "model no", "model number"],
    "Brand": ["brand", "make", "manufacturer", "manufacturer name"],
    "Specification": ["specification", "specifications", "spec", "specs", "features"],
    "Pack Size": ["pack size", "pack", "packing", "package", "packing size"],
    "Unit": ["unit", "uom"], "Price": ["price", "rate", "unit price", "selling price", "sale price", "rate each", "dealer price", "dealer rate", "net price", "net rate", "rate per unit"],
    "MRP": ["mrp", "maximum retail price"], "GST": ["gst", "gst percent", "gst percentage"],
    "Currency": ["currency"], "Contact Person": ["contact person", "contact name"],
    "Phone": ["phone", "mobile", "telephone", "contact number"], "Email": ["email", "e mail"],
    "Website": ["website", "web", "url"], "Address": ["address"], "City": ["city"],
    "State": ["state"], "PIN Code": ["pin code", "pincode", "postal code", "zip code"], "Country": ["country"],
    "Record Type": ["record type"], "HSN Code": ["hsn", "hsn code"], "MOQ": ["moq", "minimum order quantity"],
    "Carton Size": ["carton size", "carton"], "Price Basis": ["price basis"], "Price Status": ["price status"],
    "Source File": ["source file"], "Source Sheet": ["source sheet"], "Source Page": ["source page"],
    "Source Row": ["source row"], "Source Price Text": ["source price text"],
    "Review Flag": ["review flag"], "Notes": ["notes"], "Merge Source Workbook": ["merge source workbook"],
}


def normalized(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


LOOKUP = {normalized(alias): name for name, aliases in ALIASES.items() for alias in aliases}
COMPACT_LOOKUP = {key.replace(" ", ""): value for key, value in LOOKUP.items()}


def amount(value, percent=False):
    """Parse only an unambiguous amount. Retain ranges/text for human review."""
    raw = "" if value is None else str(value).strip()
    if not raw:
        return "", ""
    text = re.sub(r"(?i)^(?:inr|rs\.?|usd|eur|gbp|₹|\$|€|£)\s*", "", raw).strip()
    text = re.sub(r"/-$", "", text).strip()
    if percent:
        text = text.rstrip("%").strip()
    if not re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{2,3})+)(?:\.\d+)?", text):
        return raw, "Unclear numeric value"
    number = float(text.replace(",", ""))
    return (number, "") if math.isfinite(number) else (raw, "Unclear numeric value")


def _extract_source_documents(source, settings, cfg, destination, company_from_path, progress=None):
    products, contacts, reviews = [], [], []
    options = ReadOptions(ocr=cfg.ocr_enabled, ocr_lang=settings.ocr_lang, ocr_dpi=settings.ocr_dpi,
                          tesseract_cmd=settings.tesseract_cmd, include_hidden_sheets=cfg.include_hidden_sheets)
    files = list(source.scan())
    block_headers = {}

    def review(record, issue, original="", row=""):
        reviews.append({key: record.get(key, "") for key in REVIEW_COLUMNS} |
                       {"Issue": issue, "Original Value": original, "Source Row": row})

    def read_rows(rows, metadata, fallback, ocr):
        headers, header_cells = block_headers.get(metadata.get("Source Sheet", ""), (None, []))
        for row_number, cells in rows:
            cells = ["" if cell is None else str(cell).strip() for cell in cells]
            names = [re.sub(r" (?:inr|rs|usd|eur|gbp)$", "", normalized(cell)) for cell in cells]
            mapped = [LOOKUP.get(name, COMPACT_LOOKUP.get(name.replace(" ", ""), "")) for name in names]
            # Keep the first source column for a field, preferring an exact
            # canonical heading. Preserve additional columns in Notes.
            for field in set(mapped) - {""}:
                indices = [i for i, value in enumerate(mapped) if value == field]
                preferred = next((i for i in indices if names[i].replace(" ", "") == normalized(field).replace(" ", "")), indices[0])
                for i in indices:
                    if i != preferred:
                        mapped[i] = ""
            is_header = "Product Name" in mapped or ("Company Name" in mapped and any(
                key in mapped for key in CONTACT_COLUMNS[1:-2]))
            if is_header:
                headers = mapped
                header_cells = cells
                block_headers[metadata.get("Source Sheet", "")] = (headers, header_cells)
                continue
            if not any(cells):
                continue
            if headers is None:
                review(metadata | {"Company Name": fallback}, "OCR text needs column review" if ocr else "Unmapped source row needs column review", " | ".join(cells), row_number)
                continue
            record = metadata | {name: cells[index] for index, name in enumerate(headers) if name and index < len(cells)}
            record["Company Name"] = record.get("Company Name") or fallback
            if not record["Company Name"]:
                review(record, "Company name is missing", row=row_number)
            if any(record.get(key) for key in CONTACT_COLUMNS[1:-2]):
                contacts.append({key: record.get(key, "") for key in CONTACT_COLUMNS})
            if "Product Name" not in headers:
                continue
            if not record.get("Product Name"):
                review(record, "Product name is missing", " | ".join(cells), row_number)
                continue
            record["Record Type"] = record.get("Record Type") or ("Price list" if "Price" in headers else "Catalog listing")
            record["Source Row"] = record.get("Source Row") or row_number
            record["Source Price Text"] = record.get("Source Price Text") or record.get("Price", "")
            record["Merge Source Workbook"] = record.get("Merge Source Workbook") or metadata["Source File"]
            extras = {f"{header_cells[i]} [{i+1}]": value for i, value in enumerate(cells)
                      if value and i < len(headers) and not headers[i] and header_cells[i]}
            if extras:
                text = json.dumps(extras, ensure_ascii=False)
                record["Notes"] = (record.get("Notes", "") + " | Retained unmapped source columns: " + text).strip(" |")
                review(record, "Unmapped source columns retained in Notes", text, row_number)
                record["Review Flag"] = record.get("Review Flag") or "Needs review"
            record["Extraction Confidence"] = "Needs review (OCR)" if ocr else "high"
            if not record["Company Name"]:
                record["Extraction Confidence"] = "Needs review"
            for field in ("Price", "MRP", "GST"):
                record[field], problem = amount(record.get(field, ""), percent=field == "GST")
                if problem:
                    record["Extraction Confidence"] = "Needs review"
                    review(record, f"{field}: {problem}", record[field], row_number)
            if not record.get("Price Basis"):
                record["Price Basis"] = next((header_cells[i] for i, field in enumerate(headers) if field == "Price"), "")
            if not record.get("Price Status"):
                record["Price Status"] = "Price recorded" if isinstance(record.get("Price"), (int, float)) else "Needs review" if record.get("Price") else "No price recorded"
            if not record.get("Currency"):
                raw = " ".join(header_cells[index] + " " + cells[index] for index, name in enumerate(headers) if name in {"Price", "MRP"} and index < len(cells))
                for pattern, currency in ((r"₹|\bINR\b|\bRs\.?", "INR"), (r"\bUSD\b", "USD"), (r"€|\bEUR\b", "EUR"), (r"£|\bGBP\b", "GBP")):
                    if re.search(pattern, raw, re.I):
                        record["Currency"] = currency
                        break
            for field in ("SKU", "Price", "Currency"):
                if record.get(field, "") == "":
                    review(record, f"{field} is missing", row=row_number)
                    record["Extraction Confidence"] = "Needs review"
            if ocr:
                review(record, "Check OCR against the original document", " | ".join(cells), row_number)
            if "review" in record["Extraction Confidence"].lower():
                record["Review Flag"] = record.get("Review Flag") or "Needs review"
            products.append(record)

    for index, file in enumerate(files, 1):
        block_headers = {}
        metadata = {"Source File": file.path, "Source Sheet": "", "Source Page": ""}
        before = len(products) + len(contacts)
        try:
            cls = reader_for(file.ext)
            if cls is None:
                review(metadata, "Unsupported file format; original preserved")
                continue
            local = source.ensure_local(file)
            reader = cls(options, staging_dir=settings.staging_dir / "legacy-doc-reading") if file.ext in {".doc", ".docx"} else cls(options)
            fallback = company_from_path(file.path)
            for block in reader.read(local.local_path):
                context = metadata | {"Source Sheet": block.sheet or "", "Source Page": block.page or ""}
                ocr = block.method.startswith("ocr") or block.method == "pdf_ocr"
                if isinstance(block, TableBlock):
                    read_rows(block.rows, context, fallback, ocr)
                else:
                    lines = [(number, re.split(r"\s*\|\s*|\t|\s{2,}", text)) for number, text, _ in block.lines]
                    # Single-column prose is not a product table. Keep uncertain
                    # OCR text for review rather than inventing a product row.
                    for number, cells in lines:
                        if len(cells) == 1 and not ocr:
                            review(context | {"Company Name": fallback}, "Unstructured text needs column review", cells[0], number)
                    lines = [(number, cells) for number, cells in lines if len(cells) > 1 or ocr]
                    read_rows(lines, context, fallback, ocr)
                    labelled = {}
                    for _, text, _ in block.lines:
                        match = re.match(r"^([^:=]+)\s*[:=]\s*(.+)$", text)
                        if match and (field := LOOKUP.get(normalized(match[1]))):
                            labelled[field] = match[2].strip()
                    if any(labelled.get(key) for key in CONTACT_COLUMNS[1:-2]):
                        contacts.append(context | {key: labelled.get(key, "") for key in CONTACT_COLUMNS[1:-2]} |
                                        {"Company Name": labelled.get("Company Name", fallback)})
            for issue in reader.issues:
                review(metadata, issue.message, issue.location)
            if len(products) + len(contacts) == before:
                review(metadata, "No structured products or contacts found; review the original document")
        except Exception as error:
            review(metadata, f"Could not read file: {type(error).__name__}: {error}")
        finally:
            if progress:
                progress(index, len(files), file.name)

    return products, contacts, reviews


class _OneSource:
    def __init__(self, file):
        self.file = file

    def scan(self):
        return iter([self.file])

    def ensure_local(self, file):
        return file


def _cached_file_extraction(file, settings, cfg, destination, company_from_path, cache_name):
    cache = Path(cache_name)
    cached = cache.exists()
    if cached:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            cached = False
    if not cached:
        extracted = _extract_source_documents(_OneSource(file), settings, cfg, destination, company_from_path)
        data = {"source": file.path, "products": extracted[0], "contacts": extracted[1], "reviews": extracted[2]}
        temporary = cache.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(cache)
    return data, cached


def create_product_workbook(source, settings, cfg, destination, company_from_path, progress=None, reference_files=(), product_sheet_title="Products & Prices"):
    excluded = {str(name).replace("\\", "/").casefold() for name in reference_files}
    files = [file for file in source.scan() if file.path.replace("\\", "/").casefold() not in excluded]
    products, contacts, reviews, ledger = [], [], [], []
    def review(record, issue, original="", row=""):
        reviews.append({key: record.get(key, "") for key in REVIEW_COLUMNS} |
                       {"Issue": issue, "Original Value": original, "Source Row": row})
    cache_dir = Path(destination).parent / ".product_extraction_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    signature = repr(("sample-26-columns-v2", cfg.ocr_enabled, settings.ocr_lang, settings.ocr_dpi,
                      settings.tesseract_cmd, cfg.include_hidden_sheets, shutil.which("soffice")))
    unique = {}
    completed = 0

    def record_file(file, data, cached):
        nonlocal completed
        batches = []
        for kind in ("products", "contacts", "reviews"):
            rows = []
            for old in data[kind]:
                row = dict(old)
                for field in ("Source File", "Merge Source Workbook"):
                    if row.get(field) == data["source"]:
                        row[field] = file.path
                rows.append(row)
            batches.append(rows)
        found_products, found_contacts, found_reviews = batches
        products.extend(found_products); contacts.extend(found_contacts); reviews.extend(found_reviews)
        status = "Products extracted" if found_products else "Contacts extracted" if found_contacts else "Needs review"
        if not file.supported:
            status = "Unsupported format"
        ledger.append({"Source File": file.path, "File Type": file.ext, "Status": status,
                       "Product Rows": len(found_products), "Contact Rows": len(found_contacts),
                       "Review Items": len(found_reviews), "Cached Extraction": "Yes" if cached else "No"})
        completed += 1
        if progress:
            progress(completed, len(files), file.name)

    def prepare(file):
        local = source.ensure_local(file)
        extra = "structured-headers-v3" if file.ext in {".xlsx", ".xls", ".xlsm", ".csv", ".txt", ".json", ".doc", ".docx"} else ""
        key = hashlib.sha256((file_sha256(local.local_path) + file.ext + signature + company_from_path(file.path) + extra).encode()).hexdigest()
        return local, key

    # Fingerprint once, then schedule each unique document once. Repeated uploads
    # retain separate audit/source rows without tying up workers on the same PDF.
    workers = 4 if getattr(source, "kind", "local") == "local" else 1
    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending = {}
        for file in files:
            if reader_for(file.ext) is None:
                record_file(file, {"source":file.path,"products":[],"contacts":[],"reviews":[
                    {"Source File":file.path,"Issue":"Unsupported file format; original preserved"}]},False)
            else:
                pending[executor.submit(prepare,file)] = file
        for index,future in enumerate(as_completed(pending),1):
            file=pending[future]
            try:
                local,key=future.result()
                unique.setdefault(key,[]).append(local)
            except Exception as error:
                record_file(file,{"source":file.path,"products":[],"contacts":[],"reviews":[
                    {"Source File":file.path,"Issue":f"Could not read file: {type(error).__name__}: {error}"}]},False)
            if progress:
                progress(completed,len(files),f"Checking source signatures: {index}/{len(pending)}")
    pool = ProcessPoolExecutor if len(unique) > 16 else ThreadPoolExecutor
    with pool(max_workers=workers) as executor:
        pending={}
        for key,copies in sorted(unique.items(),key=lambda item:item[1][0].size):
            file=copies[0]
            future=executor.submit(_cached_file_extraction,file,settings,cfg,destination,company_from_path,str(cache_dir/(key+".json")))
            pending[future]=copies
        for future in as_completed(pending):
            copies=pending[future]
            try:
                data,cached=future.result()
            except Exception as error:
                data={"source":copies[0].path,"products":[],"contacts":[],"reviews":[
                    {"Source File":copies[0].path,"Issue":f"Could not read file: {type(error).__name__}: {error}"}]}
                cached=False
            for index,file in enumerate(copies):
                record_file(file,data,cached or index>0)
    ledger.sort(key=lambda row:row["Source File"].casefold())

    groups = defaultdict(list)
    for record in products:
        groups[tuple(normalized(record.get(key, "")) for key in ("Company Name", "Product Name", "SKU", "Currency"))].append(record)
    for records in groups.values():
        prices = {record.get("Price") for record in records if record.get("Price", "") != ""}
        mrps = {record.get("MRP") for record in records if record.get("MRP", "") != ""}
        if len(records) > 1:
            issue = "Conflicting prices or MRP" if len(prices) > 1 or len(mrps) > 1 else "Possible duplicate; all rows retained"
            for record in records:
                review(record, issue, str(record.get("Price", "")), record["Source Row"])
                record["Extraction Confidence"] = "Needs review"
                record["Review Flag"] = record.get("Review Flag") or "Needs review"

    products.sort(key=lambda record: tuple(str(record.get(key, "")).casefold() for key in ("Company Name", "Product Name", "SKU")))
    contacts = list({tuple(record.get(key, "") for key in CONTACT_COLUMNS): record for record in contacts}.values())
    contacts.sort(key=lambda record: str(record.get("Company Name", "")).casefold())
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(".tmp.xlsx")
    try:
        with xlsxwriter.Workbook(str(temp), {"constant_memory": True, "strings_to_formulas": False, "strings_to_urls": False}) as book:
            header = book.add_format({"bold": True, "bg_color": "#142447", "font_color": "white"})
            money = book.add_format({"num_format": "0.00"})
            for title, columns, records in ((product_sheet_title, PRODUCT_COLUMNS, products),
                                             ("Company Contacts", CONTACT_COLUMNS, contacts),
                                             ("Needs Review", REVIEW_COLUMNS, reviews),
                                             ("File Processing", ["Source File", "File Type", "Status", "Product Rows", "Contact Rows", "Review Items", "Cached Extraction"], ledger)):
                for part, start in enumerate(range(0, max(1, len(records)), EXCEL_DATA_ROWS_PER_SHEET), 1):
                    name = title if part == 1 else f"{title} {part}"
                    sheet = book.add_worksheet(name)
                    sheet.write_row(0, 0, columns, header)
                    sheet.freeze_panes(1, 1)
                    chunk = records[start:start + EXCEL_DATA_ROWS_PER_SHEET]
                    for row, record in enumerate(chunk, 1):
                        for col, key in enumerate(columns):
                            value = record.get(key, "")
                            if value is None or value == "":
                                sheet.write_blank(row, col, None)
                            elif isinstance(value, (int, float)):
                                sheet.write_number(row, col, value, money if key in {"Price", "MRP"} else None)
                            else:
                                sheet.write_string(row, col, str(value or ""))
                    sheet.autofilter(0, 0, max(1, len(chunk)), len(columns) - 1)
                    sheet.set_column(0, len(columns) - 1, 22)
                    for key in ("Specification", "Source File", "Issue", "Original Value"):
                        if key in columns:
                            sheet.set_column(columns.index(key), columns.index(key), 45)
        temp.replace(destination)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return {"file": destination.name, "products": len(products), "contacts": len(contacts),
            "review_items": len(reviews), "files": len(files), "processable_files": sum(f.supported for f in files)}
