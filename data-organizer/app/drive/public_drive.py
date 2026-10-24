"""Read public Drive folders without borrowing a browser or connector session."""
from __future__ import annotations

import hashlib
import re
import shutil
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

import requests

from vendor_dashboard.drive_import import ConfirmationForm, open_response, validate_url

from ..config import ConfigError, parse_drive_folder_id
from ..ingestion.base import SourceFile
from ..ingestion.registry import SUPPORTED_EXTENSIONS

MAX_FILES = 5000
MAX_DOCUMENT = 128 * 1024**2
MAX_LISTING = 4 * 1024**2


class FolderPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.entries = []
        self.current = None
        self.valid = False
        self.entry_count = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id') == 'flip-contents':
            self.valid = True
        if 'flip-entry' in attrs.get('class', '').split():
            self.entry_count += 1
        if tag == 'a':
            href = attrs.get('href', '')
            parsed = urlparse(href)
            if parsed.scheme != 'https' or parsed.netloc not in {'drive.google.com', 'docs.google.com'}:
                return
            match = re.fullmatch(r'/file/d/([\w-]{10,200})/view', parsed.path)
            if match:
                self.current = {'id': match[1], 'folder': False, 'name': ''}
            elif parsed.netloc == 'docs.google.com':
                native = re.match(r'/[a-z]+/d/([\w-]{10,200})(?:/|$)', parsed.path)
                if native:
                    self.current = {'id': native[1], 'folder': False, 'name': '', 'native': True}
            elif '/folders/' in parsed.path:
                try:
                    self.current = {'id': parse_drive_folder_id(href), 'folder': True, 'name': ''}
                except ConfigError:
                    pass

    def handle_data(self, data):
        if self.current is not None:
            self.current['name'] += data

    def handle_endtag(self, tag):
        if tag == 'a' and self.current is not None:
            self.current['name'] = self.current['name'].strip()
            self.entries.append(self.current)
            self.current = None


def list_public_files(folder_id):
    """Walk the public embedded folder view, with cycle and inventory bounds."""
    pending = [(parse_drive_folder_id(folder_id), '')]
    seen, files = set(), []
    with requests.Session() as session:
        while pending:
            folder, prefix = pending.pop()
            if folder in seen:
                continue
            seen.add(folder)
            if len(seen) > MAX_FILES:
                raise ConfigError('Drive folder contains too many subfolders. Choose a smaller source folder.')
            url = 'https://drive.google.com/embeddedfolderview?' + urlencode({'id': folder})
            with open_response(session, url) as response:
                content = response.raw.read(MAX_LISTING + 1, decode_content=True)
            if len(content) > MAX_LISTING:
                raise ConfigError('Drive folder listing is too large. Choose a smaller source folder.')
            page = FolderPage()
            page.feed(content.decode('utf-8', errors='replace'))
            if not page.valid:
                raise ConfigError('This Drive folder is private or unavailable. Share it for viewing with anyone who has the link, or configure Google authorization.')
            if page.entry_count and page.entry_count != len(page.entries):
                raise ConfigError('Some Drive entries could not be listed. Use Google authorization to read this folder completely.')
            for entry in page.entries:
                name = entry['name'].replace('\\', '_').replace('/', '_')
                if not name or name in {'.', '..'}:
                    raise ConfigError('Drive returned an invalid file name.')
                path = prefix + name
                if entry['folder']:
                    pending.append((entry['id'], path + '/'))
                else:
                    files.append({'id': entry['id'], 'name': name, 'path': path,
                                  'native': entry.get('native', False),
                                  'url': f"https://drive.google.com/file/d/{entry['id']}/view"})
                    if len(files) > MAX_FILES:
                        raise ConfigError('Drive contains more than 5,000 files. Choose a smaller source folder.')
    return sorted(files, key=lambda item: item['path'].casefold())


def download_public_file(file_id, destination, max_bytes=MAX_DOCUMENT):
    """Stream Google-only responses to an atomic cache file; never retain cookies."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.part')
    url = 'https://drive.usercontent.google.com/download?' + urlencode({'id': file_id, 'export': 'download', 'confirm': 't'})
    try:
        with requests.Session() as session:
            for attempt in range(2):
                with open_response(session, url) as response:
                    if 'text/html' in response.headers.get('Content-Type', '').lower():
                        content = response.raw.read(1024**2 + 1, decode_content=True)
                        form = ConfirmationForm()
                        form.feed(content[:1024**2].decode('utf-8', errors='replace'))
                        if attempt or len(content) > 1024**2 or not form.action:
                            raise ConfigError('Drive did not allow the file download. Check sharing and download quota.')
                        url = urljoin(url, form.action)
                        validate_url(url)
                        url += ('&' if '?' in url else '?') + urlencode(form.fields)
                        continue
                    length = response.headers.get('Content-Length', '')
                    if length.isdigit() and int(length) > max_bytes:
                        raise ConfigError('Drive document exceeds the 128 MB per-document limit.')
                    digest, size = hashlib.sha256(), 0
                    with temporary.open('wb') as output:
                        for chunk in response.iter_content(1024**2):
                            size += len(chunk)
                            if size > max_bytes:
                                raise ConfigError('Drive document exceeds the 128 MB per-document limit.')
                            if shutil.disk_usage(destination.parent).free < len(chunk) + 64 * 1024**2:
                                raise ConfigError('Not enough temporary disk space for the Drive document.')
                            digest.update(chunk)
                            output.write(chunk)
                    if length.isdigit() and size != int(length):
                        raise ConfigError('Drive download was incomplete. Retry the import.')
                    temporary.replace(destination)
                    return size, digest.hexdigest()
        raise ConfigError('Drive download could not be confirmed.')
    finally:
        temporary.unlink(missing_ok=True)


class PublicDriveSource:
    kind = 'drive'

    def __init__(self, settings, folder_id):
        self.settings, self.folder_id = settings, folder_id

    def describe(self):
        return f'drive:{self.folder_id}'

    def scan(self):
        # Public folder pages lack reliable file versions. Hash bounded documents
        # during refresh so edited files are picked up even when the name is unchanged.
        for item in list_public_files(self.folder_id):
            ext = Path(item['name']).suffix.lower()
            supported = ext in SUPPORTED_EXTENSIONS and not item.get('native')
            path = self.settings.raw_dir / 'drive-public' / (item['id'] + ext)
            size, version = download_public_file(item['id'], path) if supported else (0, '')
            yield SourceFile(file_key='drive:' + item['id'], name=item['name'], path=item['path'],
                             local_path=path, origin='drive', ext=ext, size=size, modified=version,
                             fingerprint=version, drive_id=item['id'], supported=supported)

    def ensure_local(self, sf):
        if not sf.local_path.is_file():
            download_public_file(sf.drive_id, sf.local_path)
        return sf
