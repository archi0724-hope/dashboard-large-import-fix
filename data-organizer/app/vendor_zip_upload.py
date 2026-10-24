"""Authenticated, disk-backed Vendor ZIP upload and import progress."""

from __future__ import annotations

import hashlib
import hmac
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
MAX_UPLOAD_BYTES = 2 * 1024**3
DISK_RESERVE = 64 * 1024**2
TOKEN_LIFETIME = 3 * 3600
_lock = threading.RLock()
_jobs: dict[str, dict] = {}
_active: str | None = None


def issue_upload_token(password: str) -> str:
    payload = f"{int(time.time()) + TOKEN_LIFETIME}:{secrets.token_hex(16)}"
    signature = hmac.new(
        password.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return payload + ":" + signature


def require_upload_token(request: Request) -> str:
    from hospkart_chat.integration import vendor_setting

    password = vendor_setting("APP_PASSWORD")
    token = request.headers.get("x-upload-token", "")
    try:
        expiry, nonce, signature = token.split(":")
        payload = expiry + ":" + nonce
        expected = hmac.new(
            password.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        valid = (
            password
            and int(time.time()) <= int(expiry) <= int(time.time()) + TOKEN_LIFETIME
        )
        valid = valid and hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise HTTPException(
            401,
            "Your upload session expired. Return to the dashboard and choose Upload ZIP again.",
        )
    return hashlib.sha256(token.encode()).hexdigest()


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
    owner: str = Depends(require_upload_token),
):
    global _active
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
    if length < 0 or length > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Choose a ZIP no larger than 2 GB.")
    folder = Path(tempfile.gettempdir())
    if length + DISK_RESERVE > shutil.disk_usage(folder).free:
        raise HTTPException(
            507, "Not enough temporary disk space for this ZIP. Try a smaller batch."
        )
    with _lock:
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
        job_id = secrets.token_hex(16)
        _active = job_id
        _jobs[job_id] = {
            "owner": owner,
            "status": "uploading",
            "processed": 0,
            "received": 0,
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
                if received > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "Choose a ZIP no larger than 2 GB.")
                if received >= disk_check:
                    if len(chunk) + DISK_RESERVE > shutil.disk_usage(folder).free:
                        raise HTTPException(
                            507, "Temporary disk is full. Try a smaller batch."
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


@router.get("/{job_id}")
def upload_status(job_id: str, owner: str = Depends(require_upload_token)):
    with _lock:
        job = _jobs.get(job_id)
        if not job or job["owner"] != owner:
            raise HTTPException(
                404,
                "Upload not found. If the server restarted, retry the ZIP; saved documents are kept.",
            )
        return {key: value for key, value in job.items() if key != "owner"}
