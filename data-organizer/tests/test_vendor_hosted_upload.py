import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


def dashboard(tmp_path, monkeypatch, hosted=True):
    vendor_dir = Path(__file__).resolve().parents[1] / 'vendor_dashboard'
    monkeypatch.syspath_prepend(str(vendor_dir))
    monkeypatch.setenv('VENDOR_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('DATABASE_URL', '')
    monkeypatch.setenv('APP_PASSWORD', 'synthetic-ui-password')
    monkeypatch.setenv('CLOUD_DEPLOYMENT', 'true' if hosted else '')
    page = AppTest.from_file(str(vendor_dir / 'app.py'))
    page.session_state['authenticated'] = True
    page.session_state['page'] = 'Upload documents'
    return page


def test_hosted_upload_defaults_to_file_picker_and_recovers_stale_path_choice(tmp_path, monkeypatch):
    page = dashboard(tmp_path, monkeypatch)
    page.session_state['import_source'] = 'Local ZIP path (large files)'
    page.run(timeout=30)

    assert not page.exception
    assert page.radio(key='import_source').value == 'Upload ZIP from computer'
    assert 'Local ZIP path (large files)' not in page.radio(key='import_source').options
    assert not any(item.key == 'document_uploads' for item in page.file_uploader)
    assert any(item.proto.label == 'Choose ZIP files' for item in page.get('link_button'))
    assert not any('ZIP file path' in item.label for item in page.text_input)
    assert not any(item.key == 'save_documents' for item in page.button)


def test_local_direct_zip_saves_documents_and_skips_duplicate(tmp_path, monkeypatch):
    archive = BytesIO()
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr('Alpha Medical/GST.txt', b'Synthetic GST document')

    class Upload:
        name = 'synthetic-vendors.zip'
        def getvalue(self): return archive.getvalue()

    page = dashboard(tmp_path, monkeypatch, hosted=False)
    with patch('streamlit.file_uploader', return_value=[Upload()]):
        page.run(timeout=30)
        assert not page.exception
        assert page.radio(key='import_source').value == 'Upload ZIP from computer'
        page.button(key='save_documents').click().run(timeout=30)
        assert not page.exception and not page.error
        assert page.session_state['last_upload']['saved_files'] == 1
        page.button(key='save_documents').click().run(timeout=30)
        assert not page.exception and not page.error
        assert page.session_state['last_upload']['duplicate_files'] == 1
    from storage import Store
    assert len(Store(tmp_path).documents()) == 1


def test_local_app_retains_disk_zip_option(tmp_path, monkeypatch):
    page = dashboard(tmp_path, monkeypatch, hosted=False).run(timeout=30)
    page.radio(key='import_source').set_value('Local ZIP path (large files)').run(timeout=30)
    assert not page.exception
    assert any('ZIP file path' in item.label for item in page.text_input)


def test_old_drive_download_choice_migrates_to_library_without_downloading(tmp_path, monkeypatch):
    monkeypatch.setenv('GOOGLE_DRIVE_FOLDER_URL', 'https://drive.google.com/drive/folders/connected123')
    page = dashboard(tmp_path, monkeypatch)
    page.session_state['import_source'] = 'Google Drive ZIP (large files)'
    page.session_state['drive_zip_link'] = 'https://drive.google.com/file/d/archive12345/view'
    with patch('app.drive.public_drive.list_public_files') as listing, patch('drive_import.download_drive_zip') as download:
        page.run(timeout=30)
    listing.assert_not_called()
    download.assert_not_called()
    assert not page.exception
    assert page.radio(key='import_source').value == 'Google Drive library (10+ GB ZIPs)'
    assert not page.error
    assert not any(item.key == 'drive_zip_link' for item in page.text_input)
    assert not any(item.key == 'save_documents' for item in page.button)
    frames = page.get('iframe')
    assert len(frames) == 1
    assert frames[0].proto.src.startswith('/drive-library#')


def test_large_drive_link_entry_is_available_without_preconfigured_folder(tmp_path, monkeypatch):
    monkeypatch.setenv('GOOGLE_DRIVE_FOLDER_URL','')
    monkeypatch.setenv('GOOGLE_DRIVE_FOLDER_ID','')
    page = dashboard(tmp_path, monkeypatch).run(timeout=30)
    page.radio(key='import_source').set_value('Google Drive library (10+ GB ZIPs)').run(timeout=30)
    assert not page.exception
    assert not any(item.key == 'save_documents' for item in page.button)
    assert len(page.get('iframe')) == 1
    assert any('Paste a Google Drive folder or ZIP file link' in item.value for item in page.info)


def test_connected_drive_large_zip_stays_in_drive_and_shows_shared_library(tmp_path, monkeypatch):
    monkeypatch.setenv('GOOGLE_DRIVE_FOLDER_URL', 'https://drive.google.com/drive/folders/connected123')
    page = dashboard(tmp_path, monkeypatch).run(timeout=30)
    assert not page.exception
    assert page.radio(key='import_source').value == 'Google Drive library (10+ GB ZIPs)'
    assert any('There is no total collection-size cap' in item.value for item in page.info)
    folder = next(item for item in page.get('link_button') if item.proto.label.startswith('1. Open connected Drive'))
    assert folder.proto.url == 'https://drive.google.com/drive/folders/connected123'
    assert len(page.get('iframe')) == 1


def test_purchase_invoice_card_and_sidebar_use_shared_invoice_records(tmp_path,monkeypatch):
    page = dashboard(tmp_path,monkeypatch).run(timeout=30)
    assert not page.exception
    assert 'Served invoices' in page.radio(key='page').options
    metric = next(item for item in page.metric if item.label=='Served data invoices')
    assert metric.value == '0'
    page.radio(key='page').set_value('Purchase Invoice').run(timeout=30)
    assert not page.exception
    assert [item.value for item in page.title] == ['Served data invoices']
    assert not page.get('iframe') and not page.metric
