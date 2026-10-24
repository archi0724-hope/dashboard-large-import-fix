from io import BytesIO
from pathlib import Path
from unittest.mock import patch
import zipfile

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
    assert page.radio(key='import_source').value == 'Upload ZIP directly (large files)'
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
        assert page.radio(key='import_source').value == 'Upload ZIP directly (large files)'
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


def test_computer_path_in_drive_field_returns_to_zip_picker(tmp_path, monkeypatch):
    page = dashboard(tmp_path, monkeypatch).run(timeout=30)
    page.radio(key='import_source').set_value('Google Drive ZIP (large files)').run(timeout=30)
    page.text_input(key='drive_zip_link').set_value(r'"C:\Users\Archi\Downloads\organized_by_company.zip"').run(timeout=30)
    assert not page.exception
    assert page.radio(key='import_source').value == 'Upload ZIP directly (large files)'
    assert not page.error
    assert any('organized_by_company.zip' in item.value for item in page.info)
    assert any(item.proto.label == 'Choose ZIP files' for item in page.get('link_button'))


def test_drive_folder_is_explained_before_import(tmp_path, monkeypatch):
    page = dashboard(tmp_path, monkeypatch).run(timeout=30)
    page.radio(key='import_source').set_value('Google Drive ZIP (large files)').run(timeout=30)
    page.text_input(key='drive_zip_link').set_value('https://drive.google.com/drive/folders/abcdefghijk').run(timeout=30)
    assert not page.exception
    assert page.button(key='save_documents').disabled
    assert any('Drive folder' in item.value for item in page.info)


def test_connected_drive_zip_picker_uses_actual_file_link(tmp_path, monkeypatch):
    monkeypatch.setenv('GOOGLE_DRIVE_FOLDER_URL', 'https://drive.google.com/drive/folders/connected123')
    files = [{'id': 'archive12345', 'name': 'company.zip', 'path': 'company.zip',
              'url': 'https://drive.google.com/file/d/archive12345/view'}]
    with patch('app.drive.public_drive.list_public_files', return_value=files):
        page = dashboard(tmp_path, monkeypatch).run(timeout=30)
        page.radio(key='import_source').set_value('Google Drive ZIP (large files)').run(timeout=30)
        assert not page.exception
        page.selectbox(key='connected_drive_zip').select('company.zip').run(timeout=30)
        assert not page.exception
        assert page.text_input(key='drive_zip_link').value == files[0]['url']
        assert not page.button(key='save_documents').disabled
