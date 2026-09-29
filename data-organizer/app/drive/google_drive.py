"""Google Drive source (read-only).

* scope is ``drive.readonly``: the application can list and download, it cannot modify, move or delete anything
* auth: a service account (share the folder with its e-mail) OR an OAuth client (interactive consent once,
  token cached in ``secrets/token.json``). Credentials live in ``secrets/`` / the environment only - never in
  the database, the logs, the API responses or the exports
* the folder is walked recursively (shortcuts followed once, cycles impossible); Google Docs are exported to
  .docx and Google Sheets to .xlsx; files are downloaded to ``data/raw`` (cache keyed by file id + version)
  and every later stage reads only the cached copy
"""
from __future__ import annotations

import io
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from ..config import ConfigError, RuntimeConfig, Settings, parse_drive_folder_id
from ..ingestion.base import SourceFile
from ..ingestion.discovery import is_ignorable
from ..ingestion.registry import SUPPORTED_EXTENSIONS
from ..logging_setup import audit, get_logger

log = get_logger("extraction")
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
EXPORTS = {
    "application/vnd.google-apps.document": (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "application/vnd.google-apps.spreadsheet": (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
}
MIME_EXT = {
    "application/pdf": ".pdf", "text/csv": ".csv", "text/plain": ".txt", "application/json": ".json",
    "application/vnd.ms-excel": ".xls", "image/png": ".png", "image/jpeg": ".jpg", "image/tiff": ".tif",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}


def build_service(settings: Settings):
    """Authenticate and return a Drive v3 client."""
    from googleapiclient.discovery import build

    creds = None
    sa = settings.google_service_account_file
    if sa:
        if not Path(sa).exists():
            raise ConfigError(f"Service-account file not found: {sa}")
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(str(sa), scopes=SCOPES)
    else:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        token = settings.google_token_file
        if token.exists():
            creds = Credentials.from_authorized_user_file(str(token), SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token.write_text(creds.to_json())
        if not creds or not creds.valid:
            raise ConfigError(
                "Google Drive is not authorised yet. Run `python -m app drive-auth` once (needs secrets/client_secret.json), "
                "or set GOOGLE_SERVICE_ACCOUNT_FILE and share the folder with the service account."
            )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def run_oauth_flow(settings: Settings) -> Path:
    """Interactive one-time consent (opens a browser). Stores the refresh token in secrets/token.json."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not settings.google_client_secrets_file.exists():
        raise ConfigError(f"OAuth client file not found: {settings.google_client_secrets_file} "
                          "(create an OAuth 'Desktop app' client in Google Cloud Console and save its JSON there)")
    flow = InstalledAppFlow.from_client_secrets_file(str(settings.google_client_secrets_file), SCOPES)
    creds = flow.run_local_server(port=0)
    settings.google_token_file.parent.mkdir(parents=True, exist_ok=True)
    settings.google_token_file.write_text(creds.to_json())
    try:
        settings.google_token_file.chmod(0o600)
    except OSError:
        pass
    audit("drive_authorised")
    return settings.google_token_file


def drive_status(settings: Settings, cfg: RuntimeConfig) -> dict[str, Any]:
    folder = ""
    try:
        folder = parse_drive_folder_id(cfg.drive_folder or settings.drive_folder_id or settings.drive_folder_url)
    except ConfigError as exc:
        return {"configured": False, "message": str(exc)}
    return {
        "folder_id": folder, "folder_configured": bool(folder),
        "oauth_client_file": settings.google_client_secrets_file.exists(),
        "token_present": settings.google_token_file.exists(),
        "service_account": bool(settings.google_service_account_file),
        "ready_to_connect": bool(folder) and (settings.google_token_file.exists() or bool(settings.google_service_account_file)),
        "help": "Run `python -m app drive-auth` once to authorise (read-only), then press Scan.",
    }


class DriveSource:
    kind = "drive"

    def __init__(self, settings: Settings, folder_id: str, service: Any = None):
        self.settings, self.folder_id = settings, folder_id
        self._service = service
        self.cache = settings.raw_dir / "drive"

    @property
    def service(self):
        if self._service is None:
            self._service = build_service(self.settings)
        return self._service

    def describe(self) -> str:
        return f"drive:{self.folder_id}"

    # ------------------------------------------------------------------ listing
    def _list(self, parent: str) -> Iterator[dict[str, Any]]:
        token = None
        while True:
            resp = self.service.files().list(
                q=f"'{parent}' in parents and trashed = false", pageSize=1000, pageToken=token,
                fields="nextPageToken, files(id, name, mimeType, size, modifiedTime, md5Checksum, shortcutDetails)",
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute(num_retries=5)
            yield from resp.get("files", [])
            token = resp.get("nextPageToken")
            if not token:
                return

    def scan(self) -> Iterator[SourceFile]:
        seen: set[str] = set()
        stack: list[tuple[str, str]] = [(self.folder_id, "")]
        while stack:
            folder, prefix = stack.pop()
            if folder in seen:
                continue
            seen.add(folder)
            try:
                items = list(self._list(folder))
            except Exception as exc:
                raise ConfigError(f"Cannot list Drive folder '{folder}': {type(exc).__name__}. Check the folder id and that it is shared with the authorised account.") from exc
            for it in sorted(items, key=lambda x: x["name"].lower()):
                mime, name, fid = it["mimeType"], it["name"], it["id"]
                if mime == SHORTCUT and it.get("shortcutDetails"):
                    fid, mime = it["shortcutDetails"]["targetId"], it["shortcutDetails"]["targetMimeType"]
                if mime == FOLDER:
                    stack.append((fid, f"{prefix}{name}/"))
                    continue
                if is_ignorable(name):
                    continue
                ext = Path(name).suffix.lower()
                if mime in EXPORTS:
                    ext = EXPORTS[mime][0]
                    name = name if name.lower().endswith(ext) else name + ext
                elif not ext:
                    ext = MIME_EXT.get(mime, "")
                yield SourceFile(
                    file_key=f"drive:{fid}", name=name, path=f"{prefix}{name}", local_path=self.cache / f"{fid}{ext}", origin="drive",
                    ext=ext, size=int(it.get("size") or 0), modified=it.get("modifiedTime", ""), mime=mime,
                    fingerprint="", drive_id=fid, supported=ext in SUPPORTED_EXTENSIONS,
                )

    # ------------------------------------------------------------------ download
    def ensure_local(self, sf: SourceFile) -> SourceFile:
        from googleapiclient.http import MediaIoBaseDownload

        self.cache.mkdir(parents=True, exist_ok=True)
        meta = sf.local_path.with_suffix(sf.local_path.suffix + ".json")
        version = f"{sf.modified}|{sf.size}"
        if sf.local_path.exists() and meta.exists():
            try:
                if json.loads(meta.read_text()).get("version") == version:
                    return sf
            except ValueError:
                pass
        if sf.mime in EXPORTS:
            request = self.service.files().export_media(fileId=sf.drive_id, mimeType=EXPORTS[sf.mime][1])
        else:
            request = self.service.files().get_media(fileId=sf.drive_id, supportsAllDrives=True)
        tmp = sf.local_path.with_name(sf.local_path.name + ".part")
        with io.FileIO(tmp, "wb") as fh:
            dl = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = dl.next_chunk(num_retries=5)
        tmp.replace(sf.local_path)
        meta.write_text(json.dumps({"version": version, "downloaded": datetime.now().isoformat()}))
        log.info(f"downloaded {sf.path} ({sf.local_path.stat().st_size} bytes)")
        return sf
