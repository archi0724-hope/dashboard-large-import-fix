"""On-demand original Drive documents; no archive download or database writes."""
from __future__ import annotations

import hashlib
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


@contextmanager
def original_copy(store, document_id, limit=MAX_DOCUMENT):
    entry, filename = registered_original(store, document_id)
    if int(entry.get('size_bytes') or 0) > limit:
        raise OriginalUnavailable('This file is too large for inline preview. Use Download file.' if limit < MAX_DOCUMENT else 'This original exceeds the 128 MB document limit.')
    with tempfile.TemporaryDirectory(prefix='vendor-original-') as folder:
        target = Path(folder) / 'original'
        try:
            if entry['member_path']:
                with DriveRangeFile(entry['drive_id']) as remote, archive_members(remote) as archive:
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
        except OriginalUnavailable:
            raise
        except Exception:
            raise OriginalUnavailable('The Drive original could not be read. Check its sharing, download quota, or try again.') from None
        yield target, filename


def preview_bytes(store, document_id):
    with original_copy(store, document_id, limit=PREVIEW_LIMIT) as (target, _):
        return target.read_bytes()


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
