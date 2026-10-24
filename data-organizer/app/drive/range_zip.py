"""Bounded random reads of public Drive ZIPs; no full-archive download."""
from __future__ import annotations

import io
import re
import zipfile
from collections import OrderedDict
from urllib.parse import urlencode, urljoin

import requests

from vendor_dashboard.drive_import import ConfirmationForm, validate_url

MAX_READ = 16 * 1024**2
BLOCK = 256 * 1024


class DriveRangeFile(io.RawIOBase):
    def __init__(self, file_id):
        super().__init__()
        if not re.fullmatch(r'[A-Za-z0-9_-]{10,200}', file_id):
            raise ValueError('Invalid Drive file id.')
        self.session = requests.Session()
        self.url = 'https://drive.usercontent.google.com/download?' + urlencode(
            {'id': file_id, 'export': 'download', 'confirm': 't'})
        self.position = 0
        self.cache = OrderedDict()
        self.size = 0
        self.transferred = 0
        try:
            _, self.size = self._range(0, 0)
        except Exception:
            self.close()
            raise

    def _range(self, start, end):
        url = self.url
        for _ in range(7):
            validate_url(url)
            with self.session.get(url, headers={'Range': f'bytes={start}-{end}', 'Accept-Encoding': 'identity'},
                                  stream=True, timeout=(15, 60), allow_redirects=False) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    url = urljoin(url, response.headers.get('Location', ''))
                    continue
                if 'text/html' in response.headers.get('Content-Type', '').lower():
                    html = response.raw.read(1024**2 + 1)
                    form = ConfirmationForm()
                    form.feed(html[:1024**2].decode('utf-8', errors='replace'))
                    if len(html) > 1024**2 or not form.action:
                        raise ValueError('Drive denied access to this archive. Check sharing or download quota.')
                    url = urljoin(url, form.action)
                    validate_url(url)
                    url += ('&' if '?' in url else '?') + urlencode(form.fields)
                    continue
                content_range = response.headers.get('Content-Range', '')
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', content_range)
                if response.status_code != 206 or not match or int(match[1]) != start or int(match[2]) != end:
                    raise ValueError('Drive did not support a partial read. The full archive was not downloaded.')
                size = int(match[3])
                if self.size and size != self.size:
                    raise ValueError('The Drive archive changed during reading. Refresh and resume.')
                expected = end - start + 1
                data = response.raw.read(expected + 1)
                if len(data) != expected:
                    raise ValueError('Incomplete Drive partial read. Resume the index to retry.')
                self.transferred += len(data)
                self.url = url
                return data, size
        raise ValueError('Drive redirected too many times.')

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        position = offset if whence == io.SEEK_SET else self.position + offset if whence == io.SEEK_CUR else self.size + offset
        if position < 0:
            raise ValueError('Invalid ZIP offset.')
        self.position = position
        return position

    def read(self, size=-1):
        size = self.size - self.position if size < 0 else min(size, self.size - self.position)
        if size < 0:
            return b''
        if size > MAX_READ:
            raise ValueError('ZIP directory/read exceeds the 16 MB safety limit.')
        chunks = []
        while size:
            if size >= BLOCK:
                data = self._range(self.position,self.position+size-1)[0]
                chunks.append(data)
                self.position += len(data)
                size -= len(data)
                continue
            block = self.position // BLOCK
            if block not in self.cache:
                start = block * BLOCK
                self.cache[block] = self._range(start, min(self.size - 1, start + BLOCK - 1))[0]
                if len(self.cache) > 8:
                    self.cache.popitem(last=False)
            self.cache.move_to_end(block)
            data = self.cache[block]
            offset = self.position % BLOCK
            piece = data[offset:offset + size]
            if not piece:
                raise ValueError('Invalid ZIP read offset.')
            chunks.append(piece)
            self.position += len(piece)
            size -= len(piece)
        return b''.join(chunks)

    def close(self):
        if hasattr(self, 'session'):
            self.session.close()
        super().close()


def archive_members(source):
    archive = zipfile.ZipFile(source)
    try:
        members = archive.infolist()
        if len(members) > 5000:
            raise ValueError('ZIP has more than 5,000 entries. Split it into smaller archives.')
        return archive
    except Exception:
        archive.close()
        raise
