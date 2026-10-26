"""Authenticated shared Drive library, independent of the temporary Organizer DB."""
from __future__ import annotations

import hmac
import sys
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..config import get_settings, parse_drive_folder_id
from ..vendor_zip_upload import require_upload_token
from . import indexer
from .index_store import DriveIndex
from .source_links import parse_source


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


def selection(index):
    saved = index.source_selection()
    if saved:
        return saved
    folder = folder_id()
    source = {'kind':'folder','id':folder,'url':'https://drive.google.com/drive/folders/'+folder} if folder else None
    return {'scope':folder or 'shared-drive-library','source':source}


@router.get('/status')
def status():
    index = library()
    selected = selection(index)
    return {**index.status(selected['scope']),'source_link':(selected['source'] or {}).get('url',''),
            'storage':indexer.storage_status(index.store)}


class StartSource(BaseModel):
    link: str = Field(default='',max_length=2048)


@router.post('/start')
def start(body: StartSource | None = None):
    try:
        source = parse_source(body.link) if body and body.link.strip() else None
    except ValueError as error:
        raise HTTPException(422,str(error)) from None
    index = library()
    capacity = indexer.storage_status(index.store)
    if capacity and capacity['limit_reached']:
        raise HTTPException(507, 'Indexing is paused because the database storage safety limit is reached. Saved rows are kept. Resume cannot create more space; increase database capacity or safely free storage before retrying.')
    with indexer.LOCK:
        selected = selection(index)
        source = source or selected['source']
        if not source:
            raise HTTPException(422,'Paste a Google Drive folder or ZIP file link first.')
        if indexer.ACTIVE and indexer.ACTIVE.is_alive() and source != selected['source']:
            raise HTTPException(409,'Another Drive source is being indexed. Click Pause, wait for the current batch to finish, then submit your link again.')
        if indexer.ACTIVE and indexer.ACTIVE.is_alive():
            return {'started':False,'running':True}
        selected['source'] = source
        index.select_source(selected)
        indexer.start_index(index.store,selected['scope'],source)
    return {'started':True}


@router.post('/pause')
def pause():
    indexer.CANCEL.set()
    return {'pause_requested':True}


@router.get('/search')
def search(q: str='',entries: bool=False,offset: int=0):
    index = library()
    return index.search(selection(index)['scope'],q,entries,offset)
