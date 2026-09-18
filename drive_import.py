"""Bounded, disk-backed Google Drive ZIP downloads. No vendor bytes in session state."""
from contextlib import contextmanager
from html.parser import HTMLParser
import re
import shutil
import tempfile
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
import zipfile

import requests

MAX_DOWNLOAD = 5 * 1024**3
CHUNK_SIZE = 1024**2
GOOGLE_HOSTS = {"drive.google.com", "drive.usercontent.google.com", "docs.google.com"}


def validate_url(url):
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password or
            parsed.port not in (None, 443) or
            not (host in GOOGLE_HOSTS or host.endswith(".googleusercontent.com"))):
        raise ValueError("Use an HTTPS Google Drive file link.")
    return parsed


def drive_file_id(link):
    parsed = validate_url(link.strip())
    match = re.search(r"/file/d/([A-Za-z0-9_-]+)", parsed.path)
    file_id = match.group(1) if match else parse_qs(parsed.query).get("id", [""])[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", file_id):
        raise ValueError("Paste the Google Drive link for a ZIP file, not a folder.")
    return file_id


def drive_resource_key(link):
    """Keep the access key used by some public Drive share links."""
    parsed = validate_url(link.strip())
    resource_key = parse_qs(parsed.query).get("resourcekey", [""])[0]
    if resource_key and not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", resource_key):
        raise ValueError("The Google Drive link contains an invalid resource key.")
    return resource_key


class ConfirmationForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action = None
        self.fields = {}
        self.active = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and attrs.get("id") == "download-form":
            self.active = True
            self.action = attrs.get("action")
        if self.active and tag == "input" and attrs.get("name"):
            self.fields[attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.active = False


def open_response(session, url):
    for _ in range(6):
        validate_url(url)
        response = session.get(url, stream=True, timeout=(15, 60), allow_redirects=False)
        if response.status_code in (301, 302, 303, 307, 308):
            next_url = urljoin(url, response.headers.get("Location", ""))
            response.close()
            url = next_url
            continue
        try:
            response.raise_for_status()
        except Exception:
            response.close()
            raise ValueError("Drive refused the download. Check file permissions and download quota.") from None
        return response
    raise ValueError("Drive redirected too many times. Check the file link.")


class DiskUpload:
    name = "Google_Drive_Import.zip"

    def __init__(self, file, name=None):
        self.file = file
        if name:
            self.name = name

    def __getattr__(self, name):
        return getattr(self.file, name)


@contextmanager
def download_drive_zip(link, progress=None, max_bytes=MAX_DOWNLOAD):
    file_id = drive_file_id(link)
    params = {"id": file_id, "export": "download", "confirm": "t"}
    if resource_key := drive_resource_key(link):
        params["resourcekey"] = resource_key
    url = "https://drive.usercontent.google.com/download?" + urlencode(params)
    # Always remove the temporary ZIP, including failed or cancelled imports.
    with tempfile.TemporaryDirectory(prefix="vendor-import-") as folder, requests.Session() as session:
        for attempt in range(2):
            with open_response(session, url) as response:
                content_type = response.headers.get("Content-Type", "").lower()
                if "text/html" in content_type:
                    html = response.raw.read(1024**2 + 1, decode_content=True)
                    if len(html) > 1024**2:
                        raise ValueError("Drive returned a web page instead of a ZIP.")
                    form = ConfirmationForm()
                    form.feed(html.decode("utf-8", errors="replace"))
                    if attempt or not form.action:
                        raise ValueError("Drive did not return a ZIP. Check download permissions or quota; sign-in-only links cannot be imported.")
                    action = urljoin(url, form.action)
                    validate_url(action)
                    url = action + ("&" if "?" in action else "?") + urlencode(form.fields)
                    continue
                length = response.headers.get("Content-Length", "")
                total = int(length) if length.isdigit() else 0
                if total > max_bytes:
                    raise ValueError("Drive ZIP exceeds the 5 GB download limit.")
                free = shutil.disk_usage(folder).free
                if total and total + 64 * CHUNK_SIZE > free:
                    raise ValueError("Not enough temporary disk space to download this ZIP.")
                path = folder + "/Google_Drive_Import.zip"
                downloaded = 0
                with open(path, "wb") as output:
                    for chunk in response.iter_content(CHUNK_SIZE):
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            raise ValueError("Drive ZIP exceeds the 5 GB download limit.")
                        output.write(chunk)
                        if progress and (downloaded <= CHUNK_SIZE or downloaded // CHUNK_SIZE % 8 == 0):
                            progress(downloaded)
                if total and downloaded != total:
                    raise ValueError("Drive download was incomplete. Retry the import.")
                if not zipfile.is_zipfile(path):
                    raise ValueError("The downloaded file is not a readable ZIP.")
                with open(path, "rb") as upload:
                    yield DiskUpload(upload)
                return
