import hashlib
import re
import stat
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.drive import originals, range_zip, index_api
from app.drive.index_store import DriveIndex


class Response:
    def __init__(self, data, start, end):
        self.status_code = 206
        self.headers = {'Content-Range':f'bytes {start}-{end}/{len(data)}'}
        self.raw = BytesIO(data[start:end+1])
    def __enter__(self): return self
    def __exit__(self, *args): pass


class Session:
    def __init__(self, data):
        self.data, self.transferred = data, 0
    def get(self, url, headers, **kwargs):
        start, end = map(int, re.fullmatch(r'bytes=(\d+)-(\d+)', headers['Range']).groups())
        self.transferred += end-start+1
        return Response(self.data, start, end)
    def close(self): pass


@pytest.fixture
def source(tmp_path, monkeypatch):
    vendor = Path(__file__).resolve().parents[1] / 'vendor_dashboard'
    monkeypatch.syspath_prepend(str(vendor))
    from vendor_dashboard.storage import Store
    store = Store(tmp_path / 'store')
    index = DriveIndex(store)
    member = 'organized_by_company/upload-test/Alpha Medical Catalogue.pdf'
    content = b'%PDF-1.4\nSynthetic exact original bytes\n' + b'original-pdf-data'*20000
    data = BytesIO()
    with zipfile.ZipFile(data, 'w', compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('unused.bin', b'x'*(3*1024**2))
        archive.writestr(member, content)
        info = archive.getinfo(member)
        version = f'{info.CRC}:{info.file_size}:{info.compress_size}'
    index.discover([{'entry_key':'a'*64,'folder_id':'shared','drive_id':'archive12345',
        'source_name':'vendors.zip','member_path':member,'size_bytes':len(content),
        'version':version,'status':'indexed','content_hash':hashlib.sha256(content).hexdigest()}])
    session = Session(data.getvalue())
    monkeypatch.setattr(range_zip.requests, 'Session', lambda:session)
    return SimpleNamespace(store=store,index=index,member=member,content=content,session=session,
                           archive_size=len(data.getvalue()),ident='drive:'+'a'*64,tmp=tmp_path)


def test_reads_exact_zip_member_without_fetching_whole_archive(source):
    assert originals.preview_bytes(source.store, source.ident) == source.content
    assert source.session.transferred < source.archive_size // 2
    with source.store.connection() as db:
        assert db.execute('SELECT status FROM vdd_drive_entries').fetchone()['status'] == 'indexed'
        assert db.execute('SELECT count(*) AS n FROM vdd_documents').fetchone()['n'] == 0


def test_viewer_download_preserves_bytes_filename_and_cleans_temporary_copy(source, monkeypatch):
    monkeypatch.setattr(index_api,'library',lambda:source.index)
    directories = []
    temporary_directory = originals.tempfile.TemporaryDirectory
    def tracked_directory(*args, **kwargs):
        directory = temporary_directory(*args, **kwargs)
        directories.append(Path(directory.name))
        return directory
    monkeypatch.setattr(originals.tempfile, 'TemporaryDirectory', tracked_directory)
    app = FastAPI()
    app.include_router(index_api.router)
    response = TestClient(app).get('/api/drive-index/original/'+source.ident)
    assert response.status_code == 200
    assert response.content == source.content
    assert response.headers['content-type'] == 'application/pdf'
    assert quote('Alpha Medical Catalogue.pdf') in response.headers['content-disposition']
    assert response.headers['cache-control'] == 'private, no-store'
    assert directories and all(not directory.exists() for directory in directories)


@pytest.mark.parametrize('ident', ['drive:missing', 'invalid', '../outside'])
def test_unknown_documents_fail_before_network_access(source, ident):
    with pytest.raises(originals.OriginalUnavailable):
        originals.preview_bytes(source.store, ident)
    assert source.session.transferred == 0


def test_preview_metadata_limit_is_checked_before_download(source):
    with source.store.connection() as db:
        db.execute('UPDATE vdd_drive_entries SET size_bytes=?', (originals.PREVIEW_LIMIT+1,))
    with pytest.raises(originals.OriginalUnavailable, match='too large'):
        originals.preview_bytes(source.store,source.ident)
    assert source.session.transferred == 0


def test_changed_archive_version_and_wrong_hash_fail_closed(source):
    with source.store.connection() as db:
        db.execute("UPDATE vdd_drive_entries SET version='0:1:1'")
    with pytest.raises(originals.OriginalUnavailable, match='changed'):
        originals.preview_bytes(source.store,source.ident)
    with source.store.connection() as db:
        db.execute("UPDATE vdd_drive_entries SET version='',content_hash=?", ('0'*64,))
    with pytest.raises(originals.OriginalUnavailable, match='changed'):
        originals.preview_bytes(source.store,source.ident)


@pytest.mark.parametrize('member', ['../outside.pdf', '/outside.pdf', 'C:/outside.pdf'])
def test_unsafe_registered_paths_are_never_requested(source, member):
    with source.store.connection() as db:
        db.execute('UPDATE vdd_drive_entries SET member_path=?',(member,))
    with pytest.raises(originals.OriginalUnavailable, match='Unsafe'):
        originals.preview_bytes(source.store,source.ident)
    assert source.session.transferred == 0


@pytest.mark.parametrize('changes', [
    {'flag_bits':1}, {'external_attr':(stat.S_IFLNK|0o777)<<16},
    {'file_size':originals.PREVIEW_LIMIT+1}, {'file_size':2000,'compress_size':1},
])
def test_encrypted_symlink_oversized_and_extreme_ratio_members_are_rejected(source, monkeypatch, changes):
    from unittest.mock import Mock
    member = SimpleNamespace(filename=source.member, flag_bits=0, external_attr=0,
        file_size=len(source.content), compress_size=len(source.content), is_dir=lambda:False)
    for key,value in changes.items(): setattr(member,key,value)
    archive = Mock()
    archive.__enter__ = Mock(return_value=archive)
    archive.__exit__ = Mock(return_value=False)
    archive.getinfo.return_value = member
    monkeypatch.setattr(originals,'archive_members',lambda remote:archive)
    with pytest.raises(originals.OriginalUnavailable, match='safely'):
        originals.preview_bytes(source.store,source.ident)
    archive.open.assert_not_called()


def test_loose_drive_original_uses_bounded_public_download(source, monkeypatch):
    with source.store.connection() as db:
        db.execute("UPDATE vdd_drive_entries SET member_path='',source_name='Catalogue.pdf'")
    def download(file_id, target, max_bytes):
        assert file_id == 'archive12345' and max_bytes == originals.PREVIEW_LIMIT
        target.write_bytes(source.content)
    monkeypatch.setattr(originals,'download_public_file',download)
    assert originals.preview_bytes(source.store,source.ident) == source.content
    assert source.session.transferred == 0


def test_verified_migrated_document_reads_the_same_archive_bytes(source):
    url = 'https://drive.google.com/file/d/archive12345/view#member='+quote(source.member,safe='')
    with source.store.connection() as db:
        db.execute('''INSERT INTO vdd_documents
            (id,company_key,company_name,filename,original_path,file_hash,uploaded_at,stored_path,size_bytes)
            VALUES(1,'alpha','Alpha Medical','Catalogue.pdf','original.pdf',?,'now',?,?)''',
            (hashlib.sha256(source.content).hexdigest(),url,len(source.content)))
    assert originals.preview_bytes(source.store,'1') == source.content


def test_verified_saved_copy_opens_when_drive_archive_is_unavailable(source):
    digest = hashlib.sha256(source.content).hexdigest()
    url = 'https://drive.google.com/file/d/archive12345/view#member='+quote(source.member,safe='')
    with source.store.connection() as db:
        db.execute("""INSERT INTO vdd_documents
            (id,company_key,company_name,filename,original_path,file_hash,uploaded_at,stored_path,size_bytes,payload)
            VALUES(1,'alpha','Alpha Medical','Catalogue.pdf','original.pdf',?,'now',?,?,?)""",
            (digest,url,len(source.content),source.content))
    with source.store.connection() as db:
        db.execute('UPDATE vdd_drive_entries SET content_hash=?',(digest,))
    assert originals.preview_bytes(source.store,source.ident) == source.content
    assert originals.preview_bytes(source.store,'1') == source.content
    assert source.session.transferred == 0
    with source.store.connection() as db:
        db.execute("UPDATE vdd_documents SET payload=? WHERE id=1",(b'corrupted bytes',))
    with pytest.raises(originals.OriginalUnavailable,match='changed'):
        originals.preview_bytes(source.store,source.ident)
    assert source.session.transferred == 0


def test_private_storage_copy_is_verified_before_preview(source,monkeypatch):
    digest=hashlib.sha256(source.content).hexdigest()
    with source.store.connection() as db:
        db.execute('UPDATE vdd_drive_entries SET content_hash=?',(digest,))
    def saved(expected,target,limit):
        assert expected==digest
        target.write_bytes(source.content)
        return True
    monkeypatch.setattr(originals,'download_saved_original',saved)
    assert originals.preview_bytes(source.store,source.ident)==source.content
    assert source.session.transferred==0
    monkeypatch.setattr(originals,'download_saved_original',lambda expected,target,limit:(target.write_bytes(b'wrong'),True)[1])
    with pytest.raises(originals.OriginalUnavailable,match='changed'):
        originals.preview_bytes(source.store,source.ident)
