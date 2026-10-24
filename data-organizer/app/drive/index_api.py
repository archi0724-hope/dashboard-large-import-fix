"""Authenticated shared Drive library, independent of the temporary Organizer DB."""
from __future__ import annotations

import hmac
import sys
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, Request

from ..config import get_settings, parse_drive_folder_id
from ..vendor_zip_upload import require_upload_token
from . import indexer
from .index_store import DriveIndex


def authorize(request: Request):
    expected = get_settings().dashboard_token
    supplied = request.headers.get('x-token','')
    if expected and supplied and hmac.compare_digest(expected,supplied):
        return
    require_upload_token(request)


router = APIRouter(prefix='/api/drive-index',dependencies=[Depends(authorize)])


@lru_cache(maxsize=1)
def library():
    from hospkart_chat.integration import vendor_setting
    vendor_root = Path(__file__).resolve().parents[2] / 'vendor_dashboard'
    if str(vendor_root) not in sys.path:
        sys.path.append(str(vendor_root))
    from vendor_dashboard.storage import Store
    folder = vendor_setting('VENDOR_DATA_DIR',str(Path(__file__).resolve().parents[2] / 'vendor_dashboard' / 'vendor_data'))
    store = Store(folder,vendor_setting('DATABASE_URL'),initialize=False)
    return DriveIndex(store)


def folder_id():
    settings = get_settings()
    return parse_drive_folder_id(settings.drive_folder_id or settings.drive_folder_url)


@router.get('/status')
def status():
    return library().status(folder_id())


@router.post('/start')
def start():
    index = library()
    indexer.start_index(index.store,folder_id())
    return {'started':True}


@router.post('/pause')
def pause():
    indexer.CANCEL.set()
    return {'pause_requested':True}


@router.get('/search')
def search(q: str='',entries: bool=False,offset: int=0):
    return library().search(folder_id(),q,entries,offset)
