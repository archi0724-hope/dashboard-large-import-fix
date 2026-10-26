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
