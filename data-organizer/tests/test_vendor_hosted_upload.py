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
    assert page.file_uploader(key='document_uploads').allowed_type == ['.zip']
    assert not any('ZIP file path' in item.label for item in page.text_input)
    assert page.button(key='save_documents').disabled


def test_hosted_direct_zip_saves_documents_and_skips_duplicate(tmp_path, monkeypatch):
    archive = BytesIO()
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr('Alpha Medical/GST.txt', b'Synthetic GST document')

    class Upload:
        name = 'synthetic-vendors.zip'
        def getvalue(self): return archive.getvalue()

    page = dashboard(tmp_path, monkeypatch)
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
