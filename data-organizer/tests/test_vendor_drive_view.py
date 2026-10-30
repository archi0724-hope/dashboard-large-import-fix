import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.drive.index_store import DriveIndex


@pytest.fixture
def shared(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'vendor_dashboard'))
    from drive_view import inventory, source_rows
    from storage import Store

    store = Store(tmp_path)
    index = DriveIndex(store)
    index.select_source({'scope': 'shared', 'source': {'kind': 'folder', 'id': 'syntheticfolder'}})
    entries = [
        {'entry_key': 'archive', 'folder_id': 'shared', 'drive_id': 'syntheticzip', 'source_name': 'vendors.zip', 'member_path': '', 'size_bytes': 100, 'version': 'v1', 'status': 'indexed'},
        {'entry_key': 'alpha', 'folder_id': 'shared', 'drive_id': 'syntheticzip', 'source_name': 'vendors.zip', 'member_path': 'organized_by_company/Alpha Medical/GST.txt', 'size_bytes': 50, 'version': 'v1', 'status': 'indexed'},
        {'entry_key': 'beta', 'folder_id': 'shared', 'drive_id': 'syntheticzip', 'source_name': 'vendors.zip', 'member_path': 'organized_by_company/Beta Medical/Price.xlsx', 'size_bytes': 50, 'version': 'v1', 'status': 'blocked', 'error': 'Oversized file'},
    ]
    index.discover(entries)
    index.save_records(entries[1], [{'company': '', 'kind': 'product', 'product': 'Synthetic Pump', 'values': {'price': '10'},
        'source_name': 'vendors.zip', 'member_path': entries[1]['member_path'], 'source_url': 'https://drive.google.com/file/d/syntheticzip/view', 'review_status': 'Needs review'}])
    # Simulate committed rows in a batch whose final record_count update has not run.
    inventory.clear()
    source_rows.clear()
    yield store, inventory, source_rows
    inventory.clear()
    source_rows.clear()


def test_drive_inventory_is_visible_without_copying_files_or_approving_rows(shared):
    store, inventory, source_rows = shared
    result = inventory('fixture', store)
    assert result['source_rows'] == 1
    assert len(result['items']) == 2  # Archive container is not another document.
    assert {item['Detected company'] for item in result['items']} == {'Alpha Medical', 'Beta Medical'}
    alpha = next(item for item in result['items'] if item['Detected company'] == 'Alpha Medical')
    assert alpha['Document types'] == 'GST'
    beta = next(item for item in result['items'] if item['Detected company'] == 'Beta Medical')
    assert beta['Status'] == 'blocked' and beta['Issue'] == 'Oversized file'
    rows = source_rows('fixture', 'synthetic pump', 0, store)
    assert rows['total'] == 1 and rows['items'][0]['record']['review_status'] == 'Needs review'
    assert source_rows('fixture', 'not present', 0, store)['total'] == 0
    assert store.documents().empty and store.vendors().empty
    with store.connection() as db:
        assert db.execute('SELECT count(*) AS n FROM vdd_product_records').fetchone()['n'] == 0


def test_main_vendor_fragment_renders_shared_rows_and_search(shared):
    store, _, _ = shared
    app = AppTest.from_string(
        'from storage import Store\nfrom drive_view import render_drive_records\n'
        f'render_drive_records(Store({json.dumps(str(store.data_dir))}))\n'
    ).run(timeout=20)
    assert not app.exception
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics['Drive companies detected'] == '2'
    assert metrics['Drive source rows'] == '1'
    assert 'Synthetic Pump' in app.dataframe[1].value['Product / type'].tolist()
    assert 'Alpha Medical' in app.dataframe[1].value['Company'].tolist()
    app.text_input(key='drive_vendor_files_query').set_value('Beta').run()
    assert not app.exception
    assert app.dataframe[0].value['Detected company'].tolist() == ['Beta Medical']


def test_combined_checklist_groups_both_sources_and_deduplicates_known_content(shared):
    import hashlib
    import zipfile
    from io import BytesIO

    import pandas as pd
    from drive_view import combine_company_documents
    from import_service import import_documents
    from vendor_core import build_checklist, dashboard_counts

    store, inventory, _ = shared
    payload = b'Synthetic GST document'
    data = BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('Alpha Medical/GST.txt', payload)
    data.name = 'vendors.zip'
    import_documents(store, [data], '', False)
    references = inventory('combined-fixture', store)['references']
    alpha = next(row for row in references if row['entry_key'] == 'alpha')
    alpha['content_hash'] = hashlib.sha256(payload).hexdigest()
    beta = next(row for row in references if row['entry_key'] == 'beta')
    beta.update(status='indexed', error='')
    vendors, docs = combine_company_documents(store.vendors(), store.documents(), references)
    assert len(vendors) == 2 and len(docs) == 2
    counts = dashboard_counts(vendors, docs)
    assert counts['companies'] == 2 and counts['stored_files'] == 2
    checklist = build_checklist(vendors, docs).set_index('Company Name')
    assert checklist.loc['Alpha Medical', 'GST'] == 'Yes'
    assert checklist.loc['Beta Medical', 'Price'] == 'Yes'
    linked = docs.loc[docs.drive_url.ne('')].iloc[0]
    assert linked.company_name == 'Beta Medical' and str(linked.id).startswith('drive:')
    assert len(store.vendors()) == 1 and len(store.documents()) == 1  # Read view only.
    aliases = pd.DataFrame([{'alias_key': 'beta medical', 'canonical_key': 'alpha medical', 'status': 'Confirmed'}])
    merged_vendors, merged_docs = combine_company_documents(store.vendors(), store.documents(), references, aliases)
    assert len(merged_vendors) == 1 and set(merged_docs.company_name) == {'Alpha Medical'}


def test_company_export_retains_drive_links_without_fetching_other_companies(shared):
    import zipfile
    from io import BytesIO
    from unittest.mock import patch

    from drive_view import combine_company_documents
    from openpyxl import load_workbook

    store, inventory, _ = shared
    _vendors, docs = combine_company_documents(store.vendors(), store.documents(), inventory('export-fixture', store)['references'])
    with patch.object(store, 'read_bytes', side_effect=AssertionError('Drive originals must not be downloaded')):
        exported = store.company_zip('Alpha Medical', docs)
    with zipfile.ZipFile(BytesIO(exported)) as archive:
        names = archive.namelist()
        assert all('Beta' not in name for name in names)
        manifest = archive.read(next(name for name in names if name.endswith('Drive originals.csv'))).decode('utf-8-sig')
        assert 'Alpha Medical/GST.txt' in manifest and 'https://drive.google.com/' in manifest
        assert 'Beta Medical' not in manifest
        workbook = load_workbook(BytesIO(archive.read(next(name for name in names if name.endswith('.xlsx')))))
        register = list(workbook['Document Register'].values)
        assert 'Drive original' in register[3]
        assert any('https://drive.google.com/' in str(cell) for row in register[4:] for cell in row)


def test_actual_vendor_dashboard_has_one_company_checklist_for_drive_files(shared, monkeypatch):
    store, _, _ = shared
    monkeypatch.setenv('VENDOR_DATA_DIR', str(store.data_dir))
    monkeypatch.setenv('DATABASE_URL', '')
    monkeypatch.setenv('APP_PASSWORD', '')
    monkeypatch.setenv('CLOUD_DEPLOYMENT', 'false')
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'vendor_dashboard' / 'app.py')).run(timeout=40)
    assert not app.exception
    assert 'Find a company' in [heading.value for heading in app.subheader]
    assert 'Google Drive companies & documents' not in [heading.value for heading in app.subheader]
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics['Vendor headcount'] == '2'
    assert metrics['GST'] == '1'
    app.selectbox(key='company_selection').set_value('beta medical').run(timeout=40)
    assert not app.exception
    assert any('Price' in expander.label for expander in app.expander)


def test_migrated_original_keeps_its_company_checklist_and_source_link(shared):
    import zipfile
    from io import BytesIO

    from drive_view import combine_company_documents
    from import_service import import_documents
    from vendor_core import build_checklist

    store, _, _ = shared
    data = BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('Alpha Medical/GST.txt', b'Synthetic verified original')
    data.name = 'source.zip'
    import_documents(store, [data], '', False)
    before = store.documents().iloc[0]
    source = 'https://drive.google.com/file/d/syntheticBackup123/view#member=documents%2F1%2FGST.txt'
    with store.connection() as db:
        db.execute('UPDATE vdd_documents SET stored_path=?,payload=NULL WHERE id=?', (source, int(before.id)))
    docs = store.documents()
    assert len(docs) == 1 and docs.iloc[0].available
    assert docs.iloc[0].file_hash == before.file_hash and docs.iloc[0].company_key == before.company_key
    assert docs.iloc[0].drive_member == 'documents/1/GST.txt'
    assert store.read_bytes(int(before.id)) is None
    vendors, combined = combine_company_documents(store.vendors(), docs, [])
    assert combined.iloc[0].drive_url == 'https://drive.google.com/file/d/syntheticBackup123/view'
    assert build_checklist(vendors, combined).iloc[0]['GST'] == 'Yes'
    with zipfile.ZipFile(BytesIO(store.company_zip('Alpha Medical', combined))) as archive:
        manifest = archive.read(next(name for name in archive.namelist() if name.endswith('Drive originals.csv'))).decode('utf-8-sig')
    assert 'documents/1/GST.txt' in manifest and 'syntheticBackup123' in manifest


@pytest.mark.parametrize('source', [
    'http://drive.google.com/file/d/backupFile123/view#member=docs/a.pdf',
    'https://drive.google.com.evil.example/file/d/backupFile123/view#member=docs/a.pdf',
    'https://drive.google.com/file/d/backupFile123/view#member=../a.pdf',
    'https://drive.google.com/file/d/backupFile123/view',
    'https://[invalid',
])
def test_original_reference_rejects_unsafe_or_incomplete_links(shared, source):
    from storage import drive_original_reference

    assert drive_original_reference(source) == ('', '')


def test_startup_batches_missing_master_aliases_and_keeps_reviewed_assignments():
    from storage import Store
    class Result:
        def __init__(self, rows): self.rows = rows
        def fetchall(self): return self.rows
    class Database:
        def __init__(self): self.batches = []
        def execute(self, sql):
            if sql.startswith('SELECT alias_key'):
                return Result([{'alias_key':'alpha medical'}])
            return Result([{'company_key':'alpha medical','company_name':'Alpha Medical'},
                           {'company_key':'beta medical','company_name':'Beta Medical'}])
        def executemany(self, sql, params): self.batches.append((sql, params))
    db = Database()
    Store._ensure_canonical_aliases(None, db)
    assert len(db.batches) == 1
    sql, rows = db.batches[0]
    assert 'DO NOTHING' in sql and len(rows) == 1
    assert rows[0][:3] == ('beta medical','beta medical','Beta Medical')


def test_catalogue_navigation_does_not_load_full_drive_inventory(shared, monkeypatch):
    import drive_view
    store, _, _ = shared
    monkeypatch.setenv('VENDOR_DATA_DIR',str(store.data_dir))
    monkeypatch.setenv('DATABASE_URL','')
    monkeypatch.setenv('APP_PASSWORD','')
    monkeypatch.setenv('CLOUD_DEPLOYMENT','false')
    def unexpected(*args):
        raise AssertionError('Catalogue navigation must not load the full Drive inventory')
    monkeypatch.setattr(drive_view,'inventory',unexpected)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1]/'vendor_dashboard/app.py'))
    app.session_state['page'] = 'Catalogue & prices'
    app.run(timeout=30)
    assert not app.exception and not app.warning
    assert 'Drive catalogue and price rows' in [heading.value for heading in app.subheader]
    assert len(app.get('iframe')) == 1
    assert app.file_uploader(key='catalogue_manual_workbook').allowed_type == ['.xlsx', '.csv']


def test_duplicate_drive_documents_merge_types_and_pending_verified_zip_copies(shared):
    from drive_view import combine_company_documents
    store, inventory, _ = shared
    alpha = dict(next(row for row in inventory('dedupe',store)['references'] if row['entry_key']=='alpha'))
    alpha.update(content_hash='same-sha256',version='123:50:20')
    copies = [alpha,{**alpha,'entry_key':'pan-copy','member_path':'Alpha Medical/PAN Card.txt'},
              {**alpha,'entry_key':'pending-copy','drive_id':'otherArchive123','content_hash':'','status':'pending'}]
    vendors, docs = combine_company_documents(store.vendors(),store.documents(),copies)
    assert len(vendors) == 1 and len(docs) == 1
    assert set(docs.iloc[0].types) == {'GST','PAN Card'}
    assert docs.iloc[0].available and docs.iloc[0].needs_review
    assert store.documents().empty


def test_confirmed_company_alias_is_not_an_extra_headcount(shared):
    import pandas as pd
    from drive_view import combine_company_documents
    store, inventory, _ = shared
    store.upsert_vendors(pd.DataFrame({'company_name':['Alpha Medical','Alpha Alias']}))
    aliases = pd.DataFrame([{'alias_key':'alpha alias','canonical_key':'alpha medical','status':'Confirmed'},
                            {'alias_key':'alpha medical','canonical_key':'alpha medical','status':'Confirmed'}])
    entry = dict(next(row for row in inventory('alias-dedupe',store)['references'] if row['entry_key']=='alpha'))
    entry['member_path'] = 'Alpha Alias/GST.txt'
    vendors, docs = combine_company_documents(store.vendors(),store.documents(),[entry],aliases)
    assert vendors.company_name.tolist() == ['Alpha Medical']
    assert docs.company_name.tolist() == ['Alpha Medical']
    assert len(store.vendors()) == 2  # Original registry and provenance remain stored.
