"""Reviewed, shared purchase invoices; extraction runs outside the web process."""
from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from .drive.index_api import authorize, library
from .invoice_extraction import ALLOWED, MAX_BYTES

router = APIRouter(prefix='/api/purchase-invoices', dependencies=[Depends(authorize)])
LOCK = threading.RLock()
JOBS: dict[str, dict] = {}
ACTIVE = None
SCHEMA = """CREATE TABLE IF NOT EXISTS vdd_purchase_invoices (
    id TEXT PRIMARY KEY, source_hash TEXT NOT NULL UNIQUE, filename TEXT NOT NULL,
    record_json TEXT NOT NULL, fields_json TEXT NOT NULL, search_text TEXT NOT NULL, created_at TEXT NOT NULL)"""


def owner(request):
    return hashlib.sha256((request.headers.get('x-upload-token') or request.headers.get('x-token','')).encode()).hexdigest()


def database():
    store = library().store
    with store.connection() as db:
        db.execute(SCHEMA)
    return store


def invoice_count(store):
    """Count confirmed shared records for the Vendor summary card."""
    with store.connection() as db:
        db.execute(SCHEMA)
        return int(db.execute('SELECT COUNT(*) AS n FROM vdd_purchase_invoices').fetchone()['n'])


def job_for(job_id, request):
    with LOCK:
        job = JOBS.get(job_id)
        if not job or job['owner'] != owner(request):
            raise HTTPException(404, 'Draft not found. Extract the invoice again if the server restarted.')
        return job.copy()


def extract_worker(job_id, path):
    global ACTIVE
    try:
        result = subprocess.run([sys.executable,'-m','app.invoice_extraction',str(path)],
            cwd=str(Path(__file__).resolve().parents[1]), check=False, capture_output=True, timeout=150,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        data = json.loads(result.stdout)
        with LOCK:
            JOBS[job_id].update(status='ready' if data.get('ok') else 'failed',
                draft=data.get('draft'),error=data.get('error',''))
    except Exception:  # noqa: BLE001 - isolate worker failures without logging invoice contents
        with LOCK:
            JOBS[job_id].update(status='failed', error='Extraction stopped or timed out. Try a smaller invoice or an Excel version.')
    finally:
        path.unlink(missing_ok=True)
        with LOCK:
            if ACTIVE == job_id:
                ACTIVE = None


@router.post('/extract')
async def upload(request: Request, filename: str):
    global ACTIVE
    if len(filename) > 250 or '/' in filename or '\\' in filename or Path(filename).suffix.lower() not in ALLOWED:
        raise HTTPException(422,'Choose a PDF, JPG, PNG, XLSX, XLS or CSV invoice.')
    try:
        length = int(request.headers.get('content-length','0'))
    except ValueError:
        raise HTTPException(400,'Invalid upload length.') from None
    if length < 0 or length > MAX_BYTES:
        raise HTTPException(413,'Each invoice must be 20 MB or smaller.')
    if shutil.disk_usage(tempfile.gettempdir()).free < MAX_BYTES + 64 * 1024**2:
        raise HTTPException(507,'Temporary storage is busy. Try again later.')
    with LOCK:
        if ACTIVE:
            raise HTTPException(409,'Another invoice is being extracted. Please wait and retry.')
        for key in list(JOBS):
            if time.time() - JOBS[key]['created'] > 3600 or len(JOBS) >= 8:
                del JOBS[key]
        job_id = secrets.token_hex(16)
        ACTIVE = job_id
        JOBS[job_id] = {'owner':owner(request),'created':time.time(),'status':'uploading','filename':filename}
    path = None
    try:
        digest = hashlib.sha256()
        total = 0
        with tempfile.NamedTemporaryFile(prefix='purchase-invoice-',suffix=Path(filename).suffix.lower(),delete=False) as target:
            path = Path(target.name)
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_BYTES:
                    raise HTTPException(413,'Each invoice must be 20 MB or smaller.')
                target.write(chunk)
                digest.update(chunk)
        if not total:
            raise HTTPException(422,'The invoice file is empty.')
        with LOCK:
            JOBS[job_id].update(status='extracting',source_hash=digest.hexdigest())
        threading.Thread(target=extract_worker,args=(job_id,path),daemon=True).start()
        return {'job_id':job_id}
    except BaseException:
        if path:
            path.unlink(missing_ok=True)
        with LOCK:
            JOBS.pop(job_id,None)
            ACTIVE = None
        raise


@router.get('/jobs/{job_id}')
def progress(job_id: str, request: Request):
    job = job_for(job_id,request)
    return {key:job[key] for key in ('status','draft','error','filename') if key in job}


def amount(value):
    value = value.strip().replace(',','')
    if value:
        try:
            number = Decimal(value)
            if not number.is_finite() or abs(number) > Decimal('1e15'):
                raise InvalidOperation
        except InvalidOperation:
            raise ValueError('Enter a valid number, or leave the amount blank.') from None
    return value


class Fields(BaseModel):
    invoice_number: str = Field(max_length=200)
    invoice_date: str = Field(default='',max_length=200)
    supplier: str = Field(max_length=200)
    gstin: str = Field(default='',max_length=200)
    total: str = Field(default='',max_length=200)
    currency: str = Field(default='',max_length=20)

    @field_validator('total')
    @classmethod
    def validate_total(cls,value):
        return amount(value)

    @field_validator('supplier','invoice_number')
    @classmethod
    def required(cls,value):
        if not value.strip():
            raise ValueError('Supplier and invoice number are required after review.')
        return value.strip()


class Item(BaseModel):
    description: str = Field(max_length=2000)
    quantity: str = Field(default='',max_length=100)
    unit_price: str = Field(default='',max_length=100)
    amount: str = Field(default='',max_length=100)

    @field_validator('quantity','unit_price','amount')
    @classmethod
    def numeric(cls,value):
        return amount(value)


class Reviewed(BaseModel):
    job_id: str = Field(max_length=64)
    reviewed: bool
    fields: Fields
    items: list[Item] = Field(default_factory=list,max_length=10000)


@router.post('/save')
def save(body: Reviewed, request: Request):
    if not body.reviewed:
        raise HTTPException(422,'Review the extracted values and confirm before saving.')
    job = job_for(body.job_id,request)
    if job['status'] != 'ready':
        raise HTTPException(409,'Wait for extraction to finish before saving.')
    record = {'fields':body.fields.model_dump(),'items':[item.model_dump() for item in body.items],
        'raw_text':job['draft']['raw_text'],'filename':job['filename']}
    encoded = json.dumps(record,ensure_ascii=False)
    if len(encoded.encode()) > 2 * 1024**2:
        raise HTTPException(413,'This invoice has too many line items. Split it into individual invoices.')
    store = database()
    ident = secrets.token_hex(16)
    created = datetime.now(UTC).isoformat()
    with store.connection() as db:
        existing = db.execute('SELECT id FROM vdd_purchase_invoices WHERE source_hash=?',(job['source_hash'],)).fetchone()
        if existing:
            return {'id':existing['id'],'duplicate':True}
        if store.cloud and db.execute('SELECT pg_database_size(current_database()) AS n').fetchone()['n'] >= 850 * 1024**2:
            raise HTTPException(507,'The shared database is nearly full. Increase its storage allowance before saving more invoices.')
        db.execute('INSERT INTO vdd_purchase_invoices (id,source_hash,filename,record_json,fields_json,search_text,created_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT (source_hash) DO NOTHING',
            (ident,job['source_hash'],job['filename'],encoded,json.dumps(record['fields']),encoded.casefold(),created))
        row = db.execute('SELECT id FROM vdd_purchase_invoices WHERE source_hash=?',(job['source_hash'],)).fetchone()
    return {'id':row['id'],'duplicate':row['id'] != ident}


@router.get('/records')
def search(q: str='', offset: int=0):
    offset = max(0,offset)
    query = '%' + q[:500].casefold().replace('!','!!').replace('%','!%').replace('_','!_') + '%'
    with database().connection() as db:
        total = db.execute("SELECT COUNT(*) AS n FROM vdd_purchase_invoices WHERE search_text LIKE ? ESCAPE '!'",(query,)).fetchone()['n']
        rows = db.execute("SELECT id,filename,fields_json,created_at FROM vdd_purchase_invoices WHERE search_text LIKE ? ESCAPE '!' ORDER BY created_at DESC,id LIMIT 25 OFFSET ?",(query,offset)).fetchall()
    return {'total':total,'items':[{'id':row['id'],'filename':row['filename'],'created_at':row['created_at'],
        'fields':json.loads(row['fields_json'])} for row in rows]}


@router.get('/records/{ident}')
def detail(ident: str):
    with database().connection() as db:
        row = db.execute('SELECT record_json FROM vdd_purchase_invoices WHERE id=?',(ident,)).fetchone()
    if not row:
        raise HTTPException(404,'Invoice not found.')
    return json.loads(row['record_json'])
