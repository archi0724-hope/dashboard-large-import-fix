import zipfile
from contextlib import contextmanager
from dataclasses import replace
from io import BytesIO
from unittest.mock import patch

import pytest

from app.config import ConfigError, RuntimeConfig
from app.drive import public_drive as public
from app.drive.google_drive import drive_status
from app.pipeline import make_source


class Raw(BytesIO):
    def read(self, size=-1, decode_content=False):
        return super().read(size)


class Response:
    def __init__(self, content, headers=None):
        self.raw = Raw(content)
        self.content = content
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, size):
        yield self.content


def test_public_listing_follows_folders_once_and_escapes_names():
    root = b'''<div id="flip-contents"><a href="https://drive.google.com/drive/folders/childfolder123">Sub</a>
    <a href="https://drive.google.com/file/d/document123/view"><div>A &amp; B.xlsx</div></a>
    <a href="http://evil.example/file/d/evilfile123/view">bad</a></div>'''
    child = b'''<div id="flip-contents"><a href="https://drive.google.com/drive/folders/rootfolder123">Loop</a>
    <a href="https://drive.google.com/file/d/archive12345/view">docs.zip</a></div>'''
    with patch.object(public, 'open_response', side_effect=[Response(root), Response(child)]) as opening:
        files = public.list_public_files('rootfolder123')
    assert [item['path'] for item in files] == ['A & B.xlsx', 'Sub/docs.zip']
    assert opening.call_count == 2


@pytest.mark.parametrize('content', [b'<title>Sign in</title>', b'not a folder'])
def test_private_or_unrecognized_listing_is_not_treated_as_empty(content):
    with patch.object(public, 'open_response', return_value=Response(content)), pytest.raises(ConfigError):
        public.list_public_files('rootfolder123')


def test_oversized_download_does_not_replace_cached_original(tmp_path):
    destination = tmp_path / 'file.xlsx'
    destination.write_bytes(b'original')
    with patch.object(public, 'open_response', return_value=Response(b'oversized')), pytest.raises(ConfigError):
        public.download_public_file('document123', destination, max_bytes=4)
    assert destination.read_bytes() == b'original'
    assert not destination.with_name('file.xlsx.part').exists()


def test_confirmation_form_cannot_redirect_to_an_untrusted_host(tmp_path):
    html = b'<form id="download-form" action="https://evil.example/steal"></form>'
    with patch.object(public, 'open_response', return_value=Response(html, {'Content-Type': 'text/html'})), pytest.raises(ValueError):
        public.download_public_file('document123', tmp_path / 'file.xlsx')


def test_refresh_detects_content_changes_and_does_not_download_zip(settings, tmp_path):
    source = public.PublicDriveSource(replace(settings, data_dir=tmp_path), 'rootfolder123')
    entries = [{'id': 'document123', 'name': 'a.xlsx', 'path': 'a.xlsx'},
               {'id': 'archive12345', 'name': 'archive.zip', 'path': 'archive.zip'}]
    with patch.object(public, 'list_public_files', return_value=entries), patch.object(public, 'open_response', side_effect=[Response(b'first'), Response(b'edited')]):
        first = list(source.scan())
        second = list(source.scan())
    assert first[0].modified != second[0].modified
    assert source.ensure_local(second[0]).local_path.read_bytes() == b'edited'
    assert not first[1].supported and not first[1].local_path.exists()


def test_missing_service_account_uses_public_mode(settings, tmp_path):
    settings = replace(settings, google_service_account_file=tmp_path / 'missing.json', google_token_file=tmp_path / 'missing-token.json')
    cfg = RuntimeConfig(source_kind='drive', drive_folder='rootfolder123')
    assert isinstance(make_source(settings, cfg), public.PublicDriveSource)
    status = drive_status(settings, cfg)
    assert not status['service_account'] and status['access_mode'] == 'public'


def test_drive_zip_import_requires_auth_and_supplies_organizer_source(settings, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main
    from app.pipeline import Pipeline
    from app.services import AppState
    from vendor_dashboard.drive_import import DiskUpload

    settings = replace(settings, data_dir=tmp_path / 'data', db_path=tmp_path / 'org.duckdb',
                       dashboard_token='synthetic-drive-token')
    settings.ensure_dirs()
    state = AppState(settings)
    state.update_config({'source_kind': 'drive', 'drive_folder': 'rootfolder123'})
    archive = BytesIO()
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('Companies/Alpha Medical/GST.txt', 'Alpha Medical GST document')

    @contextmanager
    def download(*args, **kwargs):
        archive.seek(0)
        yield DiskUpload(archive)

    monkeypatch.setattr(main, 'st', lambda: state)
    monkeypatch.setattr(public, 'list_public_files', lambda folder: [
        {'id': 'archive12345', 'name': 'Companies.zip', 'url': 'https://drive.google.com/file/d/archive12345/view'}])
    monkeypatch.setattr('vendor_dashboard.drive_import.download_drive_zip', download)
    client = TestClient(main.app)
    try:
        assert client.post('/api/drive/import-zip', json={'file_id': 'archive12345'}).status_code == 401
        headers = {'X-Token': 'synthetic-drive-token'}
        assert client.post('/api/drive/import-zip', headers=headers, json={'file_id': 'not-in-folder'}).status_code == 400
        response = client.post('/api/drive/import-zip', headers=headers, json={'file_id': 'archive12345'})
        assert response.status_code == 200 and response.json()['skipped_entries'] == 0
        files = Pipeline(settings, state.cfg, state.repo).discover()
        assert len(files) == 1 and files[0].name == 'GST.txt'
        assert files[0].local_path.read_text() == 'Alpha Medical GST document'
        result = state.organize_source_files()
        assert (tmp_path / 'data' / 'organized_by_company' / 'Alpha Medical' / 'GST.txt').is_file()
        assert result['files_copied'] == 1
    finally:
        state.repo.close()
