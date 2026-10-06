"""FastAPI application: JSON API + the single-page dashboard.

    uvicorn app.main:app            (or)   python -m app serve
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from a2wsgi import WSGIMiddleware
from hospkart_chat.app import app as chatbot_app
from pydantic import BaseModel, Field
import streamlit as streamlit

from .config import ConfigError
from .drive.google_drive import drive_status
from .logging_setup import get_logger
from .matching.review import ReviewError
from .pipeline import make_source
from .services import AppState, get_state

STATIC = Path(__file__).parent / "dashboard" / "static"
VENDOR_SCRIPT = Path(__file__).resolve().parents[1] / "vendor_dashboard" / "app.py"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024 * 1024
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


# ----------------------------------------------------------------------------- setup helpers
@app.post("/api/demo/prepare")
def demo_prepare():
    return J(st().prepare_demo())


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    s = st()
    saved = []
    has_archive = False
    skipped_entries = 0
    for f in files:
        try:
            filename = f.filename or "upload"
            saved.append(s.save_upload_stream(filename, f.file, MAX_UPLOAD_BYTES))
            if filename.lower().endswith(".zip"):
                report = s.extract_upload_archive(filename, MAX_UPLOAD_BYTES)
                skipped_entries += report["skipped"]
                has_archive = True
        except ConfigError as exc:
            raise HTTPException(413, str(exc)) from exc
    source = s.settings.uploads_dir / "_extracted" if has_archive else s.settings.uploads_dir
    s.update_config({"source_kind": "local", "local_input_dir": str(source)})
    return J({"saved": saved, "folder": str(source), "skipped_entries": skipped_entries})


class ResetBody(BaseModel):
    scope: Literal["derived", "all"] = "derived"


@app.post("/api/reset")
def reset(body: ResetBody):
    st().reset(body.scope)
    return J({"ok": True})


# ----------------------------------------------------------------------------- files / inspection / dry run
@app.post("/api/scan")
def scan():
    s = st()
    pipe = s.pipeline()
    files = pipe.discover()
    return J({"count": len(files), "files": pipe.inventory()})


@app.post("/api/organize-files")
def organize_files():
    return J(st().organize_source_files())


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
    return J(s.create_structured_zip())


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
