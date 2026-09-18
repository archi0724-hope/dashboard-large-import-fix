from io import BytesIO
from pathlib import Path
from unittest.mock import patch
import zipfile
import pytest

from drive_import import download_drive_zip, drive_file_id, drive_resource_key, open_response
from import_service import import_documents
from storage import Store
from vendor_core import iter_uploads, discover_folder_companies, ArchiveLimits


def zip_bytes():
    output = BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('Batch/Alpha Medical/GST.txt', b'GST fixture')
    return output.getvalue()


class Raw(BytesIO):
    def read(self, size=-1, decode_content=False):
        return super().read(size)


class Response:
    def __init__(self, body, content_type='application/zip', status=200, headers=None):
        self.body = body
        self.raw = Raw(body)
        self.status_code = status
        self.headers = {'Content-Type': content_type, **(headers or {})}
        self.closed = False
    def close(self): self.closed = True
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError('HTTP failure')
    def iter_content(self, chunk_size):
        for i in range(0, len(self.body), chunk_size): yield self.body[i:i+chunk_size]
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


def test_drive_stream_import_and_cleanup(tmp_path):
    with patch('drive_import.requests.Session') as factory:
        factory.return_value.__enter__.return_value.get.return_value = Response(zip_bytes())
        with download_drive_zip('https://drive.google.com/file/d/abcdefghijk/view') as upload:
            path = Path(upload.file.name)
            assert path.exists()
            store = Store(tmp_path)
            result = import_documents(store, [upload], retain_archive=False)
            assert result.saved_files == 1 and not result.issues and result.notes
            assert import_documents(store, [upload], retain_archive=False).duplicate_files == 1
            assert store.upload_archives().empty
        assert not path.exists()


def test_virus_confirmation_form():
    html = b'<form id="download-form" action="https://drive.usercontent.google.com/download"><input name="id" value="abcdefghijk"><input name="uuid" value="token"></form>'
    with patch('drive_import.requests.Session') as factory:
        session = factory.return_value.__enter__.return_value
        session.get.side_effect = [Response(html, 'text/html'), Response(zip_bytes())]
        with download_drive_zip('https://drive.usercontent.google.com/open?id=abcdefghijk') as upload:
            assert zipfile.is_zipfile(upload)
        assert 'uuid=token' in session.get.call_args.args[0]


def test_public_share_resource_key_is_preserved():
    with patch('drive_import.requests.Session') as factory:
        factory.return_value.__enter__.return_value.get.return_value = Response(zip_bytes())
        link = 'https://drive.google.com/file/d/abcdefghijk/view?resourcekey=AbC_123'
        with download_drive_zip(link):
            pass
        assert drive_resource_key(link) == 'AbC_123'
        assert 'resourcekey=AbC_123' in factory.return_value.__enter__.return_value.get.call_args.args[0]


@pytest.mark.parametrize('response,limit', [
    (Response(b'not a zip'), 9999),
    (Response(b'<html>Sign in</html>', 'text/html'), 9999),
    (Response(zip_bytes()), 10),
    (Response(zip_bytes(), headers={'Content-Length': '99999'}), 100),
    (Response(zip_bytes(), headers={'Content-Length': '999'}), 9999),
])
def test_rejects_bad_and_oversized_download(response, limit):
    with patch('drive_import.requests.Session') as factory:
        factory.return_value.__enter__.return_value.get.return_value = response
        with pytest.raises(ValueError):
            with download_drive_zip('https://drive.google.com/open?id=abcdefghijk', max_bytes=limit):
                pytest.fail('invalid payload accepted')
        assert response.closed


@pytest.mark.parametrize('url', ['http://drive.google.com/open?id=abcdefghijk',
    'https://drive.google.com.evil.test/open?id=abcdefghijk',
    'https://localhost/open?id=abcdefghijk', 'https://drive.google.com/drive/folders/abcdefghijk'])
def test_invalid_links(url):
    with pytest.raises(ValueError): drive_file_id(url)


def test_redirect_to_private_host_rejected():
    with patch('drive_import.requests.Session') as factory:
        session = factory.return_value
        response = Response(b'', status=302, headers={'Location': 'http://127.0.0.1/private'})
        session.get.return_value = response
        with pytest.raises(ValueError): open_response(session, 'https://drive.google.com/open?id=abcdefghijk')
        assert session.get.call_count == 1 and response.closed


def test_no_whole_zip_read(tmp_path):
    class StreamingOnly(BytesIO):
        name = 'large.zip'
        def getvalue(self): raise AssertionError('whole archive copied')
        def read(self, size=-1):
            # zipfile reads the short end-of-central-directory tail with -1.
            if size < 0: assert len(self.getbuffer()) - self.tell() < 65536
            return super().read(size)
    upload = StreamingOnly(zip_bytes())
    assert discover_folder_companies([upload])
    assert len(list(iter_uploads([upload]))) == 1
    result = import_documents(Store(tmp_path), [upload], retain_archive=False)
    assert result.saved_files == 1 and not result.issues
    assert ArchiveLimits().max_total_bytes == 5 * 1024**3
