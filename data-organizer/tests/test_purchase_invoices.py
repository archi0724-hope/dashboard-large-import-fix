import io
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app import invoice_extraction as extraction
from app import purchase_invoices as invoices
from app.drive.range_zip import DriveRangeFile, archive_members
from app.vendor_zip_upload import issue_upload_token


def workbook(path):
    book = Workbook()
    sheet = book.active
    for row in [['Invoice No','INV-42'],['Supplier','Example Medical'],['Invoice Date','2026-10-24'],
                ['Grand Total','250'],['Currency','INR'],['Description','Qty','Rate','Amount'],
                ['Surgical gloves','5','50','250']]:
        sheet.append(row)
    book.save(path)
    return path


def test_excel_extracts_reviewable_fields_and_lines(tmp_path):
    draft = extraction.extract(workbook(tmp_path / 'invoice.xlsx'))
    assert draft['fields']['invoice_number'] == 'INV-42'
    assert draft['fields']['supplier'] == 'Example Medical'
    assert draft['fields']['total'] == '250'
    assert draft['items'][0]['description'] == 'Surgical gloves'
    assert draft['items'][0]['quantity'] == '5'


def test_csv_horizontal_metadata_does_not_treat_headers_as_values(tmp_path):
    path = tmp_path / 'invoice.csv'
    path.write_text('Invoice No,Supplier,Invoice Date\nINV-99,Example,2026-10-24\nDescription,Qty,Amount\nGloves,2,100\n')
    draft = extraction.extract(path)
    assert draft['fields']['invoice_number'] == 'INV-99'
    assert draft['fields']['supplier'] == 'Example'


def test_unlabelled_text_does_not_invent_financial_values():
    draft = extraction.parse_text('Example Medical\nGloves 500\nTotal tax 50')
    assert draft['total'] == ''
    assert draft['currency'] == ''


def test_native_pdf_text_extraction(tmp_path):
    from reportlab.pdfgen import canvas
    path = tmp_path / 'invoice.pdf'
    pdf = canvas.Canvas(str(path))
    for index,line in enumerate(['Invoice No: PDF-42','Supplier: Example Medical','Invoice Date: 2026-10-24','Grand Total: INR 250.00']):
        pdf.drawString(50,750-index*25,line)
    pdf.save()
    draft = extraction.extract(path)
    assert draft['fields']['invoice_number'] == 'PDF-42'
    assert draft['fields']['total'] == '250.00'


def test_scanned_pdf_renders_for_ocr(tmp_path,monkeypatch):
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    path = tmp_path / 'scan.pdf'
    pdf = canvas.Canvas(str(path))
    pdf.drawImage(ImageReader(Image.new('RGB',(100,100),'white')),50,500,200,200)
    pdf.save()
    monkeypatch.setattr(extraction,'image_text',lambda image,work:'Invoice No: SCAN-42\nSupplier: Example')
    assert extraction.extract(path)['fields']['invoice_number'] == 'SCAN-42'


@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'vendor_dashboard'))
    from vendor_dashboard.storage import Store
    store = Store(tmp_path / 'store',initialize=False)
    monkeypatch.setenv('APP_PASSWORD','invoice-test-password')
    monkeypatch.setattr(invoices,'library',lambda:SimpleNamespace(store=store))
    with invoices.LOCK:
        invoices.JOBS.clear()
        assert invoices.ACTIVE is None
    app = FastAPI()
    app.include_router(invoices.router)
    yield TestClient(app),{'X-Upload-Token':issue_upload_token('invoice-test-password')}
    for _ in range(300):
        if invoices.ACTIVE is None:
            break
        time.sleep(.02)
    assert invoices.ACTIVE is None


def test_extract_review_save_search_and_duplicate(client,tmp_path):
    c,headers = client
    content = workbook(tmp_path / 'invoice.xlsx').read_bytes()
    job = c.post('/api/purchase-invoices/extract?filename=invoice.xlsx',headers=headers,content=content).json()['job_id']
    for _ in range(300):
        result = c.get('/api/purchase-invoices/jobs/'+job,headers=headers).json()
        if result['status'] in {'ready','failed'}:
            break
        time.sleep(.03)
    assert result['status'] == 'ready',result
    other = {'X-Upload-Token':issue_upload_token('invoice-test-password')}
    assert c.get('/api/purchase-invoices/jobs/'+job,headers=other).status_code == 404
    body = {'job_id':job,'reviewed':False,**{key:result['draft'][key] for key in ['fields','items']}}
    assert c.post('/api/purchase-invoices/save',headers=headers,json=body).status_code == 422
    body['reviewed'] = True
    body['fields']['supplier'] = 'Reviewed Medical'
    saved = c.post('/api/purchase-invoices/save',headers=headers,json=body)
    assert saved.status_code == 200,saved.text
    assert saved.json()['duplicate'] is False
    body['fields']['supplier'] = 'Should not overwrite'
    assert c.post('/api/purchase-invoices/save',headers=headers,json=body).json()['duplicate'] is True
    found = c.get('/api/purchase-invoices/records?q=Reviewed',headers=other).json()
    assert found['total'] == 1
    record = c.get('/api/purchase-invoices/records/'+saved.json()['id'],headers=other).json()
    assert record['fields']['supplier'] == 'Reviewed Medical'
    assert record['items'][0]['description'] == 'Surgical gloves'


def test_authorization_and_size_limits_before_extract(client,monkeypatch):
    c,headers = client
    assert c.get('/api/purchase-invoices/records').status_code == 401
    assert c.post('/api/purchase-invoices/extract?filename=invoice.xlsx',headers={**headers,'Content-Length':str(21*1024**2)},content=b'x').status_code == 413
    assert c.post('/api/purchase-invoices/extract?filename=collection.zip',headers=headers,content=b'x').status_code == 422
    assert c.post('/api/purchase-invoices/extract?filename=invoice.csv',headers=headers,content=b'').status_code == 422


def test_numeric_review_rejects_nan():
    with pytest.raises(ValueError):
        invoices.Fields(invoice_number='1',supplier='Example',total='NaN')


def test_worker_failure_deletes_upload_and_releases_slot(client,monkeypatch):
    c,headers = client
    paths = []
    factory = invoices.tempfile.NamedTemporaryFile

    def track(**kwargs):
        result = factory(**kwargs)
        paths.append(Path(result.name))
        return result

    monkeypatch.setattr(invoices.tempfile,'NamedTemporaryFile',track)
    job = c.post('/api/purchase-invoices/extract?filename=broken.xlsx',headers=headers,content=b'not a workbook').json()['job_id']
    for _ in range(300):
        result = c.get('/api/purchase-invoices/jobs/'+job,headers=headers).json()
        if result['status'] == 'failed' and invoices.ACTIVE is None:
            break
        time.sleep(.02)
    assert result['status'] == 'failed'
    assert invoices.ACTIVE is None
    assert paths and not paths[0].exists()


def test_zip64_beyond_50_gib_uses_only_small_remote_reads(monkeypatch,tmp_path):
    # A sparse, virtual archive tests real ZIP64 offsets without allocating 51 GiB.
    class Sparse(io.RawIOBase):
        position = 0
        blocks = None

        def __init__(self):
            self.blocks = []

        def tell(self):
            return self.position

        def seek(self,offset,whence=0):
            self.position = offset if whence == 0 else self.position + offset
            return self.position

        def write(self,data):
            self.blocks.append((self.position,bytes(data)))
            self.position += len(data)
            return len(data)

        def read_range(self,start,end):
            output = bytearray(end-start+1)
            for position,data in self.blocks:
                left,right = max(position,start),min(position+len(data),end+1)
                if right > left:
                    output[left-start:right-start] = data[left-position:right-position]
            return bytes(output)

    sparse = Sparse()
    sparse.seek(51*1024**3)
    with zipfile.ZipFile(sparse,'w',allowZip64=True) as archive:
        archive.writestr('company/products.csv','Company,Product,Price\nLarge Medical,Pump,99\n')
    size = sparse.position
    reads = []

    def read_range(remote,start,end):
        reads.append(end-start+1)
        return sparse.read_range(start,end),size

    monkeypatch.setattr(DriveRangeFile,'_range',read_range)
    with DriveRangeFile('syntheticzip123') as remote,archive_members(remote) as archive:
        assert remote.size > 50*1024**3
        assert archive.read('company/products.csv') == b'Company,Product,Price\nLarge Medical,Pump,99\n'
    assert sum(reads) < 1024**2
    from app.drive import indexer
    from app.drive.index_store import DriveIndex
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'vendor_dashboard'))
    from vendor_dashboard.storage import Store
    index = DriveIndex(Store(tmp_path / 'index',initialize=False))
    monkeypatch.setattr(indexer,'list_public_files',lambda folder:[{
        'id':'syntheticzip123','name':'large.zip','path':'large.zip','native':False}])
    indexer.run_index(index.store,'folder123456')
    assert index.status('folder123456')['status'] == 'done'
    found = index.search('folder123456','Pump')
    assert found['total'] == 1
    assert found['items'][0]['record']['company'] == 'Large Medical'
    assert sum(reads) < 2 * 1024**2
