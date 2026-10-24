import re
import zipfile
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.drive import indexer, range_zip
from app.drive.index_store import DriveIndex


class Response:
    def __init__(self, payload, start, end, status=206):
        self.status_code = status
        self.headers = {'Content-Range':f'bytes {start}-{end}/{len(payload)}'}
        self.raw = BytesIO(payload[start:end+1])

    def __enter__(self):
        return self

    def __exit__(self,*args):
        pass


class Session:
    def __init__(self,payload,status=206):
        self.payload,self.status = payload,status
        self.received = 0

    def get(self,url,headers,**kwargs):
        match = re.fullmatch(r'bytes=(\d+)-(\d+)',headers['Range'])
        start,end = map(int,match.groups())
        self.received += end-start+1
        return Response(self.payload,start,end,self.status)

    def close(self):
        pass


@pytest.fixture
def index(tmp_path,monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'vendor_dashboard'))
    from vendor_dashboard.storage import Store
    indexer.CANCEL.clear()
    return DriveIndex(Store(tmp_path,initialize=False))


def test_range_zip_can_seek_without_downloading_large_payload(monkeypatch):
    payload = BytesIO()
    with zipfile.ZipFile(payload,'w',compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('Large/unused.txt', b'x'*(3*1024**2))
        archive.writestr('Alpha/list.csv', b'Company Name,Product,Price\nAlpha,Pump,100\n')
    session = Session(payload.getvalue())
    monkeypatch.setattr(range_zip.requests,'Session',lambda:session)
    with range_zip.DriveRangeFile('archive12345') as source, range_zip.archive_members(source) as archive:
        assert len(archive.infolist()) == 2
        assert b'Pump' in archive.read('Alpha/list.csv')
    assert session.received < len(payload.getvalue()) // 2


def test_range_read_refuses_server_that_sends_whole_archive(monkeypatch):
    monkeypatch.setattr(range_zip.requests,'Session',lambda:Session(b'archive',status=200))
    with pytest.raises(ValueError,match='partial read'):
        range_zip.DriveRangeFile('archive12345')


def test_changed_version_removes_stale_rows_and_resume_deduplicates(index):
    entry = {'entry_key':'source','folder_id':'folder123456','drive_id':'file1234567','source_name':'list.csv',
             'size_bytes':30,'version':'one'}
    index.discover([entry])
    index.save_records(entry,[{'company':'Old Company','product':'Pump','kind':'product'}])
    index.save_records(entry,[{'company':'Old Company','product':'Pump','kind':'product'}])
    assert index.search('folder123456')['total'] == 1
    index.discover([{**entry,'version':'two'}])
    assert index.search('folder123456')['total'] == 0


def test_full_index_preserves_originals_and_reports_unsafe_and_unsupported(index,monkeypatch):
    payload = BytesIO()
    with zipfile.ZipFile(payload,'w') as archive:
        archive.writestr('Alpha/list.csv','Company Name,Product,Price\nAlpha Medical,Surgical Pump,99\n')
        archive.writestr('../escape.csv','secret')
        archive.writestr('Alpha/photo.jpg',b'not-an-image')
    monkeypatch.setattr(range_zip.requests,'Session',lambda:Session(payload.getvalue()))
    monkeypatch.setattr(indexer,'list_public_files',lambda folder:[
        {'id':'archive12345','name':'vendors.zip','path':'vendors.zip','native':False},
        {'id':'unsupported7z','name':'data.7z','path':'data.7z','native':False}])
    indexer.run_index(index.store,'folder123456')
    result = index.status('folder123456')
    assert result['status'] == 'finished_with_issues'
    assert result['counts']['indexed'] == 1
    assert result['counts']['blocked'] == result['counts']['needs_ocr'] == result['counts']['unsupported'] == 1
    found = index.search('folder123456','Surgical Pump')
    assert found['total'] == 1
    record = found['items'][0]['record']
    assert record['company'] == 'Alpha Medical' and record['price'] == '99'
    assert record['member_path'] == 'Alpha/list.csv' and record['review_status'] == 'Needs review'
    assert not list(index.store.files_dir.rglob('*'))
    with index.store.connection() as db:
        tables = {r['name'] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert 'vdd_documents' not in tables
    indexer.run_index(index.store,'folder123456')
    assert index.search('folder123456')['total'] == 1


def test_pause_and_resume_retains_batches_without_duplicate_rows(index,monkeypatch):
    csv = 'Company Name,Product,Price\n'+''.join(f'Alpha,Item {n},{n}\n' for n in range(450))
    monkeypatch.setattr(indexer,'list_public_files',lambda folder:[
        {'id':'loosefile123','name':'list.csv','path':'list.csv','native':False}])

    def download(file_id,target):
        target.write_text(csv,encoding='utf-8')
        return len(csv),'hash'

    monkeypatch.setattr(indexer,'download_public_file',download)
    @contextmanager
    def metadata(file_id):
        yield SimpleNamespace(size=len(csv))
    monkeypatch.setattr(indexer,'DriveRangeFile',metadata)
    original = DriveIndex.save_records

    def pause_after_batch(self,entry,records):
        original(self,entry,records)
        indexer.CANCEL.set()

    with patch.object(DriveIndex,'save_records',pause_after_batch):
        indexer.run_index(index.store,'folder123456')
    assert index.status('folder123456')['status'] == 'paused'
    assert index.search('folder123456')['total'] == 200
    indexer.CANCEL.clear()
    indexer.run_index(index.store,'folder123456')
    assert index.status('folder123456')['status'] == 'done'
    assert index.search('folder123456')['total'] == 450


def test_xlsx_stream_reads_shared_strings_and_original_row_positions(tmp_path):
    import xlsxwriter
    path = tmp_path / 'source.xlsx'
    with xlsxwriter.Workbook(path) as book:
        sheet = book.add_worksheet('Medical products')
        sheet.write_row(0,0,['Company Name','Product Name','Price'])
        sheet.write_row(3,0,['Alpha Medical','Pump',120])
        sheet.write_row(4,0,['Beta Medical','Valve',20])
    rows = list(indexer.extracted_records(path))
    assert [row['company'] for row in rows] == ['Alpha Medical','Beta Medical']
    assert [row['row'] for row in rows] == [4,5]
    assert rows[0]['price'] == '120'


def test_index_api_requires_auth_before_opening_storage(index,monkeypatch):
    from fastapi.testclient import TestClient

    from app import main
    from app.drive import index_api
    settings = SimpleNamespace(dashboard_token='synthetic-drive-token',drive_folder_id='folder123456',drive_folder_url='')
    monkeypatch.setattr(index_api,'get_settings',lambda:settings)
    monkeypatch.setattr(index_api,'library',lambda:index)
    client = TestClient(main.app)
    assert client.get('/api/drive-index/search').status_code == 401
    response = client.get('/api/drive-index/status',headers={'X-Token':'synthetic-drive-token'})
    assert response.status_code == 200 and response.json()['records'] == 0
    monkeypatch.setenv('APP_PASSWORD','synthetic-library-password')
    from app.vendor_zip_upload import issue_upload_token
    scoped = issue_upload_token('synthetic-library-password')
    assert client.get('/api/drive-index/status',headers={'X-Upload-Token':scoped}).status_code == 200
    assert client.post('/api/drive-index/pause',headers={'X-Token':'synthetic-drive-token'}).status_code == 200
    indexer.CANCEL.clear()
