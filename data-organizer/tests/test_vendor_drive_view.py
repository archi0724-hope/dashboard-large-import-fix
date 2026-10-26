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
