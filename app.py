from __future__ import annotations

import hashlib
import hmac
import html
import importlib.util
import json
import logging
import mimetypes
import os
from io import BytesIO
from pathlib import Path
import time

import pandas as pd
import streamlit as st

from exports import csv_bytes, workbook_bytes
from import_service import import_documents
from drive_import import DiskUpload, download_drive_zip
from storage import Store
from vendor_core import (ALLOWED_EXTENSIONS, DOCUMENT_TYPES, build_checklist, dashboard_counts,
                         export_filename, filter_checklist, read_vendor_file, supporting_category)

APP_DIR = Path(__file__).resolve().parent
st.set_page_config(page_title="Vendor Document Dashboard", page_icon="\U0001f4c2", layout="wide")
st.markdown("""
<style>
.stApp{background:linear-gradient(145deg,#eef3f4 0%,#f8fafb 48%,#edf1f2 100%);color:#172b3a}
.block-container{max-width:1460px;padding-top:1.5rem;padding-bottom:3rem}
h1{font-size:2.1rem!important;font-weight:750!important;letter-spacing:-.025em;color:#173b43}
h2,h3{letter-spacing:-.015em;color:#173b43}
[data-testid="stSidebar"]{background:#fdfefe;border-right:1px solid #dce5e7}
[data-testid="stMetric"]{background:rgba(255,255,255,.92);border:1px solid #dce5e7;border-radius:10px;padding:.85rem 1rem;box-shadow:0 4px 14px rgba(25,55,64,.05)}
[data-testid="stMetricValue"]{font-size:1.85rem;color:#173b43;font-weight:700}
[data-testid="stMetricLabel"]{color:#58717a;font-size:.82rem;font-weight:650}
[data-testid="stFileUploader"]{border:1px dashed #8caeb3;border-radius:10px;background:rgba(255,255,255,.86)}
[data-testid="stExpander"]{background:rgba(255,255,255,.78);border:1px solid #dce5e7;border-radius:8px}
.brand{display:flex;gap:12px;align-items:center;margin:.25rem 0 1.2rem}
.brand-icon{background:#166d76;color:white;border-radius:8px;padding:9px 10px;font-weight:750;box-shadow:0 4px 10px rgba(22,109,118,.2)}
.brand-name{font-size:1.05rem;font-weight:700;color:#173b43}.brand-sub{font-size:.78rem;color:#6b8087}
.summary-label{margin:.9rem 0 .45rem;color:#58717a;font-size:.78rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em}
.type-label{margin:1.1rem 0 .45rem;color:#58717a;font-size:.78rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em}
.table-scroll{max-height:470px;overflow:auto;border:1px solid #dee6ed;border-radius:10px;background:#fff}
.desk-table{border-collapse:collapse;min-width:100%;font-size:.88rem}
.desk-table th{position:sticky;top:0;background:#203e50;color:#fff;text-align:left;padding:12px;white-space:nowrap}
.desk-table td{padding:10px 12px;border-bottom:1px solid #eef1f4;white-space:nowrap}
.desk-table tbody tr:nth-child(even){background:#fafcfd}
.yes{background:#e2f3eb;color:#17613e;border-radius:5px;padding:3px 9px;font-weight:600}
.no{background:#fff0ec;color:#a04b34;border-radius:5px;padding:3px 9px}
.small-note{font-size:.85rem;color:#647789}
@media(max-width:720px){.block-container{padding:1rem}h1{font-size:1.65rem!important}[data-testid="stMetricValue"]{font-size:1.55rem}}
</style>
""", unsafe_allow_html=True)


def setting(name: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(name, os.environ.get(name, default)))
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return os.environ.get(name, default)


def authenticated() -> bool:
    password = setting("APP_PASSWORD")
    if not password:
        if (APP_DIR / "CLOUD_DEPLOYMENT").exists():
            st.title("Vendor Document Dashboard")
            st.warning("First-time cloud setup: set APP_PASSWORD in Streamlit Settings > Secrets.")
            st.code('APP_PASSWORD = "your-own-long-password"\n# Durable online storage:\nDATABASE_URL = "postgresql://USER:PASSWORD@HOST:5432/DATABASE?sslmode=require"', language="toml")
            st.caption("Keep the app private. Never upload vendor documents or secrets to GitHub.")
            return False
        return True
    if st.session_state.get("authenticated"):
        return True
    st.title("Vendor Document Dashboard")
    with st.form("login"):
        typed = st.text_input("Team password", type="password")
        submit = st.form_submit_button("Open dashboard", type="primary")
    if submit:
        if time.time() < st.session_state.get("retry_after", 0):
            st.error("Please retry after the sign-in cooldown.")
        elif hmac.compare_digest(typed.encode(), password.encode()):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.session_state["retry_after"] = time.time() + 3
            st.error("Incorrect password.")
    return False


if not authenticated():
    st.stop()


@st.cache_resource
def open_store(data_dir: str, database_url: str) -> Store:
    return Store(Path(data_dir), database_url)


try:
    store = open_store(setting("VENDOR_DATA_DIR", str(APP_DIR / "vendor_data")), setting("DATABASE_URL"))
except Exception as error:
    logging.getLogger(__name__).error("Storage initialization failed: %s", type(error).__name__)
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
            st.info("This PDF could not be reduced to a first-page preview. Download the original file to view it.")
    elif mime.startswith("image/"):
        st.image(payload, caption=filename, width="stretch")
    elif mime.startswith("text/") or suffix in {".csv", ".txt", ".log"}:
        try:
            st.code(payload.decode("utf-8", errors="replace"), language="text")
        except Exception:
            st.info("This text file could not be decoded for inline preview.")
    else:
        st.info("Inline preview is not available for this format. Use Download file to open it.")


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
    st.session_state["view_cleared"] = False
    st.session_state["notice"] = message
    for key in list(st.session_state):
        if str(key).startswith(("export_", "zip_", "backup_download")):
            del st.session_state[key]
    st.rerun()


def execute_delete_all_data():
    """Run the destructive reset as a widget callback, before the next Streamlit rerun."""
    acknowledged = bool(st.session_state.get("reset_acknowledged"))
    confirmation = str(st.session_state.get("reset_confirmation", "")).strip()
    if not acknowledged or confirmation != "RESET HISTORY":
        st.session_state["reset_error"] = "Tick the confirmation box and type RESET HISTORY exactly."
        return
    try:
        store.reset_data("RESET HISTORY")
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


# A reset callback runs before the app reruns, so clear its widget state here before
# the reset widgets are instantiated again.
if st.session_state.pop("reset_completed", False):
    st.session_state.pop("reset_confirmation", None)
    st.session_state.pop("reset_acknowledged", None)


# Apply requested navigation before the sidebar radio is instantiated.
if pending_page := st.session_state.pop("pending_page", None):
    st.session_state["page"] = pending_page

vendors = store.vendors()
documents = store.documents()
upload_archives = store.upload_archives()
aliases = store.aliases()
review_queue = store.review_queue()
checklist = build_checklist(vendors, documents)
counts = dashboard_counts(vendors, documents)
view_cleared = bool(st.session_state.get("view_cleared", False))
display_counts = {k: 0 for k in counts} if view_cleared else counts

with st.sidebar:
    st.markdown('<div class="brand"><div class="brand-icon">VD</div><div><div class="brand-name">Vendor Workspace</div><div class="brand-sub">Documents, organised.</div></div></div>', unsafe_allow_html=True)
    page = st.radio("Navigate", ["Companies & documents", "Upload documents", "Uploaded ZIPs", "Review files", "Data & backups"], key="page", captions=["Yes / No checklist + company downloads", "Add a new vendor batch", "Original ZIP uploads saved here", "Only files that need correction", "Backup, restore or reset saved data"])
    st.divider()
    if store.cloud:
        st.success("Saved to cloud database")
        st.caption("Files and records are stored in PostgreSQL.")
    elif (APP_DIR / "CLOUD_DEPLOYMENT").exists():
        st.warning("Temporary cloud disk. Set DATABASE_URL for permanent uploads.")
    else:
        st.success("Saved on this computer")
        st.caption("Records stay in vendor_data after closing the browser. Keep this folder when updating.")
    st.caption("Vendor workspace · local records protected")
    if setting("APP_PASSWORD") and st.button("Sign out", width="stretch"):
        st.session_state.clear(); st.rerun()

header, upload_col, checklist_col, clear_col, reset_col = st.columns([4.0, 1.25, 1.4, 1.15, 1.35], vertical_alignment="center")
header.title("Vendor Document Dashboard")
upload_col.button("Upload documents", key="open_upload", on_click=open_upload, width="stretch", help="Upload a company-folders ZIP or document files.")
checklist_col.button("Yes / No checklist", key="open_checklist", on_click=open_checklist, width="stretch", help="Open the company-wise document checklist and Excel export.")
if view_cleared:
    clear_col.button("Show saved data", key="show_saved_data", on_click=show_saved_data, width="stretch", help="Show the saved companies and documents again.")
else:
    clear_col.button("Clear search", key="reset_filters", on_click=reset_filters, width="stretch", help="Clear the dashboard view and show 0 summary counters without deleting saved data.")
reset_col.button("Reset all history", key="open_reset", on_click=open_data_reset, width="stretch", help="Delete ALL dashboard data, history, backups and uploaded ZIP files. The dashboard returns to 0 / 0 / 0 / 0.")
if notice := st.session_state.pop("notice", None):
    st.success(notice)

st.markdown('<div class="summary-label">Workspace overview</div>', unsafe_allow_html=True)
m1, m2, m3, m4 = st.columns(4)
m1.metric("Vendor headcount", f"{display_counts['companies']:,}", help="Unique companies across saved vendor records and assigned documents. Multiple files do not increase headcount.")
m2.metric("Stored documents", f"{display_counts['stored_files']:,}", help="Available document records. Identical bytes uploaded again for the same company do not increase this count.")
m3.metric(f"All {len(DOCUMENT_TYPES)} types available", f"{display_counts['complete_companies']:,}", help="Every checklist category has a classified file; this is not a validity/compliance score.")
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
    st.info("Dashboard view is cleared, so the summary shows 0. Saved data is still stored. Click **Show saved data** to bring it back, or use **Reset all data** to actually delete the active records.")
else:
    st.caption("Headcount is unique companies. Document totals count files, and document-type cards count classified files.")

if page == "Upload documents":
    st.subheader("Upload vendor documents")
    st.write("Upload your company-folders ZIP here. The dashboard will detect companies, classify files, update the Yes / No checklist, and keep existing saved records. Exact repeats are skipped.")
    source_mode = st.radio("Import source", ["Google Drive ZIP (large files)", "Upload ZIP directly (large files)", "Local ZIP path (large files)", "Upload small files"], horizontal=True, key="import_source")
    drive_link = ""
    local_zip_path = ""
    uploads = []
    if source_mode == "Local ZIP path (large files)":
        local_zip_path = st.text_input("ZIP file path on this computer", placeholder=r"C:\Users\you\Downloads\vendors.zip")
        st.caption("For a large ZIP already on this computer, paste its full Windows path. The file is read directly from disk, so it does not use the browser upload limit.")
    elif source_mode == "Google Drive ZIP (large files)":
        drive_link = st.text_input("Google Drive ZIP link", placeholder="https://drive.google.com/file/d/.../view")
        st.caption("Downloads directly to the server in small chunks, then saves documents one at a time. The link must allow downloads without signing in. Keep the original ZIP in Drive; it will not be copied to Uploaded ZIPs. Transfer speed depends on Drive and your server. Keep this tab open during import.")
    else:
        if source_mode == "Upload ZIP directly (large files)":
            st.caption("Choose a ZIP directly from your computer. Browser uploads are supported up to 2 GB per file; for larger files, use Local ZIP path or Google Drive.")
        else:
            st.caption("For large ZIPs, use the direct upload, Local ZIP path or Google Drive options above.")
        uploads = st.file_uploader("Choose document ZIP or files", type=["zip"]+sorted(e.lstrip(".") for e in ALLOWED_EXTENSIONS), accept_multiple_files=True, key="document_uploads")
    with st.expander("Optional settings"):
        single_company = st.text_input("Company for loose files only", key="loose_company", help="Leave blank for company-folder ZIPs. This assigns every selected file to one company.")
        use_pdf = st.checkbox("Read PDF headings for unclear filenames", value=False, help="Optional local text extraction. No OCR or external service. Scans can still need review.")
        expected = st.number_input("Expected company count (0 = not specified)", min_value=0, value=0, step=1)
    if st.button("Save documents", key="save_documents", disabled=not (uploads or drive_link.strip() or local_zip_path.strip()), type="primary", width="stretch"):
        progress = st.empty()
        with st.spinner("Reading folders and saving documents..."):
            try:
                def show_progress(i, path):
                    if i == 1 or i % 10 == 0:
                        progress.caption(f"Saving file {i}: {path.rsplit('/', 1)[-1]}")
                if local_zip_path.strip():
                    local_path = Path(local_zip_path.strip().strip('"'))
                    if not local_path.is_file() or local_path.suffix.lower() != ".zip":
                        raise ValueError("Enter the full path to an existing .zip file on this computer.")
                    with local_path.open("rb") as local_file:
                        result = import_documents(store, [DiskUpload(local_file, local_path.name)], single_company, use_pdf, show_progress, retain_archive=False)
                elif drive_link.strip():
                    with download_drive_zip(drive_link, lambda n: progress.caption(f"Downloaded {n / 1024**2:,.0f} MB...")) as downloaded:
                        result = import_documents(store, [downloaded], single_company, use_pdf, show_progress, retain_archive=False)
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
            "Repeated files": last["duplicate_files"], "Skipped / failed": len(last["issues"])}])
        show_table(summary)
        st.caption("Source: " + last["source"] + ". Vendor head count is cumulative and unique: existing vendors are counted once, and new vendor names from later ZIPs are added automatically.")
        expected_count = st.session_state.get("expected_count", 0)
        if expected_count and expected_count != last["detected_companies"]:
            st.warning(f"You expected {expected_count} companies; this upload identified {last['detected_companies']}. Check the company list below. Counts are not padded.")
        with st.expander(f"Show {last['detected_companies']} company names from this upload"):
            show_table(pd.DataFrame({"No.": range(1,len(last["company_names"])+1), "Company Name": last["company_names"]}))
        if last["issues"]:
            st.warning("Some files were skipped or could not be saved. Review these before treating the batch as complete.")
            show_table(pd.DataFrame(last["issues"]))
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
        st.caption("Yes = a classified file is available for that company. No = no classified file was found in that category. Review files separately before treating a No as final.")
        checklist_dl1, checklist_dl2 = st.columns(2)
        visible_excel_name = export_filename(filtered.iloc[0]["Company Name"], "Checklist", "xlsx") if len(filtered) == 1 else "All_Vendors_Checklist.xlsx"
        visible_csv_name = export_filename(filtered.iloc[0]["Company Name"], "Checklist", "csv") if len(filtered) == 1 else "All_Vendors_Checklist.csv"
        checklist_dl1.download_button("Download visible checklist - Excel", workbook_bytes(filtered, scoped, True, aliases), visible_excel_name, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", width="stretch", key="visible_excel")
        checklist_dl2.download_button("Download visible checklist - CSV", csv_bytes(view), visible_csv_name, "text/csv", width="stretch", key="visible_csv")
        st.divider()
        st.markdown("### Company documents")
        options = filtered.company_key.tolist()
        mapping = dict(zip(filtered.company_key, filtered["Company Name"]))
        if st.session_state.get("company_selection") not in options:
            st.session_state["company_selection"] = options[0]
        selected = st.selectbox("Select company", options, format_func=mapping.get, key="company_selection")
        company = mapping[selected]
        company_docs = documents[documents.company_key == selected]
        company_checklist = checklist[checklist.company_key == selected]
        signature = hashlib.sha256((selected + repr(company_docs[["id","types_json","reviewed","available","file_hash"]].to_dict("records"))).encode()).hexdigest()
        with st.container(border=True):
            st.subheader(company)
            row = company_checklist.iloc[0]
            st.caption(f"{int(company_docs.available.sum())} documents saved | {row['Available']} of {len(DOCUMENT_TYPES)} document types available | {row['Needs review']} to review")
            x1,x2 = st.columns(2)
            x1.download_button("Download company Excel", workbook_bytes(company_checklist, company_docs, True, aliases),
                export_filename(company,"Checklist","xlsx"), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", width="stretch", key="company_excel")
            if x2.button("Prepare all company documents", disabled=company_docs.empty, width="stretch", key="prepare_company_zip"):
                with st.spinner("Preparing this company's files..."):
                    st.session_state["zip_company"] = (signature, store.company_zip(company, company_docs))
            value = st.session_state.get("zip_company")
            if value and value[0] == signature:
                st.download_button("Download " + company + " documents ZIP", value[1], export_filename(company,"Documents","zip"), "application/zip", type="primary", width="stretch", key="company_zip_download")
            st.caption("Excel and ZIP filenames include the selected company name. The ZIP includes its checklist and document folders.")
            for category in DOCUMENT_TYPES + ["Other documents"]:
                rows = company_docs[company_docs.types.map(lambda ts: category in ts if category in DOCUMENT_TYPES else not ts)]
                available = int(rows.available.sum())
                yes_no = "Yes" if available else "No"
                label = f"{category}  |  {yes_no}  |  {available} file(s)" if category in DOCUMENT_TYPES else f"Other documents  |  {available} file(s)"
                with st.expander(label):
                    if rows.empty:
                        st.caption("No file available in this category.")
                    for doc in rows.itertuples(index=False):
                        filename_col, preview_col = st.columns([8.5, 1.5], vertical_alignment="center")
                        filename_col.text(doc.filename)
                        detail = supporting_category(doc.filename) if not doc.types else doc.method
                        st.caption(f"{detail or 'Needs review'} | {doc.size_bytes / 1024:,.0f} KB")
                        payload = store.read_bytes(doc.id) if doc.available else None
                        if payload is not None:
                            preview_key = f"preview_{doc.id}"
                            if preview_col.button("👁", key=preview_key, help="Preview this document before downloading."):
                                st.session_state["preview_document_id"] = doc.id
                            if st.session_state.get("preview_document_id") == doc.id:
                                with st.container(border=True):
                                    preview_title, close_preview = st.columns([5, 1])
                                    preview_title.markdown(f"**Preview: {doc.filename}**")
                                    if close_preview.button("Close", key=f"close_{doc.id}"):
                                        st.session_state.pop("preview_document_id", None)
                                        st.rerun()
                                    show_document_preview(doc.filename, payload)
                            st.download_button("Download file", payload, doc.filename,
                                mimetypes.guess_type(doc.filename)[0] or "application/octet-stream", key=f"file_{category}_{doc.id}")
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

elif page == "Uploaded ZIPs":
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
    st.subheader("Check unclear documents")
    if not review_queue.empty:
        st.markdown("### Company match review queue")
        show_table(review_queue[["detected_name", "possible_company", "canonical_id", "confidence", "evidence", "source", "status"]].rename(columns={"detected_name":"Detected name", "possible_company":"Possible company", "canonical_id":"Company ID", "confidence":"Confidence", "evidence":"Evidence", "source":"Source", "status":"Status"}))
        st.caption("These records were not automatically merged because the company match was uncertain.")
    st.caption("Recognised supporting certificates stay under Other documents. Only unclear classifications and unavailable files need attention.")
    show_all=st.checkbox("Show all documents for correction")
    queue=documents if show_all else documents[documents.needs_review | ~documents.available]
    show_table(queue[["company_name","filename","method","reason"]].rename(columns={"company_name":"Company Name","filename":"File","method":"Classification","reason":"Reason"}))
    if not queue.empty:
        lookup={int(r.id):f"{r.company_name or 'Unassigned'} | {r.filename}" for r in queue.itertuples(index=False)}
        selected_doc=st.selectbox("Select file to review",list(lookup),format_func=lookup.get)
        doc=queue[queue.id==selected_doc].iloc[0]
        with st.form(f"review_{selected_doc}"):
            company=st.text_input("Correct company name",value=doc.company_name)
            types=st.multiselect("Document type(s)",DOCUMENT_TYPES,default=doc.types)
            st.caption("Leave types empty to confirm a supporting/non-checklist document.")
            note=st.text_input("Note (optional)")
            submit=st.form_submit_button("Save correction",type="primary")
        if submit:
            try:
                store.correct_document(selected_doc,company,types,note)
                refresh("Correction saved. The company checklist is updated.")
            except Exception as error:
                st.error(str(error) if isinstance(error,ValueError) else "Correction could not be saved. Check storage access.")
        data=store.read_bytes(selected_doc)
        if data is not None:
            st.download_button("Download original for review",data,doc.filename,key="review_download")
        st.caption("Source path: "+doc.original_path)
    else:
        st.success("No files are waiting for review.")

else:
    st.subheader("Data storage & backups")
    if store.cloud:
        st.success("PostgreSQL storage is configured. Files, company records and reset backups are saved there.")
    else:
        st.info("Local data is saved in the vendor_data folder beside app.py. Closing the browser or resetting search does not delete it.")
        st.caption("For Streamlit Community Cloud, configure DATABASE_URL. Its local disk is not guaranteed to persist.")
    st.caption("Password protection is a shared-team control, not individual roles. Restrict app access when using PAN, Aadhaar and bank documents.")
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
