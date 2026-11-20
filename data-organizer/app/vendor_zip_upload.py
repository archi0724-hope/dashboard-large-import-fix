"""Authenticated, disk-backed Vendor ZIP upload and import progress."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import shutil
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .logging_setup import get_logger

router = APIRouter(prefix="/api/vendor-uploads")
DISK_RESERVE = 64 * 1024**2
CHUNK_BYTES = 8 * 1024**2
TOKEN_LIFETIME = 3 * 3600
_lock = threading.RLock()
_jobs: dict[str, dict] = {}
_active: str | None = None


def issue_upload_token(password: str, nonce: str | None = None) -> str:
    payload = f"{int(time.time()) + TOKEN_LIFETIME}:{nonce or secrets.token_hex(16)}"
    signature = hmac.new(
        password.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return payload + ":" + signature


def _token_nonce(token: str, password: str, grace: int = 0) -> str | None:
    try:
        expiry, nonce, signature = token.split(":")
        payload = expiry + ":" + nonce
        expected = hmac.new(
            password.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        valid = (
            password
            and re.fullmatch(r"[a-f0-9]{32}", nonce)
            and int(time.time()) - grace <= int(expiry) <= int(time.time()) + TOKEN_LIFETIME
        )
        valid = valid and hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        valid = False
    return nonce if valid else None


def require_upload_token(request: Request) -> str:
    from hospkart_chat.integration import vendor_setting

    nonce = _token_nonce(request.headers.get("x-upload-token", ""), vendor_setting("APP_PASSWORD"))
    if not nonce:
        raise HTTPException(
            401,
            "Your upload session expired. Return to the dashboard and choose Upload ZIP again.",
        )
    # Renewing a signed session must not change ownership of an existing job.
    return hashlib.sha256(nonce.encode()).hexdigest()


def _run_import(job_id: str, path: Path, filename: str, company: str, read_pdf: bool):
    global _active
    try:
        from .main import catalogue_store

        store = catalogue_store()
        from vendor_dashboard.drive_import import DiskUpload
        from vendor_dashboard.import_service import import_documents

        def progress(count, source):
            with _lock:
                _jobs[job_id].update(
                    processed=count, current_file=str(source).rsplit("/", 1)[-1]
                )

        with path.open("rb") as source:
            result = import_documents(
                store,
                [DiskUpload(source, filename)],
                company,
                read_pdf,
                progress,
                retain_archive=False,
            )
        with _lock:
            _jobs[job_id].update(
                status="done",
                result={
                    "saved": result.saved_files,
                    "duplicates": result.duplicate_files,
                    "processed": result.processed_files,
                    "issues": len(result.issues),
                    "companies": result.total_companies,
                    "detected_companies": result.detected_companies,
                },
            )
    except Exception as error:  # noqa: BLE001 - Report worker failure without exposing credentials or document bytes.
        get_logger("application").error(
            "Vendor ZIP import failed: %s", type(error).__name__
        )
        with _lock:
            _jobs[job_id].update(
                status="failed",
                error=(
                    str(error)
                    if isinstance(error, ValueError)
                    else "Import interrupted. Previously saved documents remain available. Retry the ZIP to skip saved duplicates."
                ),
            )
    finally:
        path.unlink(missing_ok=True)
        with _lock:
            if _active == job_id:
                _active = None


@router.post("")
async def upload_zip(
    request: Request,
    filename: str,
    company: str = "",
    read_pdf: bool = False,
    upload_id: str = '',
    owner: str = Depends(require_upload_token),
):
    global _active
    if upload_id and not re.fullmatch(r'[a-f0-9]{32}',upload_id):
        raise HTTPException(422,'Invalid upload session ID.')
    if (
        not filename
        or "/" in filename
        or "\\" in filename
        or Path(filename).suffix.lower() != ".zip"
    ):
        raise HTTPException(
            422,
            "Choose a ZIP using the file picker, rather than pasting a computer path.",
        )
    try:
        length = int(request.headers.get("content-length", "0"))
    except ValueError:
        raise HTTPException(400, "Invalid upload length.") from None
    if length < 0:
        raise HTTPException(400, "Invalid upload length.")
    folder = Path(tempfile.gettempdir())
    if length + DISK_RESERVE > shutil.disk_usage(folder).free:
        raise HTTPException(
            507, "This ZIP exceeds available server space. Keep the original in Google Drive and paste its link in the Drive library."
        )
    with _lock:
        if upload_id in _jobs:
            raise HTTPException(409,'This upload already exists. Resume its progress rather than sending it again.')
        if _active:
            raise HTTPException(
                409,
                "Another ZIP is being uploaded or imported. Wait until it finishes, then retry.",
            )
        # Keep a small bounded history. Completed jobs never retain document bytes.
        for key in list(_jobs):
            if len(_jobs) < 20:
                break
            if _jobs[key]["status"] in {"done", "failed"}:
                del _jobs[key]
        job_id = upload_id or secrets.token_hex(16)
        _active = job_id
        _jobs[job_id] = {
            "owner": owner,
            "status": "uploading",
            "processed": 0,
            "received": 0,
            "filename": filename,
            "created": time.time(),
        }
    path = None
    started = False
    try:
        with tempfile.NamedTemporaryFile(
            prefix="vendor-zip-", suffix=".zip", delete=False
        ) as destination:
            path = Path(destination.name)
            received = 0
            disk_check = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received >= disk_check:
                    if len(chunk) + DISK_RESERVE > shutil.disk_usage(folder).free:
                        raise HTTPException(
                            507, "Available server space is exhausted. Use a Google Drive link for this collection. Saved documents are kept."
                        )
                    disk_check = received + 8 * 1024**2
                await run_in_threadpool(destination.write, chunk)
                with _lock:
                    _jobs[job_id]["received"] = received
        if not received or (length and received != length):
            raise HTTPException(
                400, "The upload was incomplete. Choose the ZIP and try again."
            )
        if not await run_in_threadpool(zipfile.is_zipfile, path):
            raise HTTPException(422, "The selected file is not a readable ZIP.")
        with _lock:
            _jobs[job_id]["status"] = "importing"
        worker = threading.Thread(
            target=_run_import,
            args=(job_id, path, filename, company, read_pdf),
            name="vendor-zip-import",
            daemon=True,
        )
        worker.start()
        started = True
        return {"job_id": job_id}
    finally:
        if not started:
            if path:
                path.unlink(missing_ok=True)
            with _lock:
                _jobs[job_id].update(
                    status="failed",
                    error="Upload interrupted. Choose the ZIP and try again.",
                )
                if _active == job_id:
                    _active = None


@router.get('/capacity')
def upload_capacity(owner: str = Depends(require_upload_token)):
    available = max(0,shutil.disk_usage(tempfile.gettempdir()).free-DISK_RESERVE)
    return {'fixed_upload_limit_bytes':None,'available_temporary_bytes':available,
        'large_collection_mode':'drive_index'}


@router.post('/session')
def upload_session(request: Request):
    from hospkart_chat.integration import vendor_setting

    from .config import get_settings

    password = vendor_setting('APP_PASSWORD')
    # A recently expired, correctly signed proof may renew its own scope only.
    nonce = _token_nonce(request.headers.get('x-upload-token',''), password, grace=24 * 3600)
    if nonce:
        return {'token':issue_upload_token(password, nonce)}
    expected = get_settings().dashboard_token
    supplied = request.headers.get('x-token','')
    if expected and supplied and hmac.compare_digest(expected,supplied):
        if not password:
            raise HTTPException(503,'The shared upload password must be configured on the server.')
        return {'token':issue_upload_token(password)}
    require_upload_token(request)
    return {'token':request.headers['x-upload-token']}


@router.get("/{job_id}")
def upload_status(job_id: str, owner: str = Depends(require_upload_token)):
    with _lock:
        job = _jobs.get(job_id)
        if not job or job["owner"] != owner:
            raise HTTPException(
                404,
                "Upload not found. If the server restarted, retry the ZIP; saved documents are kept.",
            )
        return {key: value for key, value in job.items() if key not in {"owner", "path", "writing"}}


@router.post("/begin")
def begin_upload(
    filename: str,
    size: int,
    upload_id: str,
    company: str = "",
    read_pdf: bool = False,
    owner: str = Depends(require_upload_token),
):
    """Register a small request before transferring any archive bytes."""
    global _active
    if not re.fullmatch(r"[a-f0-9]{32}", upload_id):
        raise HTTPException(422, "Invalid upload session ID.")
    if not filename or "/" in filename or "\\" in filename or Path(filename).suffix.lower() != ".zip" or size <= 0:
        raise HTTPException(422, "Choose a non-empty ZIP using the file picker.")
    with _lock:
        existing = _jobs.get(upload_id)
        if existing:
            if existing["owner"] != owner:
                raise HTTPException(404, "Upload not found.")
            if existing.get("filename") != filename or existing.get("size") != size:
                raise HTTPException(409, "This upload belongs to a different file.")
            return {"job_id": upload_id, "chunk_bytes": CHUNK_BYTES}
        if _active:
            raise HTTPException(409, "Another ZIP is being uploaded or imported. Wait until it finishes, or cancel its transfer.")
        if size + DISK_RESERVE > shutil.disk_usage(tempfile.gettempdir()).free:
            raise HTTPException(507, "This ZIP exceeds available server space. Use the Google Drive import instead.")
        for key in list(_jobs):
            if len(_jobs) < 20:
                break
            if _jobs[key]["status"] in {"done", "failed"}:
                del _jobs[key]
        with tempfile.NamedTemporaryFile(prefix="vendor-zip-", suffix=".zip", delete=False) as destination:
            path = Path(destination.name)
        _jobs[upload_id] = {
            "owner": owner, "status": "uploading", "received": 0,
            "processed": 0, "filename": filename, "size": size,
            "created": time.time(), "path": path, "company": company,
            "read_pdf": read_pdf, "writing": False, "chunk_bytes": CHUNK_BYTES,
        }
        _active = upload_id
    return {"job_id": upload_id, "chunk_bytes": CHUNK_BYTES}


def _owned_job(job_id: str, owner: str) -> dict:
    job = _jobs.get(job_id)
    if not job or job["owner"] != owner:
        raise HTTPException(404, "This transfer is no longer on the server. Select the ZIP again to restart; saved documents are kept.")
    return job


@router.put("/{job_id}/chunk")
async def upload_chunk(job_id: str, offset: int, request: Request, owner: str = Depends(require_upload_token)):
    with _lock:
        job = _owned_job(job_id, owner)
        if job["status"] != "uploading" or job["writing"] or offset != job["received"]:
            raise HTTPException(409, "Check upload progress before retrying this chunk.")
        job["writing"] = True
    try:
        # Bound memory even if Content-Length is absent or incorrect.
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > CHUNK_BYTES:
                raise HTTPException(413, "Upload chunks must be at most 8 MiB.")
            body.extend(chunk)
        if not body or offset + len(body) > job["size"]:
            raise HTTPException(422, "The chunk does not match the selected ZIP size.")
        if len(body) + DISK_RESERVE > shutil.disk_usage(tempfile.gettempdir()).free:
            raise HTTPException(507, "Available temporary space is exhausted. Cancel this transfer and use Google Drive instead.")

        def write_chunk():
            with job["path"].open("r+b") as destination:
                destination.seek(offset)
                destination.write(body)
                destination.truncate()

        await run_in_threadpool(write_chunk)
        with _lock:
            job["received"] = offset + len(body)
        return {"received": job["received"], "status": "uploading"}
    finally:
        with _lock:
            job["writing"] = False


@router.post("/{job_id}/complete")
async def complete_upload(job_id: str, owner: str = Depends(require_upload_token)):
    global _active
    with _lock:
        job = _owned_job(job_id, owner)
        if job["status"] in {"importing", "done", "failed"}:
            return {"job_id": job_id, "status": job["status"]}
        if job["writing"] or job["received"] != job["size"]:
            raise HTTPException(409, "Finish uploading the ZIP before starting import.")
        job["writing"] = True
    try:
        if not await run_in_threadpool(zipfile.is_zipfile, job["path"]):
            with _lock:
                job.update(status="failed", error="The selected file is not a readable ZIP.")
                _active = None
            job["path"].unlink(missing_ok=True)
            raise HTTPException(422, job["error"])
        with _lock:
            job["status"] = "importing"
        worker = threading.Thread(
            target=_run_import,
            args=(job_id, job["path"], job["filename"], job["company"], job["read_pdf"]),
            name="vendor-zip-import", daemon=True,
        )
        worker.start()
        return {"job_id": job_id, "status": "importing"}
    finally:
        with _lock:
            job["writing"] = False


@router.delete("/{job_id}")
def cancel_upload(job_id: str, owner: str = Depends(require_upload_token)):
    global _active
    with _lock:
        job = _owned_job(job_id, owner)
        if job["status"] != "uploading" or job.get("writing"):
            raise HTTPException(409, "Only an idle, unfinished transfer can be cancelled.")
        job["path"].unlink(missing_ok=True)
        job.update(status="failed", error="Transfer cancelled. Saved documents are kept.")
        if _active == job_id:
            _active = None
    return {"status": "failed"}
