"""On-demand original Drive documents; no archive download or database writes."""
from __future__ import annotations

import hashlib
from io import BytesIO
import mimetypes
import re
import stat
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from fastapi import HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from .indexer import safe_member
from .original_storage import download_saved_original, saved_archive_source, OriginalStorageUnavailable
from .public_drive import MAX_DOCUMENT, download_public_file
from .range_zip import DriveRangeFile, archive_members

PREVIEW_LIMIT = 16 * 1024**2


class OriginalUnavailable(ValueError):
    pass


def registered_original(store, document_id):
    """Resolve only an existing indexed entry or verified migrated document."""
    ident = str(document_id)
    with store.connection() as db:
        if ident.startswith('drive:'):
            key = ident[6:]
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', key):
                raise OriginalUnavailable('Document not found.')
            row = db.execute('SELECT * FROM vdd_drive_entries WHERE entry_key=?', (key,)).fetchone()
            if not row:
                raise OriginalUnavailable('Document not found.')
            entry = dict(row)
            filename = PurePosixPath(entry['member_path'] or entry['source_name']).name
            if not entry['member_path'] and filename.lower().endswith('.zip'):
                raise OriginalUnavailable('Choose a document inside the archive.')
        else:
            try:
                ident = int(ident)
            except (TypeError, ValueError):
                raise OriginalUnavailable('Document not found.') from None
            row = db.execute('SELECT filename,stored_path,file_hash,size_bytes FROM vdd_documents WHERE id=?', (ident,)).fetchone()
            if not row:
                raise OriginalUnavailable('Document not found.')
            from vendor_dashboard.storage import drive_original_reference
            url, member = drive_original_reference(row['stored_path'])
            if not url:
                raise OriginalUnavailable('This document has no verified Drive original.')
            entry = {'drive_id': urlsplit(url).path.split('/')[3], 'member_path': member,
                     'size_bytes': row['size_bytes'], 'content_hash': row['file_hash'], 'version': ''}
            filename = PurePosixPath(row['filename'].replace('\\', '/')).name
    if not re.fullmatch(r'[A-Za-z0-9_-]{10,200}', entry['drive_id']):
        raise OriginalUnavailable('Invalid Drive source.')
    if entry.get('member_path') and not safe_member(entry['member_path']):
        raise OriginalUnavailable('Unsafe archive member.')
    if not filename or filename in {'.', '..'}:
        raise OriginalUnavailable('Invalid original filename.')
    return entry, filename


def _copy_archive_member(archive, entry, target, limit):
    member = archive.getinfo(entry['member_path'])
    mode = member.external_attr >> 16
    if (member.is_dir() or not safe_member(member.filename) or stat.S_ISLNK(mode)
            or member.flag_bits & 1 or member.file_size > limit
            or member.file_size > max(1, member.compress_size) * 1000):
        raise OriginalUnavailable('This archive member cannot be previewed or downloaded safely.')
    version = entry.get('version', '')
    if re.fullmatch(r'\d+:\d+:\d+', version) and version != f'{member.CRC}:{member.file_size}:{member.compress_size}':
        raise OriginalUnavailable('The Drive original changed. Refresh the Drive library before retrying.')
    with archive.open(member) as source, target.open('wb') as output:
        total = 0
        while chunk := source.read(256 * 1024):
            total += len(chunk)
            if total > limit:
                raise OriginalUnavailable('The original exceeds the document size limit.')
            output.write(chunk)


def _copy_saved_archive(store, entry, digest, target, limit):
    candidates = [entry]
    if re.fullmatch(r'[a-fA-F0-9]{64}', digest):
        from vendor_dashboard.storage import drive_original_reference
        with store.connection() as db:
            rows = db.execute('SELECT stored_path FROM vdd_documents WHERE file_hash=?', (digest,)).fetchall()
        for row in rows:
            url, member = drive_original_reference(row['stored_path'])
            if url and member:
                candidates.append({'drive_id': urlsplit(url).path.split('/')[3], 'member_path': member, 'version': ''})
    seen = set()
    for candidate in candidates:
        token = (candidate['drive_id'], candidate.get('member_path', ''))
        if not token[1] or token in seen:
            continue
        seen.add(token)
        source = saved_archive_source(token[0])
        if source is not None:
            with source as remote, archive_members(remote) as archive:
                _copy_archive_member(archive, candidate, target, limit)
            return True
    return False


@contextmanager
def original_copy(store, document_id, limit=MAX_DOCUMENT):
    entry, filename = registered_original(store, document_id)
    if int(entry.get('size_bytes') or 0) > limit:
        raise OriginalUnavailable('This file is too large for inline preview. Use Download file.' if limit < MAX_DOCUMENT else 'This original exceeds the 128 MB document limit.')
    with tempfile.TemporaryDirectory(prefix='vendor-original-') as folder:
        target = Path(folder) / 'original'
        try:
            cached = None
            expected = entry.get('content_hash', '')
            if re.fullmatch(r'[a-fA-F0-9]{64}', expected):
                with store.connection() as db:
                    row = db.execute('SELECT payload FROM vdd_documents WHERE file_hash=? AND payload IS NOT NULL AND length(payload)<=? LIMIT 1', (expected, limit)).fetchone()
                cached = bytes(row['payload']) if row else None
            if cached is not None:
                target.write_bytes(cached)
            elif download_saved_original(expected, target, limit):
                pass
            elif _copy_saved_archive(store, entry, expected, target, limit):
                pass
            elif entry['member_path']:
                with DriveRangeFile(entry['drive_id']) as remote, archive_members(remote) as archive:
                    _copy_archive_member(archive, entry, target, limit)
            else:
                download_public_file(entry['drive_id'], target, max_bytes=limit)
            expected = entry.get('content_hash', '')
            if re.fullmatch(r'[a-fA-F0-9]{64}', expected):
                digest = hashlib.sha256()
                with target.open('rb') as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                if digest.hexdigest().lower() != expected.lower():
                    raise OriginalUnavailable('The Drive original changed. Refresh the Drive library before retrying.')
        except OriginalStorageUnavailable as error:
            raise OriginalUnavailable(str(error)) from None
        except OriginalUnavailable:
            raise
        except Exception:
            raise OriginalUnavailable('The Drive original could not be read. Check its sharing, download quota, or try again.') from None
        yield target, filename


def preview_bytes(store, document_id):
    entry, filename = registered_original(store, document_id)
    is_pdf = Path(filename).suffix.lower() == '.pdf'
    limit = MAX_DOCUMENT if is_pdf else PREVIEW_LIMIT
    with original_copy(store, document_id, limit=limit) as (target, _):
        if target.stat().st_size <= PREVIEW_LIMIT:
            return target.read_bytes()
        if not is_pdf:
            raise OriginalUnavailable('This original is too large for inline preview. Use Download file.')
        # Render a bounded first page from disk, preserving the complete original
        # for downloads and avoiding a large PDF in Streamlit session/cache memory.
        try:
            import pypdfium2 as pdfium
            document = pdfium.PdfDocument(str(target))
            try:
                page = document[0]
                try:
                    width, height = page.get_size()
                    scale = min(1.5, 1600/max(width, 1), 2000/max(height, 1))
                    bitmap = page.render(scale=scale)
                    try:
                        image = bitmap.to_pil().convert('RGB')
                        try:
                            output = BytesIO()
                            image.save(output, format='PDF', resolution=72)
                            preview = output.getvalue()
                        finally:
                            image.close()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            finally:
                document.close()
            if len(preview) > PREVIEW_LIMIT:
                raise ValueError('Preview exceeds memory bound')
            return preview
        except Exception:
            raise OriginalUnavailable('This PDF could not be previewed. Use Download file for the complete original.') from None


def download_response(store, document_id):
    stack = ExitStack()
    try:
        target, filename = stack.enter_context(original_copy(store, document_id))
        return FileResponse(target, filename=filename,
                            media_type=mimetypes.guess_type(filename)[0] or 'application/octet-stream',
                            headers={'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'},
                            background=BackgroundTask(stack.close))
    except OriginalUnavailable as error:
        stack.close()
        raise HTTPException(404 if str(error) == 'Document not found.' else 422, str(error)) from None
    except Exception:
        stack.close()
        raise
