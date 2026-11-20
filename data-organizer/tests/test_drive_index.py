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


def test_selected_zip_also_resumes_other_saved_sources_and_reuses_directory(index, monkeypatch):
    payload = BytesIO()
    with zipfile.ZipFile(payload, 'w') as archive:
        archive.writestr('Alpha/one.txt', 'First searchable document')
        archive.writestr('Alpha/two.txt', 'Second searchable document')
    opened = []
    def session():
        opened.append(True)
        return Session(payload.getvalue())
    monkeypatch.setattr(range_zip.requests, 'Session', session)
    index.discover([{'entry_key':'older-file','folder_id':'folder123456','drive_id':'olderzip12345',
                    'source_name':'older.zip','member_path':'Alpha/one.txt','size_bytes':25,'version':'1'}])
    indexer.run_index(index.store, 'folder123456',
                      {'kind':'zip','id':'currentzip12345','url':'https://drive.google.com/file/d/currentzip12345/view'})
    assert index.status('folder123456')['counts']['indexed'] == 3
    assert index.existing('older-file')['status'] == 'indexed'
    # One discovery reader plus one reader for each of the two archives.
    assert len(opened) == 3
    assert index.search('folder123456')['total'] == 2  # Identical text copied into both ZIPs is grouped.
    assert index.status('folder123456')['raw_records'] == 3


def test_fresh_pending_entries_are_processed_before_failed_retries(index):
    base = {'folder_id':'folder123456','drive_id':'archive12345','source_name':'one.zip','version':'1'}
    index.discover([{**base,'entry_key':'failed','member_path':'small.txt','size_bytes':1,'status':'failed'},
                    {**base,'entry_key':'pending','member_path':'larger.txt','size_bytes':100,'status':'pending'}])
    assert [entry['entry_key'] for entry in index.pending('folder123456')] == ['pending','failed']


def test_neon_worker_lease_uses_direct_connection_without_changing_web_pool():
    from psycopg.conninfo import conninfo_to_dict
    original = 'postgresql://synthetic:password@ep-synthetic-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require'
    store = SimpleNamespace(cloud=True, database_url=original)
    direct = indexer.lease_store(store)
    params = conninfo_to_dict(direct.database_url)
    assert params['host'] == 'ep-synthetic.ap-southeast-1.aws.neon.tech'
    assert params['user'] == 'synthetic' and params['dbname'] == 'neondb'
    assert store.database_url == original and direct is not store
    assert indexer.lease_store(SimpleNamespace(cloud=False)).cloud is False


def test_changed_version_removes_stale_rows_and_resume_deduplicates(index):
    entry = {'entry_key':'source','folder_id':'folder123456','drive_id':'file1234567','source_name':'list.csv',
             'size_bytes':30,'version':'one'}
    index.discover([entry])
    index.save_records(entry,[{'company':'Old Company','product':'Pump','kind':'product'}])
    index.save_records(entry,[{'company':'Old Company','product':'Pump','kind':'product'}])
    assert index.search('folder123456')['total'] == 1
    index.discover([{**entry,'version':'two'}])
    assert index.search('folder123456')['total'] == 0


def test_product_search_labels_existing_rows_without_reindexing_or_mutating_source(index):
    import json

    entry = indexer.top_entry({'id':'polar', 'name':'catalogue.csv', 'path':'catalogue.csv'}, 'folder')
    index.discover([entry])
    record = {'kind':'product', 'company':'', 'product':'Actuator', 'price':'',
              'member_path':'organized_by_company/Polar Automation/POLAR AUTOMATION PRICE LIST.csv',
              'values':{'Regular Price':'14080','Sales Price':'12800'}}
    index.save_records(entry,[record])
    result = index.search('folder','Polar Automation',kind='product')
    assert result['total'] == 1
    assert result['items'][0]['company'] == 'Polar Automation'
    assert result['items'][0]['record']['price'] == '12800'
    with index.store.connection() as db:
        saved = db.execute('SELECT company,record_json FROM vdd_drive_records').fetchone()
        assert saved['company'] == '' and json.loads(saved['record_json']) == record


def test_product_search_is_paginated_and_does_not_publish_unreviewed_rows(index):
    entry = {'entry_key':'catalogue','folder_id':'folder123456','drive_id':'file1234567',
             'source_name':'products.csv','size_bytes':30,'version':'one'}
    index.discover([entry])
    index.save_records(entry,[{'company':'Alpha Medical','kind':'product','product':'Pump','price':'120'},
                              {'company':'Alpha Medical','kind':'product','product':'Valve','price':'50'},
                              {'company':'Alpha Medical','kind':'document','text':'Unrelated document'}])
    result = index.search('folder123456',kind='product',limit=1,offset=1)
    assert result['total'] == 2 and len(result['items']) == 1
    assert result['items'][0]['product'] == 'Valve'
    assert result['items'][0]['record']['price'] == '50'
    assert index.search('folder123456','Pump',kind='product')['total'] == 1
    with index.store.connection() as db:
        assert db.execute("SELECT count(*) AS n FROM sqlite_master WHERE name='vdd_product_records'").fetchone()['n'] == 0


def test_duplicate_source_rows_group_across_files_but_keep_distinct_prices_and_specs(index):
    base = {'folder_id':'shared','drive_id':'synthetic12345','size_bytes':100,'version':'1'}
    entries = [{**base,'entry_key':'one','source_name':'one.csv'},
               {**base,'entry_key':'two','source_name':'two.csv'}]
    index.discover(entries)
    row = {'kind':'product','company':'Alpha Medical','product':'Pump','price':'100',
           'values':{'Product':'Pump','Model':'Steel'}}
    index.save_records(entries[0],[{**row,'row':1,'source_name':'one.csv'},
                                  {**row,'price':'120','row':2},
                                  {**row,'values':{'Product':'Pump','Model':'Plastic'},'row':3}])
    index.save_records(entries[1],[{**row,'row':8,'source_name':'two.csv'},
                                  {**row,'company':'Beta Medical','row':9}])
    result = index.search('shared',kind='product')
    assert result['total'] == 4 and sum(row['copies'] for row in result['items']) == 5
    assert max(row['copies'] for row in result['items']) == 2
    assert index.record_totals('shared') == {'raw_records':5,'unique_records':4}
    assert index.search('shared',kind='product',offset=4)['items'] == []


def test_unknown_vendors_and_individual_ocr_pages_are_not_merged(index):
    base = {'folder_id':'shared','drive_id':'synthetic12345','size_bytes':100,'version':'1'}
    entries = [{**base,'entry_key':'one','source_name':'one.pdf'},
               {**base,'entry_key':'two','source_name':'two.pdf'}]
    index.discover(entries)
    row = {'kind':'product','company':'','product':'Pump','price':'100'}
    index.save_records(entries[0],[row,{'kind':'needs_ocr','page':1},{'kind':'needs_ocr','page':2}])
    index.save_records(entries[1],[row])
    assert index.search('shared')['total'] == 4


def test_verified_content_hash_refreshes_cached_identity_without_deleting_rows(index):
    base = {'folder_id':'shared','drive_id':'synthetic12345','size_bytes':100,'version':'1'}
    entries = [{**base,'entry_key':'one','source_name':'one.csv'},
               {**base,'entry_key':'two','source_name':'two.csv'}]
    index.discover(entries)
    for entry in entries:
        index.save_records(entry,[{'kind':'product','company':'','product':'Pump','price':'100'}])
    assert index.record_totals('shared')['unique_records'] == 2
    for entry in entries:
        index.mark(entry['entry_key'],'indexed',content_hash='verified-same-content',records=1)
    assert index.record_totals('shared') == {'raw_records':2,'unique_records':1}
    assert index.search('shared')['items'][0]['copies'] == 2


def test_zero_characters_do_not_equate_different_source_text(index):
    entry = {'folder_id':'shared','drive_id':'synthetic12345','size_bytes':100,
             'version':'1','entry_key':'one','source_name':'one.txt'}
    index.discover([entry])
    texts = ['A\x00B\ufffd','A\ufffdB\x00','A\ufffdB\ufffd']
    index.save_records(entry,[{'kind':'text','text':text} for text in texts])
    assert index.search('shared')['total'] == 3


def test_top_level_inventory_is_saved_before_slow_archive_requests(index,monkeypatch):
    files = [{'id':'archive12345','name':'one.zip','path':'one.zip'},
             {'id':'archive67890','name':'two.zip','path':'two.zip'}]
    monkeypatch.setattr(indexer,'list_public_files',lambda folder:files)
    indexer.CANCEL.set()
    try:
        indexer.discover(index,'folder123456')
        assert index.search('folder123456',entries=True)['total'] == 2
    finally:
        indexer.CANCEL.clear()


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


def test_pdf_text_and_image_only_pages_use_installed_parser(tmp_path):
    from reportlab.pdfgen.canvas import Canvas

    path = tmp_path / 'catalogue.pdf'
    canvas = Canvas(str(path))
    canvas.drawString(50, 700, 'Synthetic Medical Surgical Pump')
    canvas.showPage()
    canvas.showPage()
    canvas.save()
    rows = list(indexer.extracted_records(path, 'Synthetic Medical'))
    assert rows[0]['kind'] == 'document'
    assert 'Surgical Pump' in rows[0]['text']
    assert rows[0]['page'] == 1
    assert rows[1]['kind'] == 'needs_ocr' and rows[1]['page'] == 2


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


def test_cloud_startup_does_not_reparse_stored_documents_or_publish_pending_rows(monkeypatch):
    from vendor_dashboard.storage import Store

    class Query:
        def execute(self,*args): return self
        def fetchall(self): return []
        def fetchone(self): return {'n':0}

    @contextmanager
    def connection(self):
        yield Query()

    monkeypatch.setattr(Store,'connection',connection)
    monkeypatch.setattr(Store,'_ensure_canonical_ids',lambda self,db:None)
    monkeypatch.setattr(Store,'_ensure_product_columns',lambda self,db:None)
    monkeypatch.setattr(Store,'apply_catalogue_policy',lambda self:None)
    def unexpected(self):
        raise AssertionError('Cloud startup must not run document extraction')
    monkeypatch.setattr(Store,'backfill_product_records',unexpected)
    store = object.__new__(Store)
    store.cloud = True
    store.initialize()


def test_storage_pause_is_reported_and_resume_does_not_restart_a_full_database(index, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main
    from app.drive import index_api

    monkeypatch.setattr(index_api, 'library', lambda: index)
    monkeypatch.setattr(index_api, 'get_settings', lambda: SimpleNamespace(dashboard_token='synthetic-drive-token', drive_folder_id='folder123456', drive_folder_url=''))
    capacity = {'used_bytes': 852 * 1024**2, 'index_budget_bytes': 850 * 1024**2,
                'limit_reached': True, 'remaining_index_bytes': 0}
    monkeypatch.setattr(indexer, 'storage_status', lambda _: capacity)
    index.state('folder123456', 'paused', 'Storage limit')
    client = TestClient(main.app)
    headers = {'X-Token': 'synthetic-drive-token'}
    assert client.get('/api/drive-index/status', headers=headers).json()['storage'] == capacity
    with patch.object(indexer, 'start_index') as start:
        response = client.post('/api/drive-index/start', headers=headers)
        assert response.status_code == 507
        assert 'Saved rows are kept' in response.json()['detail']
        start.assert_not_called()
    assert index.status('folder123456')['status'] == 'paused'


def test_storage_status_reports_actual_size_and_boundary():
    class Query:
        def execute(self, sql):
            assert sql == 'SELECT pg_database_size(current_database()) AS n'
            return self

        def fetchone(self):
            return {'n': indexer.INDEX_STORAGE_BUDGET}

    @contextmanager
    def connection():
        yield Query()

    assert indexer.storage_status(SimpleNamespace(cloud=False)) is None
    result = indexer.storage_status(SimpleNamespace(cloud=True, connection=connection))
    assert result['limit_reached'] is True and result['remaining_index_bytes'] == 0
