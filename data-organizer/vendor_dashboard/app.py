from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import logging
import mimetypes
import os
import re
import unicodedata
from datetime import datetime
from io import BytesIO
from functools import partial
from pathlib import Path
import time
from urllib.parse import quote_plus

import pandas as pd
import streamlit as st

import exports as exports_module
import catalogue_jobs
from import_service import import_documents
from drive_import import DiskUpload
import storage as storage_module
from vendor_core import (ALLOWED_EXTENSIONS, DOCUMENT_TYPES, build_checklist, dashboard_counts,
                         documents_for_category, export_filename, filter_checklist, read_vendor_file, supporting_category)

# Streamlit can retain imported modules during a hot reload. Refresh an older
# export or Store class once so the app starts without a manual restart.
if not hasattr(exports_module, "read_manual_catalogue_workbook") or not getattr(exports_module, "STREAMING_MANUAL_IMPORT", False):
    exports_module = importlib.reload(exports_module)
catalogue_price_workbook_bytes = exports_module.catalogue_price_workbook_bytes
csv_bytes = exports_module.csv_bytes
workbook_bytes = exports_module.workbook_bytes

MANUAL_CATALOGUE_IMPORT_VERSION = 6
if getattr(storage_module.Store, "manual_catalogue_import_version", 0) != MANUAL_CATALOGUE_IMPORT_VERSION or not all(
    hasattr(storage_module.Store, method)
    for method in (
        "product_record_count",
        "product_classification_counts",
        "product_review_records",
        "import_catalogue_workbook",
        "catalogue_export_records",
    )
):
    storage_module = importlib.reload(storage_module)
Store = storage_module.Store

APP_DIR = Path(__file__).resolve().parent
st.set_page_config(page_title="HOSPkart.com · Vendor Document Dashboard", page_icon=":material/auto_awesome:", layout="wide", initial_sidebar_state="expanded")
st.markdown("""
<style>
.stApp{background:linear-gradient(180deg,#f8fafb 0%,#f2f6f8 100%);color:#172b3a}
.block-container{max-width:1460px;padding-top:3.5rem;padding-bottom:3rem}
h1{font-size:1.65rem!important;font-weight:750!important;letter-spacing:-.025em;color:#142d55}
h2,h3{letter-spacing:-.015em;color:#142d55}
[data-testid="stHeader"]{background:#fff;border-bottom:1px solid #dfe6f1}
[data-testid="stExpandSidebarButton"]{visibility:visible!important;opacity:1!important;width:40px;height:40px;border:1px solid #dfe6f1;border-radius:8px;background:#fff;color:#142d55}
[data-testid="stSidebar"]{background:#101e38;border-right:1px solid #293b59}
[data-testid="stSidebar"] [data-testid="stRadio"] label{padding:5px 6px;border-radius:8px;margin:2px 0}
[data-testid="stSidebar"] [data-testid="stRadio"] label:has(input:checked){background:#203c54;box-shadow:inset 3px 0 #167d82}
[data-testid="stMetric"]{background:#fff;border:1px solid #dbe4ec;border-top:3px solid #167d82;border-radius:12px;padding:1rem;box-shadow:0 6px 18px rgba(23,43,58,.05)}
[data-testid="stMetricValue"]{font-size:1.85rem;color:#172b3a;font-weight:700}
[data-testid="stMetricLabel"]{color:#63758e;font-size:.875rem;font-weight:650}
[data-testid="stFileUploader"]{border:1px dashed #b8c8e0;border-radius:10px;background:#fff}
[data-testid="stExpander"]{background:#fff;border:1px solid #dfe6f1;border-radius:10px}
[data-testid="stBaseButton-secondary"]{background:#fff;color:#142d55;border-color:#dfe6f1;font-weight:600}
[data-testid="stBaseButton-primary"]{background:#167d82;color:#fff;border-color:#167d82;font-weight:700}
[data-testid="stSidebar"] [data-testid="stBaseButton-secondary"]{background:#253858;color:#e0e7f4;border-color:#334567}
[data-testid="stSidebar"] [data-testid="stExpander"]{background:#192942;border-color:#334567;color:#e0e7f4}
.brand{display:flex;align-items:center;margin:.5rem 0 1.3rem}
.brand img{display:block;width:160px;max-width:100%;height:auto}
.workspace-hero{position:relative;overflow:hidden;background:linear-gradient(120deg,#16254b 0%,#29375f 65%,#154b5b 100%);border:1px solid #34466e;border-radius:18px;padding:30px;margin:1rem 0;color:#fff}
.workspace-hero::after{content:'✦';position:absolute;right:32px;top:12px;font-size:110px;line-height:1;color:#a2b7ff;opacity:.13;pointer-events:none}
.workspace-hero .hero-eyebrow{font-size:.875rem;font-weight:750;letter-spacing:.18em;color:#79eaff;text-transform:uppercase}
.workspace-hero h2{font-size:1.5rem!important;color:#fff;margin:16px 0 18px;max-width:80%}
.workspace-hero p{font-size:.88rem;line-height:1.7;max-width:650px;color:#e4eaff}
.hero-badges{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}.hero-badges span{border:1px solid #526080;border-radius:30px;padding:5px 10px;font-size:.875rem;color:#e4eaff;background:#ffffff06}
.summary-label{margin:.9rem 0 .45rem;color:#63758e;font-size:.875rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em}
.type-label{margin:1.1rem 0 .45rem;color:#63758e;font-size:.78rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em}
.table-scroll{max-height:470px;overflow:auto;border:1px solid #dee6ed;border-radius:10px;background:#fff}
.desk-table{border-collapse:collapse;min-width:100%;font-size:.88rem}
.desk-table th{position:sticky;top:0;background:#f5f7fc;color:#47607f;text-align:left;padding:12px;white-space:nowrap;border-bottom:1px solid #dfe6f1}
.desk-table td{padding:10px 12px;border-bottom:1px solid #eef1f4;white-space:nowrap}
.desk-table tbody tr:nth-child(even){background:#fafcfd}
.yes{background:#e2f3eb;color:#17613e;border-radius:5px;padding:3px 9px;font-weight:600}
.no{background:#fff0ec;color:#a04b34;border-radius:5px;padding:3px 9px}
.small-note{font-size:.85rem;color:#647789}
@media(max-width:720px){.block-container{padding:4.5rem 1rem 2rem}h1{font-size:1.65rem!important}[data-testid="stMetricValue"]{font-size:1.55rem}}
</style>
""", unsafe_allow_html=True)


def setting(name: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(name, os.environ.get(name, default)))
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return os.environ.get(name, default)


from app.admin_access import COOKIE as ADMIN_COOKIE, is_admin

def admin_allowed():
    return is_admin(st.context.cookies.get(ADMIN_COOKIE, ""))


def require_admin():
    if not admin_allowed():
        st.warning("Admin login required. Use the A icon at the top-right of the main dashboard.")
        st.stop()


can_edit = admin_allowed()

workspace_started = time.perf_counter()
loading_placeholder = st.empty()
with loading_placeholder.container():
    st.markdown("### Opening Vendor workspace")
    startup_status = st.empty()
    startup_status.info("Connecting to saved data…")
    st.caption("Your workspace is loading. Please keep this page open.")


@st.cache_resource(show_spinner=False)
def open_store(data_dir: str, database_url: str, storage_schema: str = "product-records-v3") -> Store:
    return Store(Path(data_dir), database_url)


@st.cache_data(ttl=60, max_entries=4, show_spinner=False)
def load_workspace_data(identity: str, include_drive: bool, _store: Store) -> dict:
    """Reuse shared read data across UI interactions; writes and Refresh clear it."""
    from drive_view import combine_company_documents, inventory
    vendors = _store.vendors()
    documents = _store.documents()
    uploaded_documents = documents
    aliases = _store.aliases()
    drive_failed = False
    if include_drive:
        try:
            drive_started = time.perf_counter()
            snapshot = inventory(identity, _store, include_record_totals=False)
            logging.getLogger("organizer.application").info("Drive references read in %.3fs", time.perf_counter() - drive_started)
            projection_started = time.perf_counter()
            vendors, documents = combine_company_documents(vendors, documents, snapshot['references'], aliases)
            logging.getLogger("organizer.application").info("Drive references projected in %.3fs", time.perf_counter() - projection_started)
        except Exception as error:
            logging.getLogger(__name__).error("Drive references unavailable: %s", type(error).__name__)
            drive_failed = True
    checklist = build_checklist(vendors, documents)
    return {"vendors": vendors, "documents": documents, "uploaded_documents": uploaded_documents,
            "aliases": aliases, "checklist": checklist,
            "counts": dashboard_counts(vendors, documents, checklist=checklist), "drive_failed": drive_failed}


try:
    vendor_data_dir = setting("VENDOR_DATA_DIR", str(APP_DIR / "vendor_data"))
    database_url = setting("DATABASE_URL")
    storage_schema = f"product-records-v5:manual-{MANUAL_CATALOGUE_IMPORT_VERSION}"
    store = open_store(
        vendor_data_dir,
        database_url,
        storage_schema,
    )
    if getattr(store, "manual_catalogue_import_version", 0) != MANUAL_CATALOGUE_IMPORT_VERSION or not all(
        callable(getattr(store, method, None))
        for method in (
            "product_record_count",
            "product_classification_counts",
            "product_review_records",
            "import_catalogue_workbook",
            "catalogue_export_records",
        )
    ):
        open_store.clear()
        store = open_store(vendor_data_dir, database_url, storage_schema)
        if not callable(getattr(store, "product_classification_counts", None)):
            raise RuntimeError("Cached vendor storage does not support catalogue classification.")
except Exception as error:
    logging.getLogger(__name__).error("Storage initialization failed: %s", type(error).__name__)
    startup_status.error("Could not connect to saved data")
    st.error("Storage could not be opened. Check the data folder or your private database settings. No documents were loaded.")
    st.stop()

HAS_ARROW = importlib.util.find_spec("pyarrow") is not None


def show_table(frame: pd.DataFrame):
    if HAS_ARROW:
        return st.dataframe(frame, hide_index=True, width="stretch", height=min(460, max(150, 40+35*len(frame))))
    markup = frame.to_html(index=False, escape=True, classes="desk-table", border=0)
    markup = markup.replace(">Yes<", '><span class="yes">Yes</span><').replace(">No<", '><span class="no">No</span><')
    st.markdown('<div class="table-scroll">'+markup+'</div>', unsafe_allow_html=True)


@st.cache_data(show_spinner=False, max_entries=128)
def first_page_pdf(payload: bytes) -> bytes | None:
    """Create a small cached PDF containing only the first page for fast preview."""
    try:
        from pypdf import PdfReader, PdfWriter
        source = PdfReader(BytesIO(payload), strict=False)
        if not source.pages:
            return None
        output = BytesIO()
        writer = PdfWriter()
        writer.add_page(source.pages[0])
        writer.write(output)
        return output.getvalue()
    except Exception:
        return None


@st.cache_data(show_spinner=False, max_entries=64)
def document_bytes(_store: Store, document_id: int, file_hash: str) -> bytes | None:
    """Cache document reads across Streamlit reruns until the file hash changes."""
    return _store.read_bytes(document_id)


@st.cache_data(ttl=60, max_entries=4, show_spinner=False)
def drive_preview_bytes(identity: str, document_id: str, version: str, _store: Store) -> bytes:
    from app.drive.originals import preview_bytes
    return preview_bytes(_store, document_id)


def remove_dashboard_document(store: Store, document_id: int | str) -> bool:
    ident = str(document_id)
    if ident.isdecimal():
        return store.delete_document(int(ident))
    if ident.startswith("drive:"):
        from app.drive.index_store import DriveIndex
        return DriveIndex(store).remove_entry(ident[6:])
    raise ValueError("This document cannot be removed from the dashboard.")


def render_dashboard_delete_confirmation(
    store: Store,
    document_id: int | str,
    filename: str,
    state_key: str,
    key_prefix: str,
    *,
    drive_original: bool = False,
):
    ident = str(document_id)
    if str(st.session_state.get(state_key, "")) != ident:
        return
    if drive_original or ident.startswith("drive:"):
        st.warning(
            f"Remove {filename} from this dashboard? The original in Google Drive stays untouched "
            "and can return if the source is indexed again."
        )
    else:
        st.warning(f"Delete {filename} and its extracted rows from this dashboard? Uploaded ZIP archives are kept.")
    confirm, cancel = st.columns(2)
    if confirm.button("Confirm delete", key=f"confirm_delete_{key_prefix}", type="primary"):
        require_admin()
        try:
            if not remove_dashboard_document(store, ident):
                raise ValueError("This document is no longer available. Refresh and retry.")
        except Exception as error:
            logging.getLogger(__name__).exception("Dashboard document deletion failed")
            st.error(str(error) if isinstance(error, (ValueError, RuntimeError, OSError)) else "The document could not be removed. Check storage access.")
        else:
            st.session_state.pop(state_key, None)
            for key in ("preview_document_id", "review_preview_document_id", "review_drive_preview_id", "review_drive_document"):
                st.session_state.pop(key, None)
            message = (
                "Drive reference removed from the dashboard. Its original remains in Google Drive."
                if drive_original or ident.startswith("drive:")
                else "Document deleted. The checklist and search have been refreshed."
            )
            refresh(message)
    if cancel.button("Cancel", key=f"cancel_delete_{key_prefix}"):
        st.session_state.pop(state_key, None)
        st.rerun()


def clear_document_views():
    document_bytes.clear()
    drive_preview_bytes.clear()
    from drive_view import inventory, source_rows
    inventory.clear()
    source_rows.clear()


@st.cache_data(show_spinner=False, max_entries=2, ttl=600)
def catalogue_download(_store: Store, store_identity: str, signature: tuple) -> bytes:
    return catalogue_price_workbook_bytes(_store.catalogue_export_records(), include_review_details=True)


@st.cache_data(show_spinner=False, max_entries=32)
def first_sheet_excel_preview(payload: bytes) -> tuple[str, pd.DataFrame] | None:
    """Read a bounded, display-only preview from the first Excel worksheet."""
    try:
        workbook = pd.ExcelFile(BytesIO(payload))
        sheet_name = workbook.sheet_names[0]
        return sheet_name, pd.read_excel(workbook, sheet_name=sheet_name, nrows=100).iloc[:, :50]
    except Exception:
        return None


def show_document_preview(filename: str, payload: bytes):
    """Render a fast preview while keeping the original file available for download."""
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        preview = first_page_pdf(payload)
        if preview:
            st.caption("Fast preview: first page. Download the original file for the complete document.")
            st.pdf(preview)
        else:
            st.pdf(payload)
    elif mime.startswith("image/"):
        st.image(payload, caption=filename, width="stretch")
    elif suffix in {".xlsx", ".xls"}:
        preview = first_sheet_excel_preview(payload)
        if preview is None:
            st.info("This Excel workbook could not be read for preview. Use Download file to open it.")
        else:
            sheet_name, rows = preview
            st.caption(f"Preview: {sheet_name} · first {len(rows)} data row(s), up to 50 columns. Download the original file for all sheets and rows.")
            st.dataframe(rows, hide_index=True, height=360)
    elif suffix == ".docx":
        try:
            from docx import Document
            document = Document(BytesIO(payload))
            lines = [paragraph.text for paragraph in document.paragraphs[:300] if paragraph.text.strip()]
            for table in document.tables[:10]:
                lines.extend(" | ".join(cell.text for cell in row.cells[:20]) for row in table.rows[:100])
            st.caption("Word document text preview. Download file for the original formatting and complete document.")
            st.text("\n".join(lines)[:200000] or "This Word document has no readable text.")
        except Exception:
            st.info("This Word document could not be previewed. Use Download file to open it.")
    elif mime.startswith("text/") or suffix in {".csv", ".txt", ".log"}:
        try:
            st.code(payload.decode("utf-8", errors="replace"), language="text")
        except Exception:
            st.info("This text file could not be decoded for inline preview.")
    else:
        st.info("Inline preview is not available for this format. Use Download file to open it.")


VENDOR_AI_SUGGESTIONS = [
    "Find vendors for surgical gloves",
    "Compare prices for wheelchairs",
    "Show catalogue products from ABC Medical",
    "Which vendors have GST documents?",
    "Find the cheapest supplier for this product",
    "Search company information online",
]

VENDOR_AI_SYNONYMS = {
    "glove": ["glove", "gloves", "nitrile glove", "surgical glove", "examination glove", "hand protection"],
    "wheelchair": ["wheelchair", "wheelchairs", "mobility chair", "transport chair"],
    "bed": ["bed", "beds", "hospital bed", "medical bed"],
    "catalogue": ["catalogue", "catalog", "price list", "rate list", "product list"],
    "gst": ["gst", "goods and services tax", "gstin", "gst registration"],
    "contact": ["contact", "phone", "mobile", "email", "website", "address"],
    "price": ["price", "prices", "mrp", "dealer price", "rate", "cost"],
    "company": ["company", "vendor", "supplier", "manufacturer", "dealer"],
}


def vendor_ai_normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = text.replace("₹", "rs ").replace("€", "eur ").replace("$", "usd ")
    text = re.sub(r"[^a-z0-9\s]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def vendor_ai_tokens(value: str) -> list[str]:
    return [token for token in vendor_ai_normalize(value).split() if len(token) > 1]


def vendor_ai_terms(question: str) -> set[str]:
    terms = set(vendor_ai_tokens(question))
    lower = vendor_ai_normalize(question)
    for key, aliases in VENDOR_AI_SYNONYMS.items():
        if key in lower or any(alias in lower for alias in aliases):
            terms.update({key, *aliases})
    return {t for t in terms if t}


@st.cache_data(show_spinner=False, max_entries=128, ttl=3600)
def vendor_ai_extract_text(filename: str, payload: bytes | None) -> tuple[str, str]:
    if payload is None:
        return "", "Original file is unavailable."
    suffix = Path(filename).suffix.lower()
    warning = ""
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader
            from pypdf.errors import PdfReadError
            try:
                reader = PdfReader(BytesIO(payload), strict=False)
                pages = [page.extract_text() or "" for page in reader.pages]
            except (PdfReadError, OSError, ValueError) as error:
                return "", f"Could not read the PDF ({type(error).__name__})."
            scanned_pages = [index for index, text in enumerate(pages) if not text.strip()]
            if scanned_pages:
                try:
                    import pypdfium2 as pdfium
                    import pytesseract
                except ImportError:
                    return "\n".join(pages), "Scanned pages were not OCR-indexed because OCR dependencies are unavailable."
                try:
                    with pdfium.PdfDocument(payload) as document:
                        for index in scanned_pages:
                            bitmap = document[index].render(scale=2)
                            try:
                                with bitmap.to_pil() as image:
                                    pages[index] = pytesseract.image_to_string(image, lang="eng", timeout=30)
                            finally:
                                bitmap.close()
                except pytesseract.TesseractNotFoundError:
                    warning = "Scanned pages were not OCR-indexed because Tesseract is unavailable."
                except pytesseract.TesseractError as error:
                    warning = f"Scanned-page OCR failed ({type(error).__name__})."
                except (pdfium.PdfiumError, OSError, ValueError) as error:
                    warning = f"Scanned-page OCR failed ({type(error).__name__})."
            text = "\n".join(pages)
        elif suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}:
            try:
                import pytesseract
                from PIL import Image, ImageSequence
            except ImportError:
                return "", "Image OCR was not run because OCR dependencies are unavailable."
            try:
                parts = []
                with Image.open(BytesIO(payload)) as image:
                    for frame in ImageSequence.Iterator(image):
                        with frame.copy() as copy:
                            parts.append(pytesseract.image_to_string(copy, lang="eng", timeout=30))
                text = "\n".join(parts)
            except pytesseract.TesseractNotFoundError:
                return "", "Image OCR was not run because Tesseract is unavailable."
            except pytesseract.TesseractError as error:
                return "", f"Image OCR failed ({type(error).__name__})."
            except (OSError, ValueError) as error:
                return "", f"Could not read the image ({type(error).__name__})."
        elif suffix in {".csv", ".txt", ".log", ".json"}:
            text = payload.decode("utf-8", errors="replace")
        elif suffix in {".xlsx", ".xls"}:
            import zipfile
            try:
                df = pd.read_excel(BytesIO(payload), sheet_name=None, dtype=str)
                text = "\n".join(sheet.to_csv(index=False, header=False) for sheet in df.values())
            except (ImportError, OSError, ValueError, zipfile.BadZipFile) as error:
                return "", f"Could not read the workbook ({type(error).__name__})."
        elif suffix == ".docx":
            import zipfile
            try:
                with zipfile.ZipFile(BytesIO(payload)) as archive:
                    xml = archive.read("word/document.xml")
                text = re.sub(r"<[^>]+>", " ", xml.decode("utf-8", errors="replace"))
                text = re.sub(r"\s+", " ", text)
            except (OSError, KeyError, zipfile.BadZipFile) as error:
                return "", f"Could not read the Word document ({type(error).__name__})."
        else:
            return "", "This file type has no text extractor."
    except ImportError:
        return "", "Required document-reading dependencies are unavailable."
    if len(text) > 100_000:
        text = text[:100_000]
        warning = "Only the first 100,000 extracted characters are searchable."
    elif not text.strip() and not warning:
        warning = "No searchable text was extracted; the filename and vendor remain searchable."
    return text, warning


def vendor_ai_purchase_invoice_signature(store) -> tuple[int, str]:
    with store.connection() as db:
        if store.cloud:
            exists = db.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() AND table_name=?",
                ("vdd_purchase_invoices",),
            ).fetchone()
        else:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                ("vdd_purchase_invoices",),
            ).fetchone()
        if not exists:
            return 0, ""
        row = db.execute(
            "SELECT COUNT(*) AS n, COALESCE(MAX(created_at), '') AS latest FROM vdd_purchase_invoices"
        ).fetchone()
    return int(row["n"]), str(row["latest"] or "")


def vendor_ai_signature(store) -> tuple:
    from search_sources import signature
    return store.search_index_signature(), vendor_ai_purchase_invoice_signature(store), signature(store)


def vendor_ai_purchase_invoices(store) -> tuple[list[dict], list[str]]:
    with store.connection() as db:
        if store.cloud:
            exists = db.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() AND table_name=?",
                ("vdd_purchase_invoices",),
            ).fetchone()
        else:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                ("vdd_purchase_invoices",),
            ).fetchone()
        if not exists:
            return [], []
        rows = db.execute(
            "SELECT id,filename,record_json FROM vdd_purchase_invoices ORDER BY created_at DESC"
        ).fetchall()
    index = []
    warnings = []
    for row in rows:
        try:
            record = json.loads(row["record_json"])
        except json.JSONDecodeError:
            warnings.append(f"{row['filename']}: saved invoice data could not be read.")
            continue
        fields = record.get("fields") or {}
        items = record.get("items") or []
        supplier = str(fields.get("supplier") or "").strip()
        number = str(fields.get("invoice_number") or "").strip()
        date = str(fields.get("invoice_date") or "").strip()
        item_text = " ".join(
            str(item.get(key) or "")
            for item in items
            for key in ("description", "quantity", "unit_price", "amount")
        )
        index.append({
            "title": f"{number or row['filename']} · {supplier or 'Purchase invoice'}",
            "source": "Purchase invoices",
            "result_id": f"invoice:{row['id']}",
            "priority": 2,
            "score_hint": 18,
            "text": f"{row['filename']} {supplier} {number} {date} {fields.get('gstin', '')} "
                    f"{fields.get('total', '')} {fields.get('currency', '')} {item_text} {record.get('raw_text', '')}",
            "vendor": supplier or "Not found in invoice",
            "product": item_text,
            "invoice_id": row["id"],
            "invoice_number": number,
            "invoice_date": date,
            "invoice_total": str(fields.get("total") or ""),
            "source_file": row["filename"],
        })
    return index, warnings


def vendor_ai_index(store) -> list[dict]:
    signature = vendor_ai_signature(store)
    cached = st.session_state.get("vendor_ai_index_cache")
    if cached and cached.get("signature") == signature:
        return cached["index"]
    index = []
    warnings = []
    vendors = store.vendors()
    for _, vendor in vendors.iterrows():
        company = str(vendor.get("company_name") or "").strip()
        if not company:
            continue
        index.append({
            "title": company,
            "source": "Vendor master",
            "result_id": f"vendor:{vendor.get('company_key', company)}",
            "priority": 0,
            "score_hint": 30,
            "text": f"{company} vendor supplier company manufacturer dealer contact details",
            "vendor": company,
            "source_file": "Vendor master",
        })
    documents = store.documents()
    for _, doc in documents.iterrows():
        company = str(doc.get("company_name") or "").strip()
        filename = str(doc.get("filename") or "").strip()
        if not filename:
            continue
        try:
            payload = store.read_bytes(int(doc["id"])) if doc.get("available") else None
            body, warning = vendor_ai_extract_text(filename, payload)
        except Exception as error:
            body, warning = "", f"Document could not be indexed ({type(error).__name__})."
        if warning:
            warnings.append(f"{filename}: {warning}")
        text = f"{company} {filename} {body} {' '.join(doc.get('types', []))}"
        if "catalog" in filename.lower() or "price" in filename.lower() or "rate" in filename.lower():
            priority = 3
        elif any(token in filename.lower() for token in ("gst", "pan", "aadhar", "company")):
            priority = 2
        else:
            priority = 1
        index.append({
            "title": company or filename,
            "source": "Uploaded documents",
            "result_id": f"document:{doc['id']}",
            "priority": priority,
            "score_hint": 12,
            "text": text,
            "vendor": company or "Not found in available vendor data",
            "source_file": filename,
            "document_id": int(doc["id"]),
        })
    for _, record in store.product_records().iterrows():
        company = str(record.get("company_name") or "").strip()
        product = str(record.get("product_name") or "").strip()
        price = record.get("price")
        price_text = f" price {price}" if pd.notna(price) else ""
        index.append({
            "title": f"{product} ({company})",
            "source": "Persisted catalogue/price records",
            "result_id": f"product:{record.get('id')}",
            "priority": 5 if record.get("record_type") == "price" else 4,
            "score_hint": 20,
            "text": (
                f"{company} {product} {record.get('sku', '')} {record.get('brand', '')} "
                f"{record.get('product_category', '')} {record.get('specification', '')} "
                f"{record.get('pack_size', '')} {record.get('unit', '')} "
                f"{record.get('description', '')}{price_text}"
            ),
            "vendor": company or "Not found in available vendor data",
            "product": product,
            "sku": str(record.get("sku") or ""),
            "brand": str(record.get("brand") or ""),
            "specification": str(record.get("specification") or ""),
            "pack_size": str(record.get("pack_size") or ""),
            "unit": str(record.get("unit") or ""),
            "gst": record.get("gst"),
            "source_file": str(record.get("source_file") or ""),
            "source_sheet": str(record.get("source_sheet") or ""),
            "source_page": str(record.get("source_page") or ""),
            "price": price,
            "document_id": int(record.get("document_id")) if pd.notna(record.get("document_id")) else None,
        })
    invoices, invoice_warnings = vendor_ai_purchase_invoices(store)
    index.extend(invoices)
    warnings.extend(invoice_warnings)
    for item in index:
        item["_normalized_text"] = vendor_ai_normalize(f"{item['title']} {item['text']}")
    st.session_state["vendor_ai_index_warnings"] = warnings
    st.session_state["vendor_ai_index_cache"] = {"signature": signature, "index": index}
    return index


def vendor_ai_search(store, question: str) -> list[dict]:
    """Return ranked results, reusing both the normalized index and exact query results."""
    signature = vendor_ai_signature(store)
    cache = st.session_state.setdefault("vendor_ai_result_cache", {})
    cache_key = (signature, vendor_ai_normalize(question))
    if cache_key in cache:
        return cache[cache_key]
    from search_sources import results as shared_results
    extra, warnings = shared_results(store, question)
    results = vendor_ai_rank(question, [*vendor_ai_index(store), *extra])
    st.session_state["vendor_ai_index_warnings"] = [*st.session_state.get("vendor_ai_index_warnings", []), *warnings]
    cache[cache_key] = results
    # Keep session memory bounded while retaining the newest searches.
    if len(cache) > 32:
        del cache[next(iter(cache))]
    return results


def vendor_ai_rank(question: str, index: list[dict]) -> list[dict]:
    terms = vendor_ai_terms(question)
    ignored = {"show", "find", "search", "please", "give", "me", "the", "all", "what", "which", "is", "are", "of", "for", "in", "and", "does", "have", "has", "do", "sell", "sells"}
    required = set(vendor_ai_tokens(question)) - ignored
    if not required:
        return []
    groups = []
    for token in required:
        aliases = next(({key, *values} for key, values in VENDOR_AI_SYNONYMS.items() if token == key or token in values), {token})
        groups.append(aliases)
    question_text = vendor_ai_normalize(question)
    ranked = []
    for item in index:
        haystack = item.get("_normalized_text") or vendor_ai_normalize(f"{item['title']} {item['text']}")
        matches = lambda term: re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", haystack) is not None
        if not all(any(matches(term) for term in group) for group in groups):
            continue
        score = 0
        for term in terms:
            if matches(term):
                score += 8 if " " in term else 4
        if question_text and question_text in haystack:
            score += 24
        if item["title"].lower() == question_text:
            score += 30
        if any(company in haystack for company in vendor_ai_tokens(question_text)[:3]):
            score += 12
        score += item["priority"] * 5 + item["score_hint"]
        if score > 0:
            ranked.append({**item, "score": score})
    ranked.sort(key=lambda row: (-row["score"], row["priority"], row["title"].lower()))
    unique = []
    seen = set()
    for item in ranked:
        key = item.get("result_id") or (
            item.get("source", ""),
            item.get("vendor", ""),
            item.get("product", item.get("title", "")),
            item.get("sku", ""),
            item.get("source_file", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _display_value(value, fallback: str = "Not found") -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return fallback
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:180] if text else fallback


def vendor_ai_source_payload(store, item: dict) -> bytes | None:
    if item.get("document_id"):
        return store.read_bytes(int(item["document_id"]))
    if not item.get("invoice_id"):
        return None
    with store.connection() as db:
        if store.cloud:
            column = db.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() "
                "AND table_name=? AND column_name='original_payload'",
                ("vdd_purchase_invoices",),
            ).fetchone()
        else:
            columns = db.execute("PRAGMA table_info(vdd_purchase_invoices)").fetchall()
            column = any(row["name"] == "original_payload" for row in columns)
        if not column:
            return None
        row = db.execute(
            "SELECT original_payload FROM vdd_purchase_invoices WHERE id=?",
            (item["invoice_id"],),
        ).fetchone()
    return bytes(row["original_payload"]) if row and row["original_payload"] is not None else None


def vendor_ai_snippet(item: dict, question: str, limit: int = 260) -> str:
    text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
    positions = [text.casefold().find(term.casefold()) for term in vendor_ai_tokens(question)]
    positions = [position for position in positions if position >= 0]
    start = max(0, min(positions) - 80) if positions else 0
    snippet = text[start:start + limit]
    return ("…" if start else "") + snippet + ("…" if start + limit < len(text) else "")


def vendor_ai_results_excel(results: list[dict], question: str) -> bytes:
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell

    headers = [
        "Type", "Title", "Vendor / Supplier", "Product / Items", "SKU",
        "Invoice Number", "Invoice Date", "Invoice Total", "Source File", "OCR / Text Snippet",
    ]
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Search Results")
    sheet.freeze_panes = "A2"
    sheet.append([WriteOnlyCell(sheet, value=value) for value in headers])
    for item in results:
        values = [
            item.get("source", ""),
            item.get("title", ""),
            item.get("vendor", ""),
            item.get("product", ""),
            item.get("sku", ""),
            item.get("invoice_number", ""),
            item.get("invoice_date", ""),
            item.get("invoice_total", ""),
            item.get("source_file", ""),
            vendor_ai_snippet(item, question),
        ]
        cells = [WriteOnlyCell(sheet, value=str(value or "")) for value in values]
        for cell in cells:
            cell.data_type = "s"
        sheet.append(cells)
    sheet.auto_filter.ref = f"A1:J{len(results) + 1}"
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def vendor_ai_search_all_page(store):
    st.subheader("Search all data")
    st.caption("Search uploaded documents, purchase invoices, original invoice worksheets, indexed Drive rows and catalogue products. Scans use OCR when Tesseract is available; incomplete reads are listed below.")
    if st.button("Refresh search index", key="refresh_search_index"):
        vendor_ai_extract_text.clear()
        for key in ("vendor_ai_index_cache", "vendor_ai_result_cache", "vendor_ai_all_results"):
            st.session_state.pop(key, None)
    query = st.text_input(
        "Search all records",
        placeholder="Search a supplier, invoice, product, SKU, or text in an uploaded file...",
        key="vendor_ai_all_query",
    )
    if st.button("Search all", type="primary", key="vendor_ai_all_search"):
        if query.strip():
            with st.spinner("Searching records and extracting document text..."):
                st.session_state["vendor_ai_all_results"] = vendor_ai_search(store, query.strip())
                st.session_state["vendor_ai_all_last_query"] = query.strip()
    last_query = st.session_state.get("vendor_ai_all_last_query", "")
    if last_query:
        st.session_state["vendor_ai_all_results"] = vendor_ai_search(store, last_query)
    results = st.session_state.get("vendor_ai_all_results", [])
    last_query = st.session_state.get("vendor_ai_all_last_query", "")
    if not last_query:
        st.info("Enter a search term to find matches across all saved data.")
        return
    if results:
        st.caption(f"{len(results)} matching result(s)")
        rows = [{
            "Type": item.get("source", ""),
            "Title": item.get("title", ""),
            "Vendor / Supplier": item.get("vendor", ""),
            "Product / Items": item.get("product", ""),
            "SKU": item.get("sku", ""),
            "Invoice Number": item.get("invoice_number", ""),
            "Invoice Date": item.get("invoice_date", ""),
            "Invoice Total": item.get("invoice_total", ""),
            "Source File": item.get("source_file", ""),
            "Sheet / Row": " · ".join(filter(None, [item.get("source_sheet", ""), str(item.get("source_page", ""))])),
            "Review status": item.get("review_status", ""),
            "OCR / Text Snippet": vendor_ai_snippet(item, last_query),
        } for item in results]
        source_options = ["All sources", *sorted({item.get("source", "") for item in results})]
        source = st.selectbox("Filter by source", source_options, key="all_search_source_filter")
        visible = rows if source == "All sources" else [row for row in rows if row["Type"] == source]
        st.dataframe(pd.DataFrame(visible), hide_index=True, width="stretch")
        drive_links = {item["source_url"]: item.get("source_file", "Document") for item in results
                       if item.get("source_url", "").startswith("https://drive.google.com/")}
        for url, name in drive_links.items():
            st.link_button("Open Drive source: " + name, url)
        st.download_button(
            "Export all matches to Excel",
            data=vendor_ai_results_excel(results, last_query),
            file_name="all_search_results.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="vendor_ai_all_excel",
        )
        source_labels = {
            f"{index + 1}. {item.get('source', '')} · {item.get('source_file') or item.get('title', '')}": index
            for index, item in enumerate(results)
            if item.get("document_id") or item.get("invoice_id")
        }
        if source_labels:
            selected_label = st.selectbox(
                "Preview or download a source file",
                list(source_labels),
                key="vendor_ai_all_source",
            )
            item = results[source_labels[selected_label]]
            payload = vendor_ai_source_payload(store, item)
            if payload:
                filename = str(item.get("source_file") or "source-file")
                st.download_button(
                    "Download original file",
                    data=payload,
                    file_name=filename,
                    mime=mimetypes.guess_type(filename)[0] or "application/octet-stream",
                    key=f"vendor_ai_source_download_{item.get('document_id') or item.get('invoice_id')}",
                )
                show_document_preview(filename, payload)
            elif item.get("invoice_id"):
                st.warning("The invoice record is searchable, but its original file is not available. Older invoices may need to be re-uploaded.")
            else:
                st.warning("The original uploaded file is no longer available for preview or download.")
    else:
        st.info(f"No records matched “{last_query}”.")
    warnings = st.session_state.get("vendor_ai_index_warnings", [])
    if warnings:
        with st.expander(f"{len(warnings)} file(s) could not be fully indexed"):
            for warning in warnings[:50]:
                st.warning(warning)
            if len(warnings) > 50:
                st.caption(f"{len(warnings) - 50} additional warning(s) not shown.")


def vendor_ai_format(question: str, results: list[dict]) -> str:
    if not results:
        return (
            "There is no matching vendor, document, catalogue, or price record in this dashboard yet. "
            "Upload vendor files first; then I can search the saved catalogue and price records. "
            "Use the Web search shortcut below for public company information."
        )
    lines = ["### Internal vendor results"]
    if any(item["source"] == "Persisted catalogue/price records" for item in results):
        if any(token in vendor_ai_normalize(question) for token in ("compare", "cheapest", "price", "prices", "cost", "rate")):
            lines = ["### Internal price comparison"]
    for i, item in enumerate(results[:5], 1):
        lines.extend([
            f"**{i}. {_display_value(item.get('product') or item.get('title'))}**",
            f"- **Vendor:** {_display_value(item.get('vendor'))}",
        ])
        if item["source"] == "Persisted catalogue/price records":
            lines.extend([
                f"- **Brand:** {_display_value(item.get('brand'))}",
                f"- **Model / code:** {_display_value(item.get('sku'))}",
                f"- **Specification:** {_display_value(item.get('specification'))}",
                f"- **Pack / unit:** {_display_value(item.get('pack_size') or item.get('unit'))}",
                f"- **Price:** {_display_value(item.get('price'))}",
                f"- **GST:** {_display_value(item.get('gst'))}",
                f"- **Source:** {_display_value(item.get('source_file'))}",
                f"- **Sheet / page:** {_display_value(item.get('source_sheet'))} / {_display_value(item.get('source_page'))}",
            ])
        else:
            lines.extend([
                f"- **Source:** {_display_value(item.get('source_file'))}",
                f"- **Details:** {_display_value(item.get('text'))}",
            ])
    lines.append(f"\n**Found:** {len(results[:5])} matching records  ")
    lines.append(f"**Vendors:** {len({item.get('vendor') for item in results[:5] if item.get('vendor')})}  ")
    lines.append(f"**Sources searched:** {len({item.get('source_file') for item in results[:5] if item.get('source_file')})}")
    if "contact" in vendor_ai_normalize(question) or "details" in vendor_ai_normalize(question):
        lines.append("Tip: review the vendor company records and any uploaded price lists or contact files for phone, email, address and website details.")
    return "\n".join(lines)


def vendor_ai_page():
    from hospkart_chat.vendor_assistant import render_assistant
    render_assistant(store)


def _store_vendor_ai_search(query: str, response: str, results: list[dict]):
    """Persist the assistant result for this Streamlit session, newest first on display."""
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    entry = {
        "timestamp": timestamp,
        "query": query,
        "response": response,
        "result_count": len(results),
        "vendor_count": len({item.get("vendor") for item in results if item.get("vendor")}),
    }
    st.session_state.setdefault("vendor_ai_search_history", []).append(entry)
    st.session_state.setdefault("vendor_ai_conversation", [])
    st.session_state["vendor_ai_conversation"].extend([
        {"role": "user", "content": query, "timestamp": timestamp},
        {"role": "assistant", "content": response, "timestamp": timestamp},
    ])


def reset_filters():
    for key in list(st.session_state):
        if key in {"company_search", "status_filter", "company_selection", "last_row_selection"} or str(key).startswith(("export_", "zip_")):
            del st.session_state[key]
    # Clear the current dashboard view without deleting persisted vendor data.
    st.session_state["view_cleared"] = True
    st.session_state["notice"] = "Dashboard view cleared. Summary counters are 0; your saved companies and documents are still stored."


def show_saved_data():
    st.session_state["view_cleared"] = False
    st.session_state["notice"] = "Saved dashboard data is visible again."


def go_to(page_name: str):
    # Never mutate the radio widget key after it has been instantiated in the same run.
    st.session_state["pending_page"] = page_name


def open_checklist():
    go_to("Companies & documents")
    st.session_state["view_cleared"] = False


def open_upload():
    go_to("Upload documents")
    st.session_state["view_cleared"] = False


def open_data_reset():
    go_to("Data & backups")
    st.session_state["reset_request"] = True


def refresh(message: str):
    load_workspace_data.clear()
    clear_document_views()
    st.session_state["view_cleared"] = False
    st.session_state["notice"] = message
    for key in ("vendor_ai_index_cache", "vendor_ai_result_cache", "vendor_ai_all_results", "vendor_ai_all_signature"):
        st.session_state.pop(key, None)
    for key in list(st.session_state):
        if str(key).startswith(("export_", "zip_", "backup_download")):
            del st.session_state[key]
    st.rerun()


def execute_delete_all_data():
    """Run the destructive reset as a widget callback, before the next Streamlit rerun."""
    require_admin()
    acknowledged = bool(st.session_state.get("reset_acknowledged"))
    confirmation = str(st.session_state.get("reset_confirmation", "")).strip()
    if not acknowledged or confirmation != "RESET HISTORY":
        st.session_state["reset_error"] = "Tick the confirmation box and type RESET HISTORY exactly."
        return
    try:
        store.reset_data("RESET HISTORY")
        load_workspace_data.clear()
        status = store.reset_status()
        if not status.get("clean", False):
            raise RuntimeError("Some dashboard data is still present after reset.")
        st.session_state["view_cleared"] = False
        st.session_state["pending_page"] = "Companies & documents"
        st.session_state["reset_completed"] = True
        st.session_state["notice"] = "Delete All Data complete. Companies, documents, history, backups and uploaded ZIP files are empty. All four counters are 0."
        st.session_state.pop("reset_error", None)
        for key in list(st.session_state):
            if key in {"last_upload", "expected_count", "company_search", "company_selection", "status_filter", "uploaded_zip_selection"} or str(key).startswith(("export_", "zip_", "backup_download")):
                st.session_state.pop(key, None)
    except Exception as error:
        logging.getLogger(__name__).exception("Delete All Data failed")
        st.session_state["reset_error"] = str(error) if isinstance(error, (ValueError, RuntimeError, OSError)) else "Delete All Data failed. Close any open files and retry."


def clear_manual_catalogue_import():
    for key in ("manual_catalogue_import_token", "manual_catalogue_import_result", "manual_catalogue_import_error", "manual_catalogue_job"):
        st.session_state.pop(key, None)


@st.fragment(run_every='2s')
def manual_import_progress():
    state = catalogue_jobs.status(st.session_state.get('manual_catalogue_job'))
    if not state:
        return
    if state['status'] == 'running':
        st.info(f"Import running: {state.get('processed', 0):,} catalogue candidate rows processed · {state['phase']}. You can browse while it works.")
        st.caption('Rows become available after the database transaction completes. A server restart rolls back unfinished changes; retrying skips previously saved rows.')
    elif state['status'] == 'complete' and not st.session_state.get('manual_catalogue_import_result'):
        st.session_state['manual_catalogue_import_result'] = state['result']
        refresh('Workbook import completed and saved.')
    elif state['status'] == 'failed' and not st.session_state.get('manual_catalogue_import_error'):
        st.session_state['manual_catalogue_import_error'] = state['error']
        st.rerun()


# A reset callback runs before the app reruns, so clear its widget state here before
# the reset widgets are instantiated again.
if st.session_state.pop("reset_completed", False):
    st.session_state.pop("reset_confirmation", None)
    st.session_state.pop("reset_acknowledged", None)


# Apply requested navigation before the sidebar widgets are instantiated.
pending_page = st.session_state.pop("pending_page", None)
if st.query_params.get('view') == 'served-invoices' and not st.session_state.get('served_deep_link_applied'):
    pending_page = 'Purchase Invoice'
    st.session_state['served_deep_link_applied'] = True
if st.query_params.get("assistant") == "1" and not st.session_state.get("assistant_deep_link_applied"):
    pending_page = "🤖 Vendor AI Assistant"
    st.session_state["assistant_deep_link_applied"] = True

with st.sidebar:
    st.markdown('<div class="brand"><img src="/static/hospkart-logo.png" alt="HOSPkart.com"></div>', unsafe_allow_html=True)
    navigation = ["Companies & documents", "Catalogue & prices", "Upload documents", "Uploaded ZIPs", "Review files", "Google Drive files", "Search all", "🤖 Vendor AI Assistant", "Purchase Invoice", "Data & backups"]
    page_map = {item: item for item in navigation}
    captions = [
        "Yes / No checklist + company downloads",
        "Extracted products and prices",
        "Add a new vendor batch",
        "Original ZIP uploads saved here",
        "Review or correct any uploaded file",
        "Browse and search all indexed Drive files",
        "Documents, OCR, invoices and products",
        "Natural-language vendor search and company answers",
        "Original served data.xlsx workbook",
        "Backup, restore or reset saved data",
    ]
    if pending_page:
        matching_label = next((label for label, target in page_map.items() if target == pending_page), None)
        if matching_label:
            st.session_state["page"] = matching_label
    elif st.session_state.get("page") not in navigation:
        st.session_state["page"] = navigation[0]
    page_label = st.radio(
        "Navigate",
        navigation,
        key="page",
        captions=captions,
        format_func=lambda label: {"🤖 Vendor AI Assistant": "✦ QuoteSaarthi", "Purchase Invoice": "Served invoices"}.get(label, label),
    )
    page = page_map[page_label]

if not can_edit:
    st.caption("View only · Browse, search and download. Admin sign in is available through the A icon on the main dashboard.")
    st.link_button("Admin account ↗", "/")
if page in {"Upload documents", "Review files", "Data & backups"}:
    if not can_edit:
        loading_placeholder.empty()
    require_admin()

if page == 'Purchase Invoice':
    loading_placeholder.empty()
    from served_invoices_ui import render_served_invoices
    render_served_invoices(store)
    st.stop()

startup_status.info("Loading companies and documents…")
from drive_view import inventory, render_drive_records
try:
    drive_identity = hashlib.sha256((str(store.data_dir)+store.database_url).encode()).hexdigest()
    snapshot = load_workspace_data(drive_identity, page not in {"Catalogue & prices", "Google Drive files"}, store)
    vendors, documents = snapshot["vendors"], snapshot["documents"]
    uploaded_documents, aliases = snapshot["uploaded_documents"], snapshot["aliases"]
    checklist, counts = snapshot["checklist"], snapshot["counts"]
    if snapshot["drive_failed"]:
        load_workspace_data.clear()
        st.warning('Drive references could not be loaded. Uploaded companies are shown; refresh to retry.')
except Exception as error:
    logging.getLogger(__name__).error("Workspace metadata load failed: %s", type(error).__name__)
    startup_status.error("Could not load workspace data")
    st.error("Saved data could not be loaded. Please retry in a moment.")
    st.button("Retry loading workspace", key="retry_workspace_load")
    st.stop()
view_cleared = bool(st.session_state.get("view_cleared", False))
display_counts = {k: 0 for k in counts} if view_cleared else counts
logging.getLogger("organizer.application").info("Vendor workspace data loaded in %.3fs", time.perf_counter() - workspace_started)
startup_status.success("Workspace ready")
loading_placeholder.empty()

header, upload_col, checklist_col, clear_col, reset_col = st.columns([4.0, 1.25, 1.4, 1.15, 1.35], vertical_alignment="center")
dashboard_title = header.empty()
dashboard_title.title("Vendor Document Dashboard")
from app.vendor_zip_upload import issue_upload_token, upload_signing_key
with st.sidebar:
    with st.expander('Workspace tools'):
        st.link_button('Upload purchase invoice / Excel', '/purchase-invoices#'+issue_upload_token(upload_signing_key()))
        if setting('GOOGLE_DRIVE_FOLDER_URL') or setting('GOOGLE_DRIVE_FOLDER_ID'):
            st.link_button('Search all Google Drive data', '/drive-library#'+issue_upload_token(upload_signing_key()))
upload_col.button("Upload documents", disabled=not can_edit, key="open_upload", on_click=open_upload, width="stretch", help="Upload a company-folders ZIP or document files.")
checklist_col.button("Yes / No checklist", key="open_checklist", on_click=open_checklist, width="stretch", help="Open the company-wise document checklist and Excel export.")
if view_cleared:
    clear_col.button("Show saved data", key="show_saved_data", on_click=show_saved_data, width="stretch", help="Show the saved companies and documents again.")
else:
    clear_col.button("Clear search", key="reset_filters", on_click=reset_filters, width="stretch", help="Clear the dashboard view and show 0 summary counters without deleting saved data.")
reset_col.button("Reset all history", disabled=not can_edit, key="open_reset", on_click=open_data_reset, width="stretch", help="Delete ALL dashboard data, history, backups and uploaded ZIP files. The dashboard returns to 0 / 0 / 0 / 0.")
if notice := st.session_state.pop("notice", None):
    st.success(notice)

if page == "Companies & documents":
    st.markdown('''<section class="workspace-hero" aria-label="QuoteSaarthi workspace">
<div class="hero-eyebrow">QuoteSaarthi · Futuristic intelligence dashboard</div>
<h2>Your data. A clearer way forward.</h2>
<p>Explore company documents, find products and compare recorded prices with an assistant connected to your saved workspace.</p>
<div class="hero-badges"><span>Company-wise records</span><span>Source-backed search</span><span>Team-protected workspace</span></div>
</section>''', unsafe_allow_html=True)
    st.button("✦ Ask QuoteSaarthi", key="ask_quotesaarthi", on_click=partial(go_to, "🤖 Vendor AI Assistant"))

if page not in {"🤖 Vendor AI Assistant", "Catalogue & prices"}:
    st.markdown('<div class="summary-label">Workspace overview</div>', unsafe_allow_html=True)
    m1, m2, m3, m4 = st.columns(4)
    from app.served_workbook import served_invoice_count
    m1.metric("Served data invoices", f"{served_invoice_count(store):,}", help="Distinct non-blank invoice numbers in Client Call List of the latest served workbook. Each invoice counts once across repeated item rows.")
    m2.metric("Stored documents", f"{display_counts['stored_files']:,}", help="Available uploaded files and Drive references. Drive originals remain in Drive. Known identical content within a company counts once.")
    m3.metric("Vendor headcount", f"{display_counts['companies']:,}", help="Unique companies across saved vendor records and assigned documents. Multiple files do not increase headcount.")
    m4.metric("Needs review", f"{display_counts['review_files']:,}", help="Unassigned, unclassified or unavailable document files.")
    document_type_counts = {
        document_type: int(documents.loc[
            documents.available & documents.types.map(lambda types: document_type in types),
            "company_key",
        ].nunique())
        for document_type in DOCUMENT_TYPES
    }
    if view_cleared:
        document_type_counts = {document_type: 0 for document_type in DOCUMENT_TYPES}
    st.markdown('<div class="type-label">Document coverage</div>', unsafe_allow_html=True)
    for start in range(0, len(DOCUMENT_TYPES), 5):
        type_columns = st.columns(5)
        for column, document_type in zip(type_columns, DOCUMENT_TYPES[start:start + 5]):
            column.metric(
                document_type,
                f"{document_type_counts[document_type]:,}",
                help=f"Unique companies with at least one available {document_type} file. Multiple files for one company count once.",
            )
    if view_cleared:
        st.info("The vendor document view is cleared. Saved documents and original workbooks are kept; the Served data invoices card still counts unique invoices. Click **Show saved data** to restore the vendor summary.")
    else:
        st.caption("Served data invoices counts unique invoice numbers in the latest served workbook. Headcount is unique companies; document totals and coverage describe vendor documents.")

if page == "Upload documents":
    st.subheader("Upload vendor documents")
    st.write("Upload documents from your computer, or index the connected Google Drive collection. Drive originals stay in Drive; extracted records are searchable by everyone with dashboard access.")
    connected_drive = setting("GOOGLE_DRIVE_FOLDER_URL") or setting("GOOGLE_DRIVE_FOLDER_ID")
    if connected_drive:
        st.caption("Google Drive source folder is configured for Organizer and Vendor.")
    direct_zip = "Upload ZIP from computer"
    drive_library = "Google Drive library (10+ GB ZIPs)"
    import_sources = [direct_zip, drive_library, "Upload small files"]
    hosted = setting("CLOUD_DEPLOYMENT").strip().lower() in {"1", "true", "yes", "on"}
    if not hosted:
        import_sources.append("Local ZIP path (large files)")
    if st.session_state.get("import_source") == "Google Drive ZIP (large files)":
        st.session_state["import_source"] = drive_library
    elif st.session_state.get("import_source") in {"Upload ZIP directly (large files)", "Upload ZIP directly (up to 2 GB)"}:
        st.session_state["import_source"] = direct_zip
    elif st.session_state.get("import_source") not in import_sources:
        st.session_state["import_source"] = import_sources[1] if connected_drive else import_sources[0]
    source_mode = st.radio("Import source", import_sources, horizontal=True, key="import_source")
    streamed_upload = hosted and source_mode == direct_zip
    indexing_drive = source_mode == drive_library
    local_zip_path = ""
    uploads = []
    if source_mode == "Local ZIP path (large files)":
        local_zip_path = st.text_input("ZIP file path on the computer running this app", placeholder=r"C:\Users\you\Downloads\vendors.zip")
        st.caption("Use a ZIP already on the computer running this app. To send a ZIP from your device, choose Upload ZIP directly.")
    elif indexing_drive:
        st.info("Paste a Google Drive folder or ZIP file link below, then click Use link and start indexing. Originals stay in Drive; searchable records are shared. There is no total collection-size cap; processing and database capacity still apply.")
        if connected_drive:
            from app.config import parse_drive_folder_id
            folder_url = 'https://drive.google.com/drive/folders/' + parse_drive_folder_id(connected_drive)
            st.link_button("1. Open connected Drive folder and add your ZIP", folder_url)
        library_url = '/drive-library#' + issue_upload_token(upload_signing_key())
        st.link_button("Open Drive library in a new tab", library_url)
        st.caption("Searchable records are shared in PostgreSQL; originals remain in Drive. Drive companies, file references and extracted rows also appear on Companies & documents and Catalogue & prices.")
        st.iframe(library_url, height=1000)
    else:
        if streamed_upload:
            if hint := st.session_state.pop("local_zip_hint", ""):
                st.info(f"{hint} is on your computer. Choose it in the file picker on the ZIP upload page.")
            st.caption("Direct ZIP uploads have no fixed 2 GB cap, but must fit available server space. For the full collection, use a Google Drive link to keep originals in Drive and index shared records.")
        elif source_mode == direct_zip:
            st.caption("Click Browse files or drag your ZIP here. Wait for the upload to finish, then click Save documents. Keep this tab open during import.")
        else:
            st.caption("Choose individual documents from your device. For company-folder ZIPs, choose Upload ZIP directly.")
        if not streamed_upload:
            upload_types = ["zip"] if source_mode == direct_zip else ["zip"]+sorted(e.lstrip(".") for e in ALLOWED_EXTENSIONS)
            uploads = st.file_uploader("Choose ZIP files" if source_mode == direct_zip else "Choose document files", type=upload_types, accept_multiple_files=True, key="document_uploads")
    single_company, use_pdf, expected = '', False, 0
    if not indexing_drive:
        with st.expander("Optional settings"):
            single_company = st.text_input("Company for loose files only", key="loose_company", help="Leave blank for company-folder ZIPs. This assigns every selected file to one company.")
            use_pdf = st.checkbox("Read PDF headings for unclear filenames", value=False, help="Optional local text extraction. No OCR or external service. Scans can still need review.")
            expected = st.number_input("Expected company count (0 = not specified)", min_value=0, value=0, step=1)
    if streamed_upload:
        from app.vendor_zip_upload import issue_upload_token, upload_signing_key
        upload_token = issue_upload_token(upload_signing_key())
        upload_url = "/vendor-upload?source=computer&company=" + quote_plus(single_company) + "&read_pdf=" + str(use_pdf).lower() + "&expected=" + str(int(expected)) + "#" + upload_token
        st.link_button("Choose ZIP files", upload_url, type="primary", width="stretch")
    if not streamed_upload and not indexing_drive and st.button("Save documents", key="save_documents", disabled=not (uploads or local_zip_path.strip()), type="primary", width="stretch"):
        progress = st.empty()
        with st.spinner("Reading folders and saving documents..."):
            try:
                def show_progress(i, path):
                    if i == 1 or i % 10 == 0:
                        progress.caption(f"Saving file {i}: {path.rsplit('/', 1)[-1]}")
                if local_zip_path.strip():
                    local_path = Path(local_zip_path.strip().strip('"'))
                    if not local_path.is_file() or local_path.suffix.lower() != ".zip":
                        raise ValueError("Choose an existing .zip on the computer running this app, or use Upload ZIP directly to select a ZIP from your device.")
                    with local_path.open("rb") as local_file:
                        result = import_documents(store, [DiskUpload(local_file, local_path.name)], single_company, use_pdf, show_progress, retain_archive=False)
                else:
                    result = import_documents(store, uploads, single_company, use_pdf, show_progress)
            except Exception as error:
                st.error(str(error) if isinstance(error, ValueError) else "Import interrupted. Check the Drive download permission, server disk space and connection, then retry. Previously saved documents remain available.")
                st.stop()
        progress.empty()
        st.session_state["last_upload"] = result.to_dict()
        st.session_state["expected_count"] = int(expected)
        refresh(f"Saved {result.saved_files} new documents. {result.duplicate_files} repeats skipped. {result.detected_companies} companies identified in this upload. Total vendor head count is now {result.total_companies}.")
    last = st.session_state.get("last_upload")
    if not last:
        last = next((e["details"] for e in store.history() if e["action"] == "Document upload"), None)
    if last:
        for note in last.get("notes", []):
            st.info(note)
        st.subheader("Last upload summary")
        summary = pd.DataFrame([{
            "Companies in upload": last["detected_companies"], "Total vendor head count": last.get("total_companies", len(vendors)),
            "Files processed": last["processed_files"], "New documents": last["saved_files"],
            "Repeated files": last["duplicate_files"], "Catalogue rows awaiting approval": last.get("catalogue_records_pending", 0),
            "Repeated catalogue rows": last.get("duplicate_catalogue_rows", 0), "Skipped / failed": len(last["issues"])}])
        show_table(summary)
        st.caption("Source: " + last["source"] + ". Vendor head count is cumulative and unique: existing vendors are counted once, and new vendor names from later ZIPs are added automatically.")
        if last.get("catalogue_records_pending", 0):
            st.info("Extracted catalogue and price rows are held for approval in the Organizer dashboard before they appear in product search or Vendor AI Assistant.")
        expected_count = st.session_state.get("expected_count", 0)
        if expected_count and expected_count != last["detected_companies"]:
            st.warning(f"You expected {expected_count} companies; this upload identified {last['detected_companies']}. Check the company list below. Counts are not padded.")
        with st.expander(f"Show {last['detected_companies']} company names from this upload"):
            show_table(pd.DataFrame({"No.": range(1,len(last["company_names"])+1), "Company Name": last["company_names"]}))
        if last["issues"]:
            st.warning("Some files were skipped or could not be saved. Review these before treating the batch as complete.")
            show_table(pd.DataFrame(last["issues"]))
            failed = store.failed_files()
            if not failed.empty:
                st.caption("Files that reached processing but failed are retained separately for review.")
                show_table(failed)
    with st.expander("Optional: import a company list from Excel"):
        master = st.file_uploader("Company list", type=["xlsx", "csv"], key="vendor_master")
        st.caption("Use a Company Name or Vendor Name column. A Yes in this list is not evidence of an uploaded document.")
        if st.button("Save company list", disabled=master is None):
            try:
                count = store.upsert_vendors(read_vendor_file(master))
                refresh(f"Saved or matched {count} company names.")
            except Exception as error:
                st.error(str(error) if isinstance(error, ValueError) else "Company list could not be imported.")

elif page == "Companies & documents":
    if view_cleared:
        st.subheader("Dashboard view cleared")
        st.write("Search results and summary counters are hidden. Your saved data has not been deleted.")
        st.button("Show saved data", key="show_saved_data_main", on_click=show_saved_data, type="primary")
        st.stop()
    if st.button('Refresh companies and documents', key='refresh_shared_companies'):
        inventory.clear()
        load_workspace_data.clear()
        st.rerun()
    st.caption("Use Refresh companies and documents to include newly uploaded files.")
    st.subheader("Find a company")
    col1, col2 = st.columns([3, 1])
    search = col1.text_input("Search company", placeholder="Type a company name...", key="company_search")
    status = col2.selectbox("Show", ["All companies", "Missing documents", "All types available", "Needs review"], key="status_filter")
    filtered = filter_checklist(checklist, search, status)
    scoped = documents[documents.company_key.isin(filtered.company_key)]
    st.caption(f"Showing {len(filtered)} of {len(checklist)} companies | {int(scoped.available.sum())} stored documents in these results")
    if filtered.empty:
        st.info("No company matches. Use Clear search to remove filters." if len(checklist) else "Start with Upload documents in the left menu. Your company folders will appear here.")
    else:
        st.markdown("### Yes / No document checklist")
        view = filtered.drop(columns=["company_key", "canonical_id"], errors="ignore").copy()
        view.insert(0, "No.", range(1, len(view) + 1))
        view["Completion"] = view["Completion"].map(lambda v: f"{v:.0%}")
        show_table(view)
        st.caption("Yes = a classified uploaded file or Drive reference is available for that company. Drive originals stay in Drive; extraction issues remain marked for review. No = no classified file was found in that category.")
        checklist_dl1, checklist_dl2 = st.columns(2)
        visible_excel_name = export_filename(filtered.iloc[0]["Company Name"], "Checklist", "xlsx") if len(filtered) == 1 else "All_Vendors_Checklist.xlsx"
        visible_csv_name = export_filename(filtered.iloc[0]["Company Name"], "Checklist", "csv") if len(filtered) == 1 else "All_Vendors_Checklist.csv"
        checklist_dl1.download_button("Download visible checklist - Excel", partial(workbook_bytes, filtered, scoped, True, aliases), visible_excel_name, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", width="stretch", key="visible_excel", on_click="ignore")
        checklist_dl2.download_button("Download visible checklist - CSV", csv_bytes(view), visible_csv_name, "text/csv", width="stretch", key="visible_csv")
        st.divider()
        st.markdown("### Company documents")
        st.caption("Select a company, open a document category, then preview, review or delete an uploaded file using the controls beside its name.")
        options = filtered.company_key.tolist()
        mapping = dict(zip(filtered.company_key, filtered["Company Name"]))
        if st.session_state.get("company_selection") not in options:
            st.session_state["company_selection"] = options[0]
        selected = st.selectbox("Select company", options, format_func=mapping.get, key="company_selection")
        company = mapping[selected]
        dashboard_title.title(company)
        company_docs = documents[documents.company_key == selected]
        company_checklist = checklist[checklist.company_key == selected]
        signature = hashlib.sha256((selected + repr(company_docs[["id","types_json","reviewed","available","file_hash"]].to_dict("records"))).encode()).hexdigest()
        with st.container(border=True):
            st.subheader(company)
            row = company_checklist.iloc[0]
            st.caption(f"{int(company_docs.available.sum())} documents saved | {row['Available']} of {len(DOCUMENT_TYPES)} document types available | {row['Needs review']} to review")
            x1,x2 = st.columns(2)
            x1.download_button("Download company Excel", partial(workbook_bytes, company_checklist, company_docs, True, aliases),
                export_filename(company,"Checklist","xlsx"), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", width="stretch", key="company_excel", on_click="ignore")
            if x2.button("Prepare all company documents", disabled=company_docs.empty, width="stretch", key="prepare_company_zip"):
                with st.spinner("Preparing this company's files..."):
                    st.session_state["zip_company"] = (signature, store.company_zip(company, company_docs))
            value = st.session_state.get("zip_company")
            if value and value[0] == signature:
                st.download_button("Download " + company + " documents ZIP", value[1], export_filename(company,"Documents","zip"), "application/zip", type="primary", width="stretch", key="company_zip_download")
            st.caption("The company ZIP includes uploaded files, its checklist, and a Drive-links register for Drive originals. Drive archives are opened at their source; use the listed member path to find each document.")
            for category in DOCUMENT_TYPES + ["Other documents"]:
                rows = documents_for_category(company_docs, category)
                available = int(rows["available"].sum())
                yes_no = "Yes" if available else "No"
                label = f"{category}  |  {yes_no}  |  {available} file(s)" if category in DOCUMENT_TYPES else f"Other documents  |  {available} file(s)"
                category_panel = st.expander(label, key=f"company_category_{selected}_{category}", on_change="rerun")
                with category_panel:
                    if not category_panel.open:
                        continue
                    if rows.empty:
                        st.caption("No file available in this category.")
                    for row_number, doc in enumerate(rows.itertuples(index=False)):
                        document_token = f"{category}_{doc.id}_{row_number}"
                        filename_col, preview_col, review_col, delete_col = st.columns([6, 1.2, 1.2, 1.2], vertical_alignment="center")
                        filename_col.text(doc.filename)
                        document_id = str(doc.id)
                        is_stored_document = document_id.isdecimal()
                        if review_col.button("Review", key=f"review_document_{document_token}", disabled=not can_edit or not is_stored_document):
                            st.session_state["review_document_request"] = int(doc.id)
                            st.session_state["review_show_all"] = True
                            go_to("Review files")
                            st.rerun()
                        can_delete = is_stored_document or document_id.startswith("drive:")
                        drive_original = bool(getattr(doc, "drive_url", "")) or document_id.startswith("drive:")
                        delete_help = ("Remove this Drive reference from the dashboard; its original remains in Drive."
                                       if drive_original else
                                       "Delete this saved upload and its extracted rows; uploaded ZIP archives are kept.")
                        if delete_col.button("Delete", key=f"delete_document_{document_token}", disabled=not can_edit or not can_delete, help=delete_help):
                            st.session_state["delete_document_id"] = document_id
                        if str(st.session_state.get("delete_document_id", "")) == document_id:
                            render_dashboard_delete_confirmation(
                                store, document_id, doc.filename, "delete_document_id",
                                f"document_{document_token}", drive_original=bool(getattr(doc, "drive_url", "")),
                            )
                        detail = 'Google Drive' if getattr(doc, 'drive_url', '') else (supporting_category(doc.filename) if not doc.types else doc.method)
                        st.caption(f"{detail or 'Needs review'} | {doc.size_bytes / 1024:,.0f} KB")
                        if getattr(doc, 'drive_url', ''):
                            preview_token = f"{category}_{doc.id}_{row_number}"
                            if preview_col.button("Preview", key=f"preview_{preview_token}", help="Preview this document before downloading."):
                                st.session_state["preview_document_id"] = preview_token
                            if st.session_state.get("preview_document_id") == preview_token:
                                with st.container(border=True):
                                    preview_title, close_preview = st.columns([5, 1])
                                    preview_title.markdown(f"**Preview: {doc.filename}**")
                                    if close_preview.button("Close", key=f"close_{preview_token}"):
                                        st.session_state.pop("preview_document_id", None)
                                        st.rerun()
                                    try:
                                        with st.spinner("Opening original document…"):
                                            payload = drive_preview_bytes(drive_identity, str(doc.id), getattr(doc, 'drive_version', '') or doc.file_hash, store)
                                        from app.drive.originals import PREVIEW_LIMIT
                                        if doc.size_bytes > PREVIEW_LIMIT and Path(doc.filename).suffix.lower() == '.pdf':
                                            st.caption("Large PDF: first-page preview. Download file for the complete original.")
                                        show_document_preview(doc.filename, payload)
                                    except Exception as error:
                                        from app.drive.originals import OriginalUnavailable
                                        st.warning(str(error) if isinstance(error, OriginalUnavailable) else "The Drive original could not be previewed. Please try again.")
                                        st.link_button("Open original in Drive", doc.drive_url, key=f"drive_fallback_{preview_token}")
                            st.link_button("Download file", "/api/drive-index/original/" + str(doc.id), key=f"file_{preview_token}")
                            continue
                        payload = document_bytes(store, doc.id, doc.file_hash) if doc.available else None
                        if payload is not None:
                            preview_token = f"{category}_{doc.id}_{row_number}"
                            preview_key = f"preview_{preview_token}"
                            if preview_col.button("Preview", key=preview_key, help="Preview this document before downloading."):
                                st.session_state["preview_document_id"] = preview_token
                            if st.session_state.get("preview_document_id") == preview_token:
                                with st.container(border=True):
                                    preview_title, close_preview = st.columns([5, 1])
                                    preview_title.markdown(f"**Preview: {doc.filename}**")
                                    if close_preview.button("Close", key=f"close_{preview_token}"):
                                        st.session_state.pop("preview_document_id", None)
                                        st.rerun()
                                    show_document_preview(doc.filename, payload)
                            st.download_button("Download file", payload, doc.filename,
                                mimetypes.guess_type(doc.filename)[0] or "application/octet-stream", key=f"file_{preview_token}")
                        else:
                            st.warning("Original bytes unavailable. Re-upload this file; it does not count as Yes.")
        with st.expander("Document coverage across these companies"):
            import plotly.graph_objects as go
            values=[int((filtered[t]=="Yes").sum()) for t in DOCUMENT_TYPES]
            fig=go.Figure(go.Bar(x=values,y=DOCUMENT_TYPES,orientation="h",text=values,textposition="outside",cliponaxis=False))
            fig.update_layout(title="Companies with each document type",height=355,margin=dict(l=20,r=45,t=55,b=35),showlegend=False)
            fig.update_xaxes(title=f"Companies (out of {len(filtered)})",range=[0,max(1,len(filtered)*1.12)],dtick=max(1,len(filtered)//8))
            fig.update_yaxes(autorange="reversed")
            st.plotly_chart(fig,width="stretch",config={"displayModeBar":False})

elif page == "Catalogue & prices":
    st.subheader('Drive catalogue and price rows')
    catalogue_url = '/drive-library?view=catalogue#'+issue_upload_token(upload_signing_key())
    st.link_button('Open Drive catalogue in a new tab', catalogue_url)
    st.iframe(catalogue_url, height=850)
    st.subheader("Saved catalogue and manual Excel records")
    st.caption("Drive source rows appear in the searchable catalogue above. Saved manual/document rows below include both validated records and rows needing review. Their status is shown explicitly.")
    if can_edit:
        st.caption("Upload your populated manual workbook once. Validated rows are saved automatically alongside extracted catalogue rows in the table, search and Excel export below. You do not need to upload the generated workbook.")
        manual_workbook = st.file_uploader(
            "Upload manual catalogue & prices (.xlsx or .csv)", type=["xlsx", "csv"], max_upload_size=2048,
            key="catalogue_manual_workbook", on_change=clear_manual_catalogue_import,
            help="Excel (.xlsx) and CSV (.csv) are supported. Include Company Name and Product Name columns. Maximum 2 GB and 200,000 scanned product rows. Original files are retained; repeated identical uploads are skipped.",
        )
        retry_import = False
        if manual_workbook is not None:
            interrupted = (
                st.session_state.get("manual_catalogue_import_token") == (MANUAL_CATALOGUE_IMPORT_VERSION, manual_workbook.file_id)
                and not st.session_state.get("manual_catalogue_import_result")
                and not st.session_state.get("manual_catalogue_import_error")
                and not catalogue_jobs.status(st.session_state.get('manual_catalogue_job'))
            )
            if interrupted:
                st.warning("The previous workbook import did not finish. Retry to continue; already saved rows are skipped.")
            if interrupted or st.session_state.get("manual_catalogue_import_error"):
                retry_import = st.button("Retry catalogue import", key="retry_manual_catalogue_import")
        if manual_workbook is not None:
            token = (MANUAL_CATALOGUE_IMPORT_VERSION, manual_workbook.file_id)
            if token != st.session_state.get("manual_catalogue_import_token") or retry_import:
                st.session_state["manual_catalogue_import_token"] = token
                st.session_state.pop("manual_catalogue_import_error", None)
                st.session_state.pop('manual_catalogue_import_result', None)
                st.session_state.pop('manual_catalogue_job', None)
                try:
                    st.session_state['manual_catalogue_job'] = catalogue_jobs.start(
                        store, manual_workbook.name, manual_workbook.getvalue(), retry=True)
                except Exception as error:
                    logging.getLogger(__name__).exception("Manual catalogue import failed")
                    st.session_state["manual_catalogue_import_error"] = (
                        str(error) if isinstance(error, (ValueError, RuntimeError, OSError))
                        else f"Saving failed ({type(error).__name__}). Retry the import; the server log contains the error details."
                    )
            manual_import_progress()
        if st.session_state.get("manual_catalogue_import_error"):
            st.error(f"Could not import the manual workbook: {st.session_state['manual_catalogue_import_error']}")
        result = st.session_state.get("manual_catalogue_import_result")
        if result:
            if not result['added'] and not result['review'] and result['duplicates']:
                st.info("This workbook was already imported. No new rows were added; its saved rows remain in the table and full Excel export below.")
            else:
                st.success(f"Saved {result['added']:,} validated row(s) and {result['review']:,} row(s) needing review.")
            st.caption(f"{result['duplicates']:,} already processed source row(s) skipped; {result['excluded']:,} medicine/drug row(s) excluded from this workbook. Source rows can expand into multiple prices or collapse as exact duplicates.")
    catalogue_counts = store.product_classification_counts()
    st.caption(
        "Medical devices, equipment, diagnostics and consumables only; medicines and drugs are excluded. "
        "Names and price fields are cleaned and checked before display. "
        f"{catalogue_counts['approved']:,} validated row(s) and {catalogue_counts['review']:,} row(s) needing review are saved. "
        "The full Excel export includes review rows with their reasons and available original price text. "
        "Re-importing the same document is safe and does not duplicate rows."
    )
    export_signature = store.search_index_signature()
    st.download_button(
        "Export company-wise catalogue & prices — Excel",
        data=partial(catalogue_download, store, hashlib.sha256((str(store.data_dir) + store.database_url).encode()).hexdigest(), export_signature),
        file_name="Company_Wise_Catalogue_and_Prices.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        icon=":material/download:",
        key="company_wise_catalogue_price_export",
        on_click="ignore",
        help="Includes validated and Needs review rows, with Review Flag, Review reason and source price text. One tab per company; originals are preserved.",
    )
    st.caption("Excel generates when you click download. You can keep browsing while it prepares.")
    search = st.text_input("Search products, SKUs, descriptions or vendors", key="product_search")
    record_type = st.selectbox("Record type", ["All", "catalogue", "price"], key="product_record_type")
    kind = "" if record_type == "All" else record_type
    status_label = st.selectbox("Rows to show", ["All saved rows", "Validated only", "Needs review"], key="product_status")
    status = {"All saved rows":"all", "Validated only":"approved", "Needs review":"review"}[status_label]
    total_records = store.product_record_count(search, kind, status=status)
    st.metric("Saved catalogue records", f"{total_records:,}")
    page_size = 200
    page_count = max(1, (total_records + page_size - 1) // page_size)
    filter_key = (search, kind, status)
    if st.session_state.get("product_page_filter") != filter_key:
        st.session_state["product_page_filter"] = filter_key
        st.session_state["product_page"] = 1
    st.session_state["product_page"] = min(st.session_state.get("product_page", 1), page_count)
    page_number = st.number_input("Page", min_value=1, max_value=page_count, step=1, key="product_page")
    offset = (int(page_number) - 1) * page_size
    records = store.product_records(search, kind, limit=page_size, offset=offset, status=status)
    if total_records:
        st.caption(f"Showing {offset + 1:,}–{offset + len(records):,} of {total_records:,} matching records. Search covers all records; Excel export includes the full catalogue.")
    if records.empty:
        st.info("No extracted catalogue or price rows match this search yet.")
    else:
        product_display_columns = [
            "company_name", "record_type", "product_name", "sku", "brand", "specification",
            "pack_size", "unit", "price", "mrp", "gst", "currency", "source_file",
            "source_sheet", "source_page", "extraction_confidence", "catalogue_status", "classification_reason",
        ]
        # Keep legacy product rows visible while a hot-reloaded Store/schema catches up.
        records = records.reindex(columns=list(dict.fromkeys([*records.columns, *product_display_columns])), fill_value="")
        display = records[product_display_columns].rename(columns={
            "company_name": "Vendor", "record_type": "Type", "product_name": "Product", "sku": "SKU",
            "brand": "Brand", "specification": "Specification", "pack_size": "Pack", "unit": "Unit",
            "price": "Price", "mrp": "MRP", "gst": "GST", "currency": "Currency", "source_file": "Source file",
            "source_sheet": "Sheet", "source_page": "Page", "extraction_confidence": "Confidence",
            "catalogue_status":"Review status", "classification_reason":"Review reason",
        })
        display['Review status'] = display['Review status'].replace({'approved':'Validated', 'review':'Needs review'})
        show_table(display)

elif page == "Google Drive files":
    render_drive_records(store)

elif page == "Uploaded ZIPs":
    upload_archives = store.upload_archives()
    st.subheader("Uploaded ZIP Files")
    st.write("Every original vendor ZIP saved through the dashboard appears here separately. Downloading returns the exact original ZIP bytes and original filename.")
    if upload_archives.empty:
        st.info("No ZIP upload has been archived yet. Go to Upload documents and save a vendor ZIP.")
    else:
        unique_vendors = vendors.drop_duplicates("company_key")
        company_names = unique_vendors.sort_values("company_name", key=lambda s: s.str.casefold()).company_name.tolist()
        st.metric("Company head count", f"{len(company_names):,}", help="Unique companies currently stored in the dashboard across all uploaded ZIP files.")
        st.caption(f"One cumulative count across {len(upload_archives)} uploaded ZIP file(s). A company uploaded in multiple ZIPs is counted once.")

        with st.expander(f"Show {len(company_names)} company names"):
            if company_names:
                show_table(pd.DataFrame({"No.": range(1, len(company_names)+1), "Company Name": company_names}))
            else:
                st.caption("No companies are currently stored.")

        st.markdown("#### Download uploaded ZIP files")
        st.caption("These are source-archive downloads only; their contents are not used as separate company head counts.")
        for row in upload_archives.itertuples(index=False):
            with st.container(border=True):
                c1, c2 = st.columns([3.6, 1.4], vertical_alignment="center")
                c1.markdown(f"**{row.filename}**")
                c1.caption(f"{int(row.processed_files)} files read | {round(int(row.size_bytes)/(1024*1024),1)} MB")
                payload = store.read_upload_archive(row.id)
                if payload is not None:
                    c2.download_button(f"Download {row.filename}", payload, row.filename, "application/zip", type="primary", width="stretch", key=f"download_uploaded_zip_{row.id}")
                else:
                    c2.warning("ZIP unavailable")

elif page == "Review files":
    review_queue = store.review_queue()
    st.subheader("Review documents")
    product_review = store.product_review_records(limit=1000)
    if not product_review.empty:
        st.markdown("### Product rows needing catalogue validation")
        show_table(product_review.rename(columns={
            "company_name": "Company", "record_type": "Type", "product_name": "Product",
            "sku": "SKU", "price": "Price", "currency": "Currency",
            "source_file": "Source file", "source_sheet": "Sheet",
            "source_page": "Row/page", "classification_reason": "Review reason",
        }))
        st.caption("Rows shown here are hidden from Catalogue & prices, product search, and Excel exports until reviewed.")
    if not review_queue.empty:
        st.markdown("### Company match review queue")
        show_table(review_queue[["detected_name", "possible_company", "canonical_id", "confidence", "evidence", "source", "status"]].rename(columns={"detected_name":"Detected name", "possible_company":"Possible company", "canonical_id":"Company ID", "confidence":"Confidence", "evidence":"Evidence", "source":"Source", "status":"Status"}))
        st.caption("These records were not automatically merged because the company match was uncertain.")
    st.caption("Review uploaded and Drive documents. Drive originals stay in Google Drive; deleting a Drive item here removes only its dashboard record.")
    show_all=st.checkbox("Show all documents for correction", value=True, key="review_show_all")
    drive_issues = documents[documents.drive_url.ne('')] if 'drive_url' in documents else pd.DataFrame()
    if not show_all and not drive_issues.empty:
        drive_issues = drive_issues[drive_issues.needs_review | ~drive_issues.available]
    if not drive_issues.empty:
        st.caption('Drive originals remain in Google Drive. Preview, download, or remove only their dashboard references below.')
        show_table(drive_issues[['company_name','filename','drive_status','reason','drive_url']])
    drive_only_issues = drive_issues[drive_issues.id.astype(str).str.startswith("drive:")] if not drive_issues.empty else pd.DataFrame()
    if not drive_only_issues.empty:
        drive_lookup = {
            str(row.id): f"{row.company_name or 'Unassigned'} | {row.filename}"
            for row in drive_only_issues.itertuples(index=False)
        }
        selected_drive_id = st.selectbox(
            "Select a Drive-only file",
            list(drive_lookup),
            format_func=drive_lookup.get,
            key="review_drive_document",
        )
        drive_doc = drive_only_issues[drive_only_issues.id.astype(str) == selected_drive_id].iloc[0]
        drive_token = selected_drive_id.replace(":", "_")
        with st.container(horizontal=True):
            if st.button("Preview", key=f"preview_review_drive_{drive_token}"):
                st.session_state["review_drive_preview_id"] = selected_drive_id
            st.link_button("Open in Drive", drive_doc.drive_url, key=f"open_review_drive_{drive_token}")
            st.link_button("Download original", "/api/drive-index/original/" + selected_drive_id, key=f"download_review_drive_{drive_token}")
            if st.button("Delete from dashboard", key=f"delete_review_drive_{drive_token}"):
                st.session_state["review_drive_delete_id"] = selected_drive_id
        if st.session_state.get("review_drive_preview_id") == selected_drive_id:
            try:
                with st.spinner("Opening original document…"):
                    payload = drive_preview_bytes(
                        drive_identity, selected_drive_id,
                        getattr(drive_doc, "drive_version", "") or drive_doc.file_hash, store,
                    )
                show_document_preview(drive_doc.filename, payload)
            except Exception as error:
                from app.drive.originals import OriginalUnavailable
                st.warning(str(error) if isinstance(error, OriginalUnavailable) else "The Drive original could not be previewed. Please try again.")
        render_dashboard_delete_confirmation(
            store, selected_drive_id, drive_doc.filename, "review_drive_delete_id",
            f"review_drive_{drive_token}", drive_original=True,
        )
    queue=uploaded_documents if show_all else uploaded_documents[uploaded_documents.needs_review | ~uploaded_documents.available]
    show_table(queue[["company_name","filename","method","reason"]].rename(columns={"company_name":"Company Name","filename":"File","method":"Classification","reason":"Reason"}))
    if not queue.empty:
        lookup={int(r.id):f"{r.company_name or 'Unassigned'} | {r.filename}" for r in queue.itertuples(index=False)}
        requested = st.session_state.pop("review_document_request", None)
        if requested in lookup:
            st.session_state["review_selected_doc"] = requested
        if st.session_state.get("review_selected_doc") not in lookup:
            st.session_state.pop("review_selected_doc", None)
        selected_doc=st.selectbox("Select file to review",list(lookup),format_func=lookup.get, key="review_selected_doc")
        doc=queue[queue.id==selected_doc].iloc[0]
        with st.form(f"review_{selected_doc}"):
            company=st.text_input("Correct company name",value=doc.company_name)
            types=st.multiselect("Document type(s)",DOCUMENT_TYPES,default=[value for value in doc.types if value in DOCUMENT_TYPES])
            st.caption("Leave types empty to confirm a supporting/non-checklist document.")
            note=st.text_input("Note (optional)")
            submit=st.form_submit_button("Save correction",type="primary")
        if submit:
            try:
                store.correct_document(selected_doc,company,types,note)
                refresh("Correction saved. The company checklist is updated.")
            except Exception as error:
                st.error(str(error) if isinstance(error,ValueError) else "Correction could not be saved. Check storage access.")
        data=document_bytes(store, selected_doc, doc.file_hash)
        if getattr(doc, 'drive_url', ''):
            st.caption('Drive archive member: '+getattr(doc, 'drive_member', ''))
        with st.container(horizontal=True):
            if data is not None:
                st.download_button("Download original for review", data, doc.filename, key="review_download")
            if getattr(doc, "drive_url", ""):
                if data is None and st.button("Preview original", key=f"preview_review_document_{selected_doc}"):
                    st.session_state["review_preview_document_id"] = str(selected_doc)
                st.link_button("Open original in Drive", doc.drive_url, key=f"open_review_document_{selected_doc}")
                if data is None:
                    st.link_button("Download original", "/api/drive-index/original/" + str(selected_doc), key=f"download_review_document_{selected_doc}")
            if st.button("Delete from dashboard", key=f"delete_review_document_{selected_doc}"):
                st.session_state["review_delete_document_id"] = str(selected_doc)
        if data is not None:
            show_document_preview(doc.filename, data)
        elif getattr(doc, "drive_url", ""):
            if st.session_state.get("review_preview_document_id") == str(selected_doc):
                try:
                    with st.spinner("Opening original document…"):
                        preview = drive_preview_bytes(
                            drive_identity, str(selected_doc),
                            getattr(doc, "drive_version", "") or doc.file_hash, store,
                        )
                    show_document_preview(doc.filename, preview)
                except Exception as error:
                    from app.drive.originals import OriginalUnavailable
                    st.warning(str(error) if isinstance(error, OriginalUnavailable) else "The Drive original could not be previewed. Please try again.")
        elif not getattr(doc, "drive_url", ""):
            st.warning("Original file unavailable. You can still correct its company and document types.")
        render_dashboard_delete_confirmation(
            store, selected_doc, doc.filename, "review_delete_document_id",
            f"review_document_{selected_doc}", drive_original=bool(getattr(doc, "drive_url", "")),
        )
        st.caption("Source path: "+doc.original_path)
    else:
        if drive_only_issues.empty:
            st.success("No files are waiting for review.")
        else:
            st.caption("No uploaded files are waiting for review. Drive-only files are listed above.")

elif page == "Search all":
    vendor_ai_search_all_page(store)

elif page == "🤖 Vendor AI Assistant":
    vendor_ai_page()

else:
    st.subheader("Data storage & backups")
    if store.cloud:
        st.success("PostgreSQL storage is configured. Files, company records and reset backups are saved there.")
    else:
        st.info("Local data is saved in the vendor_data folder beside app.py. Closing the browser or resetting search does not delete it.")
        st.caption("For Streamlit Community Cloud, configure DATABASE_URL. Its local disk is not guaranteed to persist.")
    st.caption("Only the configured administrator can change shared data. Keep admin credentials private.")
    st.markdown("#### Download or restore a checkpoint")
    if st.button("Prepare full data backup",key="prepare_backup"):
        try:
            with st.spinner("Creating a backup..."):
                st.session_state["backup_download"] = store.backup_bytes()
        except Exception as error:
            st.error(str(error) if isinstance(error,ValueError) else "Backup failed. Saved data was not changed.")
    if data:=st.session_state.get("backup_download"):
        st.download_button("Download data backup",data,"Vendor_Dashboard_Backup.zip","application/zip")
    with st.expander("Restore Checkpoint"):
        restore_file=st.file_uploader("Backup ZIP made by this dashboard",type=["zip"],key="restore_upload")
        if st.button("Restore Checkpoint",disabled=restore_file is None):
            try:
                result=store.restore_backup(restore_file.getvalue())
                refresh(f"Restored {result['restored']} documents; {result['duplicates']} repeats skipped. Existing data was kept.")
            except Exception as error:
                st.error(str(error) if isinstance(error,ValueError) else "Restore could not finish. Existing data was kept; check the backup and retry.")
        backups=store.backup_list()
        if backups:
            ids=[b["id"] for b in backups]
            picked=st.selectbox("Saved checkpoints",ids)
            b1,b2=st.columns(2)
            if b1.button("Restore Checkpoint"):
                try:
                    result=store.restore_backup(store.read_backup(picked))
                    refresh(f"Restored {result['restored']} documents from the saved reset backup.")
                except Exception:
                    st.error("Restore failed. Existing data was kept; check storage and retry.")
            if b2.button("Download checkpoint"):
                st.session_state["backup_download"] = store.read_backup(picked)
                st.rerun()
    st.subheader("Delete ALL dashboard data")
    with st.container(border=True):
        st.error("**Permanent delete:** this removes every company, classified document/file, upload-history row, saved backup and every original ZIP in Uploaded ZIP Files. This action cannot be undone.")
        status_before = store.reset_status()
        active_items = sum(int(status_before.get(k, 0)) for k in ("companies", "documents", "history", "backups", "uploaded_zips", "legacy_documents", "legacy_vendors", "stored_document_files", "stored_zip_files", "other_runtime_files"))
        c1, c2, c3 = st.columns(3)
        c1.metric("Companies to delete", int(status_before.get("companies", 0)))
        c2.metric("Documents to delete", int(status_before.get("documents", 0)))
        c3.metric("Uploaded ZIPs to delete", int(status_before.get("uploaded_zips", 0)))
        if active_items == 0:
            st.success("Everything is already empty. You can still run the reset again to verify a clean state.")
        acknowledged = st.checkbox("I understand this permanently deletes ALL dashboard data and ALL uploaded ZIP files.", key="reset_acknowledged")
        confirmation = st.text_input("Type RESET HISTORY to confirm", key="reset_confirmation", placeholder="RESET HISTORY")
        reset_ready = bool(acknowledged and confirmation.strip() == "RESET HISTORY")
        st.button(
            "DELETE ALL DATA NOW",
            type="primary",
            disabled=not reset_ready,
            key="reset_history_button",
            width="stretch",
            on_click=execute_delete_all_data,
        )
        if not reset_ready:
            st.caption("The red delete button activates only after you tick the box and type RESET HISTORY exactly.")
        if reset_error := st.session_state.pop("reset_error", None):
            st.error(reset_error)
    with st.expander("Upload history"):
        rows=[]
        for event in store.history():
            details=event["details"]
            rows.append({"Time (UTC)":event["time"],"Action":event["action"],"Source":details.get("source",""),"Companies in upload":details.get("detected_companies",""),"New files":details.get("saved_files",""),"Repeats":details.get("duplicate_files","")})
        if rows:
            show_table(pd.DataFrame(rows))
        else:
            st.info("No completed uploads yet. Select Upload documents, choose a ZIP, and click Save documents. Large direct and Drive uploads appear here after the import finishes; the original ZIP may remain at its source.")
    with st.expander("Folder structure & matching"):
        st.code("Any handover vendor/\n  01 - Company A/\n    GST.pdf\n    PAN Card.pdf\n  02 - Company B/\n    ISO.pdf\n    Cancelled Cheque.jpg",language="text")
        st.write("Company folders decide ownership. Handover names and generic section folders are ignored, not appended to company names.")
        st.write("Identical bytes within the same company count once. Different document versions remain separate files. A company counts once even with several documents.")
        st.caption("Limits: 5 GB Drive download, 2 GB direct browser upload, 5,000 entries, 128 MB per document and 5 GB expanded data. Large originals remain at their source; skipped files appear in the upload summary.")
    st.subheader("Company name cleanup")
    st.caption("Merge existing records such as 'Alpha Ltd', 'alpha ltd.' and 'Alpha   Ltd' into one company. Documents are retained; identical files are combined.")
    if st.button("Merge duplicate company names", key="merge_duplicate_companies"):
        try:
            result = store.merge_duplicate_companies()
            refresh(f"Merged {result['merged_companies']} duplicate company record(s) and {result['merged_documents']} duplicate document record(s).")
        except Exception:
            logging.getLogger(__name__).exception("Duplicate company merge failed")
            st.error("Duplicate company merge failed. Existing data was not intentionally changed; check storage and retry.")
