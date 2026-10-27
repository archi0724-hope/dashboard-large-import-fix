import sqlite3
import threading
import time
from contextlib import contextmanager

import pytest

from tests.test_vendor_manual_catalogue_merge import catalogue_page, manual_upload


def test_partial_workbook_retry_inserts_missing_rows_and_preserves_existing_review(tmp_path):
    from storage import Store

    store = Store(tmp_path)
    filename, content, _ = manual_upload([
        ['Alpha Medical', 'Wheelchair', 'W1', 100, 'INR', ''],
        ['Alpha Medical', 'Surgical forceps', 'F1', 200, 'INR', ''],
    ])
    store.import_catalogue_workbook(filename, content)
    with store.connection() as db:
        db.execute("DELETE FROM vdd_product_records WHERE sku='F1'")
        db.execute("UPDATE vdd_product_records SET catalogue_status='review',classification_reason='Reviewer decision' WHERE sku='W1'")
    result = store.import_catalogue_workbook('renamed.xlsx', content)
    assert result == {'added':1,'review':0,'excluded':0,'duplicates':1}
    assert store.product_review_records().iloc[0].classification_reason == 'Reviewer decision'
    assert store.product_records().iloc[0].sku == 'F1'


def test_cloud_manual_original_is_stored_once_and_survives_backup_and_company_merge(tmp_path):
    from storage import Query, Store

    store = Store(tmp_path/'cloud-simulation')
    @contextmanager
    def connection():
        with sqlite3.connect(store.db_path) as db:
            db.row_factory = sqlite3.Row
            yield Query(db, False)
    store.cloud = True
    store.connection = connection
    filename, content, _ = manual_upload([
        ['Alpha Medical', 'Wheelchair', 'W1', 100, 'INR', ''],
        ['Beta Medical', 'Surgical forceps', 'F1', 200, 'INR', ''],
    ])
    store.import_catalogue_workbook(filename, content)
    docs = store.documents()
    assert len(docs) == 2 and docs.available.all()
    with connection() as db:
        counts = db.execute('SELECT count(payload) AS n,sum(length(payload)) AS size FROM vdd_documents').fetchone()
        assert counts['n'] == 1 and counts['size'] == len(content)
    assert all(store.read_bytes(int(doc.id)) == content for doc in docs.itertuples())
    restored = Store(tmp_path/'restored')
    restored.restore_backup(store.backup_bytes())
    assert restored.product_record_count() == 2
    store.merge_named_companies('Beta Medical', ['Alpha Medical'])
    remaining = store.documents()
    assert len(remaining) == 1 and remaining.available.all()
    assert store.read_bytes(int(remaining.iloc[0].id)) == content


def test_failed_late_batch_rolls_back_the_whole_workbook(tmp_path, monkeypatch):
    import exports
    from storage import Store

    store = Store(tmp_path)
    def broken(*args, **kwargs):
        for number in range(201):
            yield 'approved', {'company_name':'Alpha Medical','product_name':'Wheelchair','price':100,
                               'source_sheet':'Products','source_page':str(number+2),'record_type':'catalogue'}
        raise ValueError('Synthetic failure after the first batch')
    monkeypatch.setattr(exports,'iter_manual_catalogue_rows',broken)
    with pytest.raises(ValueError,match='after the first batch'):
        store.import_catalogue_workbook('source.xlsx',b'synthetic')
    assert store.product_record_count(status='all') == len(store.documents()) == 0


def test_local_readers_see_saved_rows_while_import_has_uncommitted_batches(tmp_path, monkeypatch):
    import exports
    from storage import Store

    store = Store(tmp_path)
    with store.connection() as db:
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    reached, release = threading.Event(), threading.Event()
    def rows(*args, **kwargs):
        for number in range(401):
            yield 'approved', {'company_name':'Alpha Medical','product_name':'Surgical forceps '+('x'*8000),
                'sku':str(number),'price':100,'record_type':'catalogue','source_page':str(number+2),'source_sheet':'Products'}
    def progress(state):
        if state['processed'] == 400:
            reached.set()
            assert release.wait(10)
    monkeypatch.setattr(exports,'iter_manual_catalogue_rows',rows)
    errors = []
    def work():
        try: store.import_catalogue_workbook('source.xlsx', b'synthetic',progress=progress)
        except (ValueError, RuntimeError, sqlite3.Error, AssertionError) as error: errors.append(error)
    worker = threading.Thread(target=work)
    worker.start()
    try:
        assert reached.wait(5)
        assert store.product_record_count(status='all') == 0
    finally:
        release.set()
        worker.join(10)
    assert not errors and not worker.is_alive()
    assert store.product_record_count(status='all') == 401


def test_background_job_survives_page_reruns_without_false_interrupted_warning(tmp_path, monkeypatch):
    import catalogue_jobs
    import storage

    original = storage.Store.import_catalogue_workbook
    release = threading.Event()
    def slow(self, *args, progress=None, **kwargs):
        progress({'processed':200,'phase':'Processing rows'})
        assert release.wait(10)
        return original(self,*args,progress=progress,**kwargs)
    page = catalogue_page(tmp_path,monkeypatch)
    monkeypatch.setattr(storage.Store,'import_catalogue_workbook',slow)
    try:
        page.file_uploader[0].set_value(manual_upload()).run()
        key = page.session_state['manual_catalogue_job']
        assert catalogue_jobs.status(key)['status'] == 'running'
        page.run()
        assert any('Import running: 200' in info.value for info in page.info)
        assert not page.warning
        release.set()
        deadline = time.monotonic()+5
        while catalogue_jobs.status(key)['status'] == 'running' and time.monotonic()<deadline:
            time.sleep(.02)
        page.run()
        assert not page.exception and not page.error
        assert page.session_state['manual_catalogue_import_result']['added'] == 1
    finally:
        release.set()
