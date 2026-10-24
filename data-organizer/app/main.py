"""FastAPI application: JSON API + the single-page dashboard.

    uvicorn app.main:app            (or)   python -m app serve
"""
from __future__ import annotations

import csv
import math
import os
import sys
import zipfile
from functools import lru_cache
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from a2wsgi import WSGIMiddleware
from hospkart_chat.app import app as chatbot_app
import pandas as pd
from pydantic import BaseModel, Field
import streamlit as streamlit

from .config import ConfigError
from .drive.google_drive import drive_status
from .logging_setup import get_logger
from .matching.review import ReviewError
from .pipeline import make_source
from .services import AppState, get_state
from .vendor_zip_upload import router as vendor_upload_router

STATIC = Path(__file__).parent / "dashboard" / "static"
VENDOR_SCRIPT = Path(__file__).resolve().parents[1] / "vendor_dashboard" / "app.py"
log = get_logger("application")

# Keep both workspaces in one ASGI process. Streamlit owns the Vendor UI at
# /vendor while FastAPI continues to serve the Organizer UI and its API.
vendor_dashboard = streamlit.App(str(VENDOR_SCRIPT))
app = FastAPI(
    title="Data Organizer",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
    lifespan=vendor_dashboard.lifespan(),
)
app.include_router(vendor_upload_router)


def _clean(obj: Any) -> Any:
    """JSON-safe: NaN/Inf -> None, datetimes -> str."""
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return obj


def st() -> AppState:
    return get_state()


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path in {"/healthz", "/vendor-upload"} or path == "/api/vendor-uploads" or path.startswith("/api/vendor-uploads/"):
        return await call_next(request)
    # The mounted Streamlit app owns its own sessions and storage. Do not open
    # the Organizer database or apply Organizer API authentication to it.
    if path == "/vendor" or path.startswith("/vendor/") or path == "/chatbot" or path.startswith("/chatbot/"):
        return await call_next(request)
    token = st().settings.dashboard_token
    if token and path.startswith("/api/") and request.headers.get("x-token") != token and request.query_params.get("token") != token:
        return JSONResponse({"detail": "Missing or wrong access token"}, status_code=401)
    try:
        response = await call_next(request)
    except ConfigError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except ReviewError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except KeyError as exc:
        return JSONResponse({"detail": f"Not found: {exc}"}, status_code=404)
    except FileNotFoundError as exc:
        return JSONResponse({"detail": f"Not found: {exc}"}, status_code=404)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if not path.startswith("/api/exports/download"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def J(data: Any) -> JSONResponse:
    return JSONResponse(_clean(data))


@app.get("/healthz", include_in_schema=False)
def healthz():
    """Public liveness probe; does not open storage or reveal application data."""
    return {"status": "ok"}


@app.get("/vendor-upload", include_in_schema=False)
def vendor_zip_upload_page():
    return FileResponse(STATIC / "vendor-upload.html", headers={
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
    })


@lru_cache(maxsize=1)
def catalogue_store():
    vendor_dashboard_dir = str(VENDOR_SCRIPT.parent)
    if vendor_dashboard_dir not in sys.path:
        sys.path.insert(0, vendor_dashboard_dir)
    from vendor_dashboard.storage import Store

    try:
        database_url = str(streamlit.secrets.get("DATABASE_URL", os.environ.get("DATABASE_URL", "")))
        data_dir = str(streamlit.secrets.get("VENDOR_DATA_DIR", os.environ.get(
            "VENDOR_DATA_DIR", str(VENDOR_SCRIPT.parent / "vendor_data")
        )))
    except (FileNotFoundError, streamlit.errors.StreamlitSecretNotFoundError):
        database_url = os.environ.get("DATABASE_URL", "")
        data_dir = os.environ.get("VENDOR_DATA_DIR", str(VENDOR_SCRIPT.parent / "vendor_data"))
    return Store(Path(data_dir), database_url)


# ----------------------------------------------------------------------------- status / config
@app.get("/api/status")
def status():
    return J(st().status())


@app.get("/api/progress")
def progress():
    return J(st().progress.snapshot())


@app.get("/api/config")
def get_config():
    return J(st().cfg.to_dict())


@app.put("/api/config")
def put_config(body: dict[str, Any]):
    return J(st().update_config(body).to_dict())


@app.get("/api/schema")
def schema():
    return J(st().schema_info())


@app.get("/api/drive/status")
def drive():
    s = st()
    return J(drive_status(s.settings, s.cfg))


@app.get("/api/drive/files")
def drive_files():
    from .config import parse_drive_folder_id
    from .drive.public_drive import list_public_files
    s = st()
    folder = parse_drive_folder_id(s.cfg.drive_folder or s.settings.drive_folder_id or s.settings.drive_folder_url)
    return J({"files": list_public_files(folder)})


@app.post("/api/drive/import-zip")
def drive_import_zip(body: dict[str, str]):
    from .config import parse_drive_folder_id
    from .drive.public_drive import list_public_files
    from vendor_dashboard.drive_import import download_drive_zip
    s = st()
    if s.progress.running:
        raise ConfigError("Wait for file reading to finish before importing a Drive ZIP.")
    folder = parse_drive_folder_id(s.cfg.drive_folder or s.settings.drive_folder_id or s.settings.drive_folder_url)
    match = next((item for item in list_public_files(folder)
                  if item['id'] == body.get('file_id') and item['name'].lower().endswith('.zip')), None)
    if match is None:
        raise ConfigError("Choose a ZIP from the connected Drive folder.")
    with download_drive_zip(match['url'], max_bytes=2 * 1024**3) as downloaded:
        # Validate the entire inventory before saving/extracting anything.
        with zipfile.ZipFile(downloaded.file) as archive:
            entries = archive.infolist()
            if len(entries) > 5000 or sum(item.file_size for item in entries) > 5 * 1024**3:
                raise ConfigError("Choose a smaller ZIP: maximum 5,000 entries and 5 GiB expanded.")
            if any(item.file_size > 128 * 1024**2 for item in entries):
                raise ConfigError("A document in this ZIP exceeds the 128 MB per-document limit.")
        downloaded.file.seek(0)
        filename = s.save_upload_stream('drive-' + match['id'] + '.zip', downloaded.file, 2 * 1024**3)
    report = s.extract_upload_archive(filename, max_uncompressed_bytes=5 * 1024**3)
    source = s.settings.uploads_dir / '_extracted'
    s.update_config({'source_kind': 'local', 'local_input_dir': str(source)})
    return J({'saved': match['name'], 'skipped_entries': report['skipped']})


# ----------------------------------------------------------------------------- setup helpers
@app.post("/api/demo/prepare")
def demo_prepare():
    return J(st().prepare_demo())


@app.post("/api/upload", openapi_extra={
    "requestBody": {"required": True, "content": {"multipart/form-data": {
        "schema": {"type": "object", "required": ["files"], "properties": {
            "files": {"type": "array", "items": {"type": "string", "format": "binary"}}
        }}
    }}}
})
async def upload(request: Request):
    s = st()
    saved = []
    has_archive = False
    skipped_entries = 0
    # Starlette otherwise rejects multipart requests after 1,000 files.
    async with request.form(max_files=float("inf")) as form:
        files = form.getlist("files")
        if not files or any(not hasattr(file, "file") for file in files):
            raise HTTPException(422, "Choose at least one file")
        for f in files:
            try:
                filename = f.filename or "upload"
                saved.append(await run_in_threadpool(s.save_upload_stream, filename, f.file))
                if filename.lower().endswith(".zip"):
                    report = await run_in_threadpool(s.extract_upload_archive, filename)
                    skipped_entries += report["skipped"]
                    has_archive = True
            except ConfigError as exc:
                raise HTTPException(400, str(exc)) from exc
    source = s.settings.uploads_dir / "_extracted" if has_archive else s.settings.uploads_dir
    s.update_config({"source_kind": "local", "local_input_dir": str(source)})
    return J({"saved": saved, "folder": str(source), "skipped_entries": skipped_entries})


@app.get("/api/catalogue/vendors")
def catalogue_vendors():
    vendors = catalogue_store().vendors()
    return J([{"vendor_id": row.canonical_id, "company_name": row.company_name}
              for row in vendors.itertuples(index=False)])


@app.post("/api/catalogue/imports")
async def create_catalogue_import(request: Request):
    store = catalogue_store()
    from catalogue_extraction import extract_records, extract_whatsapp_records
    from vendor_core import Decision

    max_bytes = 100 * 1024 * 1024
    async with request.form() as form:
        upload_file = form.get("file")
        vendor_id = str(form.get("vendor_id") or "").strip()
        if not upload_file or not hasattr(upload_file, "read"):
            raise HTTPException(422, "Choose a catalogue file.")
        filename = str(upload_file.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
        suffix = Path(filename).suffix.casefold()
        if suffix not in {".xlsx", ".xls", ".csv", ".txt", ".zip"}:
            raise HTTPException(415, "Catalogue imports support Excel, CSV, WhatsApp TXT, and ZIP files.")
        content = await upload_file.read(max_bytes + 1)
        if not content:
            raise HTTPException(422, "The selected file is empty.")
        if len(content) > max_bytes:
            raise HTTPException(413, "Catalogue files must be 100 MB or smaller.")

    vendors = await run_in_threadpool(store.vendors)
    matches = vendors[vendors.canonical_id.astype(str) == vendor_id]
    if matches.empty:
        raise HTTPException(422, "Select a vendor that already exists in the verified vendor list.")
    vendor_name = str(matches.iloc[0].company_name)
    skipped_messages = 0
    records = []
    if suffix == ".zip":
        try:
            with zipfile.ZipFile(BytesIO(content)) as archive:
                text_files = [item for item in archive.infolist() if not item.is_dir()
                              and Path(item.filename).suffix.casefold() == ".txt"]
                if len(text_files) > 200 or sum(item.file_size for item in text_files) > max_bytes:
                    raise HTTPException(413, "ZIP imports support up to 200 text files and 100 MB extracted text.")
                for item in sorted(text_files, key=lambda entry: entry.filename.casefold()):
                    chat_rows, skipped = await run_in_threadpool(
                        extract_whatsapp_records, item.filename, archive.read(item)
                    )
                    for row in chat_rows:
                        row["source_sheet"] = item.filename[:200]
                    records.extend(chat_rows)
                    skipped_messages += skipped
        except zipfile.BadZipFile as exc:
            raise HTTPException(422, "The selected ZIP file is invalid or damaged.") from exc
        kind = "price"
    elif suffix == ".txt":
        records, skipped_messages = await run_in_threadpool(extract_whatsapp_records, filename, content)
        kind = "price"
        if not records:
            kind, records = await run_in_threadpool(extract_records, filename, content)
    else:
        kind, records = await run_in_threadpool(extract_records, filename, content)
    if not records:
        raise HTTPException(
            422,
            "No reliable product-price rows were found. WhatsApp imports only pair a product and price "
            "from the same message; add an Excel/CSV catalogue or correct the chat formatting.",
        )

    decision = Decision(
        company_name=vendor_name,
        document_types=("Price" if kind == "price" else "Catalogue",),
        method="Organizer catalogue import",
        reason="Extracted catalogue rows require review before publication.",
    )
    await run_in_threadpool(store.save_document, filename, content, decision, "Organizer catalogue approval")
    document_id = await run_in_threadpool(store.document_id_for, filename, content, vendor_name)
    if document_id is None:
        raise RuntimeError("The original catalogue file was not retained; no rows were staged.")
    staged = await run_in_threadpool(store.stage_product_records, document_id, records)
    return J({
        **staged,
        "filename": filename,
        "vendor_id": vendor_id,
        "vendor_name": vendor_name,
        "records_extracted": len(records),
        "messages_needing_review": skipped_messages,
        "duplicate_upload": staged["staged"] == 0,
        "message": "Rows are held for review and are not visible in product search until approved. "
                   "WhatsApp rows require confirmation; price/product associations are not inferred across messages.",
    })


@app.get("/api/catalogue/imports")
def catalogue_imports():
    return J({"items": catalogue_store().product_imports()})


@app.get("/api/catalogue/imports/{import_id}/rows")
def catalogue_import_rows(import_id: int, status: str = "pending", limit: int = 200, offset: int = 0):
    if status not in {"pending", "approved", "duplicate", "rejected", "all"}:
        raise HTTPException(422, "Invalid catalogue review status.")
    rows = catalogue_store().product_import_rows(import_id, "" if status == "all" else status, limit, offset)
    return J({"items": rows, "limit": limit, "offset": offset})


class CatalogueRowEdit(BaseModel):
    changes: dict[str, Any] = Field(min_length=1)


@app.patch("/api/catalogue/import-rows/{row_id}")
def edit_catalogue_row(row_id: int, body: CatalogueRowEdit):
    return J(catalogue_store().edit_product_import_row(row_id, body.changes))


class CatalogueRowDecision(BaseModel):
    action: Literal["approve", "reject"]


@app.post("/api/catalogue/import-rows/{row_id}/decide")
def decide_catalogue_row(row_id: int, body: CatalogueRowDecision):
    return J(catalogue_store().decide_product_import_row(row_id, body.action))


@app.get("/api/catalogue/metrics")
def catalogue_metrics():
    return J(catalogue_store().product_import_metrics())


@app.get("/api/catalogue/products")
def catalogue_products(query: str = "", vendor_id: str = "", limit: int = 100, offset: int = 0):
    rows, total = catalogue_store().published_catalogue_rows(query, vendor_id, limit, offset)
    return J({"items": rows, "total": total, "limit": limit, "offset": offset})


@app.get("/api/catalogue/products/export.xlsx")
def export_catalogue_products(query: str = "", vendor_id: str = ""):
    store = catalogue_store()
    _, total = store.published_catalogue_rows(query, vendor_id, 1, 0)
    if total > 100_000:
        raise HTTPException(413, "Excel export is limited to 100,000 catalogue records. Filter by vendor or search term.")
    products = []
    offset = 0
    while offset < total:
        rows, _ = store.published_catalogue_rows(query, vendor_id, 500, offset)
        if not rows:
            break
        products.extend(rows)
        offset += len(rows)
    columns = ["company_name", "product_name", "sku", "product_category", "brand", "price", "mrp",
               "currency", "unit", "pack_size", "availability", "source_file", "source_sheet",
               "source_page", "source_row", "extracted_at"]
    workbook = BytesIO()
    with pd.ExcelWriter(
        workbook,
        engine="xlsxwriter",
        engine_kwargs={"options": {"strings_to_formulas": False, "strings_to_urls": False}},
    ) as writer:
        pd.DataFrame(products, columns=columns).to_excel(writer, sheet_name="Approved catalogue", index=False)
    return StreamingResponse(
        BytesIO(workbook.getvalue()),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="approved-catalogue.xlsx"'},
    )


@app.get("/api/catalogue/price-history")
def catalogue_price_history(limit: int = 100, offset: int = 0):
    return J({"items": catalogue_store().product_price_history(limit=limit, offset=offset)})


@app.get("/api/catalogue/imports/{import_id}/export.csv")
def export_catalogue_import(import_id: int, status: str = "all"):
    if status not in {"pending", "approved", "duplicate", "rejected", "all"}:
        raise HTTPException(422, "Invalid catalogue export status.")
    store = catalogue_store()
    columns = ["status", "company_name", "product_name", "sku", "price", "currency", "unit", "pack_size",
               "product_category", "brand", "source_file", "source_sheet", "source_page", "source_row", "decision"]

    def safe_cell(value):
        if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
            return "'" + value
        return value

    def content():
        first = StringIO()
        writer = csv.writer(first)
        writer.writerow(columns)
        yield first.getvalue()
        offset = 0
        while True:
            rows = store.product_import_rows(import_id, "" if status == "all" else status, 500, offset)
            if not rows:
                break
            for item in rows:
                record = item["record"]
                line = StringIO()
                csv.writer(line).writerow([safe_cell(value) for value in [
                    item["status"], record.get("company_name", ""), record.get("product_name", ""),
                    record.get("sku", ""), record.get("price"), record.get("currency", ""),
                    record.get("unit", ""), record.get("pack_size", ""), record.get("product_category", ""),
                    record.get("brand", ""), record.get("source_file", ""), item["source_sheet"],
                    item["source_page"], item["source_row"], item["decision"],
                ]])
                yield line.getvalue()
            offset += len(rows)

    list(store.product_import_rows(import_id, status if status != "all" else "", 1))
    return StreamingResponse(content(), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="catalogue-import-{import_id}.csv"'})


class ResetBody(BaseModel):
    scope: Literal["derived", "all"] = "derived"


@app.post("/api/reset")
def reset(body: ResetBody):
    st().reset(body.scope)
    return J({"ok": True})


# ----------------------------------------------------------------------------- files / inspection / dry run
@app.post("/api/source/uploads")
def select_uploaded_source():
    return J(st().select_uploaded_source())


@app.post("/api/scan")
def scan():
    s = st()
    pipe = s.pipeline()
    files = pipe.discover()
    return J({"count": len(files), "files": pipe.inventory()})


@app.post("/api/organize-files")
def organize_files():
    state = st()
    result = state.organize_source_files()
    result["vendor_sync"] = state.start_vendor_sync(result["folder"])
    return J(result)


@app.get("/api/vendor/sync/status")
def vendor_sync_status():
    return J(st().vendor_sync_status())


@app.get("/api/inventory")
def inventory():
    state = st()
    # A cleared workspace must not display records from a previous source.
    if state.cfg.source_kind == "local" and not state.cfg.local_input_dir:
        return J({"files": []})
    try:
        files = state.pipeline().inventory()
    except ConfigError:
        files = []
    return J({"files": files})


class InspectBody(BaseModel):
    max_files: int = Field(default=40, ge=1, le=500)


@app.post("/api/inspect")
def inspect(body: InspectBody):
    s = st()
    pipe = s.pipeline()
    return J({"files": pipe.inspect(max_files=body.max_files)})


@app.get("/api/files/{file_key}")
def file_detail(file_key: str):
    row = st().file_inspection(file_key)
    if row is None:
        raise KeyError(file_key)
    return J(row)


@app.delete("/api/files/{file_key}")
def remove_unsupported_file(file_key: str):
    return J(st().remove_unsupported_file(file_key))


class DryRunBody(BaseModel):
    sample: int | None = Field(default=None, ge=10, le=5000)


@app.post("/api/dry-run")
def dry(body: DryRunBody):
    df = st().dry_run(body.sample)
    return J({"rows": df.to_dict(orient="records"), "count": len(df)})


# ----------------------------------------------------------------------------- run control
class RunBody(BaseModel):
    limit_per_file: int | None = Field(default=None, ge=1)


@app.post("/api/run")
def run(body: RunBody):
    st().start_run(body.limit_per_file)
    return J({"started": True})


@app.post("/api/stop")
def stop():
    st().stop_run()
    return J({"stopping": True})


# ----------------------------------------------------------------------------- results
@app.get("/api/summary")
def summary():
    return J(st().summary())


@app.get("/api/entities")
def entities(search: str = "", state: str = "", city: str = "", status: str = "", sort: str = "standard_name",
             direction: str = "asc", limit: int = 50, offset: int = 0):
    return J(st().entities(search, state, city, status, sort, direction, limit, offset))


@app.get("/api/entities/{eid}")
def entity(eid: str):
    return J(st().entity(eid))


class RenameBody(BaseModel):
    standard_name: str = Field(min_length=1, max_length=300)


@app.post("/api/entities/{eid}/rename")
def rename(eid: str, body: RenameBody):
    return J(st().reviews().rename_entity(eid, body.standard_name))


class MergeBody(BaseModel):
    source_id: str
    target_id: str


@app.post("/api/entities/merge")
def merge(body: MergeBody):
    return J(st().reviews().merge_entities(body.source_id, body.target_id))


@app.get("/api/records")
def records(search: str = "", status: str = "", file: str = "", limit: int = 50, offset: int = 0):
    return J(st().records(search, status, file, limit, offset))


@app.get("/api/duplicates")
def duplicates(limit: int = 50, offset: int = 0):
    return J(st().duplicates(limit, offset))


@app.get("/api/quality")
def quality(severity: str = "", issue: str = "", limit: int = 100, offset: int = 0):
    return J(st().quality(severity, issue, limit, offset))


@app.get("/api/errors")
def errors(limit: int = 200, offset: int = 0):
    return J(st().errors(limit, offset))


@app.get("/api/logs/{name}")
def logs(name: str, lines: int = 200):
    return J({"name": name, "lines": st().tail_log(name, lines)})


# ----------------------------------------------------------------------------- manual review
@app.get("/api/reviews")
def reviews(status: str = "pending", limit: int = 30, offset: int = 0, search: str = "", min_score: float | None = None,
            max_score: float | None = None):
    return J(st().reviews().list_items(status, limit, offset, search, min_score, max_score))


@app.get("/api/reviews/{rid}")
def review_detail(rid: int):
    return J(st().reviews().get(rid))


class DecideBody(BaseModel):
    action: Literal["accept", "reject", "new_entity"]
    target_id: str | None = None
    standard_name: str | None = Field(default=None, max_length=300)
    scope: Literal["city", "alias"] = "city"


@app.post("/api/reviews/{rid}/decide")
def decide(rid: int, body: DecideBody):
    return J(st().reviews().decide(rid, body.action, body.target_id, body.standard_name, body.scope))


class BulkBody(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    action: Literal["accept", "reject", "new_entity"]


@app.post("/api/reviews/bulk")
def bulk(body: BulkBody):
    svc = st().reviews()
    done, failed = [], []
    for rid in body.ids:
        try:
            done += svc.decide(rid, body.action)["decided"]
        except ReviewError as exc:
            failed.append({"review_id": rid, "error": str(exc)})
    return J({"decided": done, "failed": failed})


# ----------------------------------------------------------------------------- export
class ProductExportBody(BaseModel):
    reference_files: list[str] = Field(default_factory=list)
    product_sheet_title: str = Field(default="Products & Prices", min_length=1, max_length=25)


@app.post("/api/export/products")
def export_products(body: ProductExportBody | None = None):
    return J(st().start_product_export(body.reference_files if body else (), body.product_sheet_title if body else "Products & Prices"))


@app.get("/api/export/products/status")
def product_export_status():
    return J(st().product_export_status())


@app.post("/api/export")
def export():
    s = st()
    if s.progress.running:
        raise ConfigError("Wait for the current run to finish before exporting")
    return J({"files": s.exporter().export_all()})


@app.post("/api/export/structured-zip")
def structured_zip():
    s = st()
    if s.progress.running:
        raise ConfigError("Wait for the current run to finish before creating the ZIP")
    result = s.create_structured_zip()
    result["vendor_sync"] = s.start_vendor_sync(
        Path(s.settings.data_dir) / "organized_by_company"
    )
    return J(result)


@app.get("/api/exports")
def exports():
    return J({"files": st().list_outputs()})


@app.get("/api/exports/download")
def download(file: str = Query(...)):
    p = st().resolve_output(file)
    return FileResponse(p, filename=p.name)


# ----------------------------------------------------------------------------- dashboard
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount("/vendor", vendor_dashboard, name="vendor-dashboard")

app.mount("/chatbot", WSGIMiddleware(chatbot_app), name="hospkart-chatbot")
