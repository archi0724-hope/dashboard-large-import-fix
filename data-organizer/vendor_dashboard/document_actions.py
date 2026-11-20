"""On-demand original previews and explicit single-document removal."""
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from io import BytesIO

import streamlit as st

from app.config import get_settings
from app.drive.google_drive import EXPORTS, build_service, has_credentials, list_account_files
from app.drive.index_store import DriveIndex
from app.drive.indexer import MAX_DOCUMENT, source_copy


@st.cache_data(ttl=30, max_entries=1, show_spinner=False)
def drive_account_files():
    settings = get_settings()
    if not has_credentials(settings):
        return None
    return list_account_files(settings)


@st.cache_data(ttl=120, max_entries=2, show_spinner=False)
def drive_account_file_bytes(file_id, mime_type, size_bytes):
    if size_bytes and size_bytes > MAX_DOCUMENT:
        raise ValueError('Document exceeds the 128 MB preview limit. Open the original in Drive.')
    service = build_service(get_settings())
    if mime_type in EXPORTS:
        request = service.files().export_media(fileId=file_id, mimeType=EXPORTS[mime_type][1])
    else:
        request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    from googleapiclient.http import MediaIoBaseDownload

    output = BytesIO()
    downloader = MediaIoBaseDownload(output, request, chunksize=4 * 1024**2)
    done = False
    while not done:
        _, done = downloader.next_chunk(num_retries=5)
        if output.tell() > MAX_DOCUMENT:
            raise ValueError('Document exceeds the 128 MB preview limit. Open the original in Drive.')
    return output.getvalue()


def delete_drive_original(store, drive_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{10,200}', drive_id or ''):
        raise ValueError('Invalid Google Drive file ID.')
    service = build_service(get_settings())
    service.files().delete(fileId=drive_id, supportsAllDrives=True).execute(num_retries=5)
    try:
        DriveIndex(store).remove_drive_file(drive_id)
    except Exception as error:
        raise RuntimeError(
            'The Drive original was deleted, but its dashboard index could not be refreshed. Contact the dashboard administrator.'
        ) from error
    drive_account_files.clear()
    drive_account_file_bytes.clear()
    from drive_view import inventory, source_rows
    inventory.clear()
    source_rows.clear()


@st.cache_data(ttl=120, max_entries=2, show_spinner=False)
def drive_document_bytes(drive_id, member, version=''):
    # Read just one bounded member, never the complete source archive.
    with TemporaryDirectory(prefix='vendor-preview-') as directory:
        target = Path(directory) / 'document'
        with source_copy({'drive_id':drive_id,'member_path':member}, target) as source:
            if source.stat().st_size > MAX_DOCUMENT:
                raise ValueError('Document exceeds the 128 MB preview limit. Open the original in Drive.')
            return source.read_bytes()


def preview_bytes(store, doc):
    if not getattr(doc, 'drive_url', ''):
        return store.read_bytes(int(doc.id))
    drive_id = urlsplit(doc.drive_url).path.split('/')[3]
    return drive_document_bytes(drive_id, getattr(doc, 'drive_member', ''), doc.file_hash)


def remove_document(store, document_id):
    if str(document_id).startswith('drive:'):
        DriveIndex(store).remove_entry(str(document_id)[6:])
    else:
        store.delete_document(int(document_id))
    from drive_view import inventory, source_rows
    inventory.clear()
    source_rows.clear()
    drive_document_bytes.clear()
