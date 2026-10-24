"""Validate pasted Drive sources before any network access."""
import re
from urllib.parse import parse_qs, urlparse


def parse_source(link):
    parsed = urlparse(link.strip())
    if parsed.scheme != 'https' or parsed.hostname != 'drive.google.com' or parsed.username or parsed.password or parsed.port not in (None,443):
        raise ValueError('Paste an HTTPS Google Drive folder or ZIP file link.')
    if parse_qs(parsed.query).get('resourcekey'):
        raise ValueError('This link requires a Drive resource key. Use a standard shared folder link containing the ZIP instead.')
    folder = re.fullmatch(r'/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]{10,200})/?',parsed.path)
    file = re.fullmatch(r'/file/d/([A-Za-z0-9_-]{10,200})(?:/view|/preview)?/?',parsed.path)
    if folder:
        return {'kind':'folder','id':folder[1],'url':'https://drive.google.com/drive/folders/'+folder[1]}
    ident = file[1] if file else parse_qs(parsed.query).get('id',[''])[0] if parsed.path in {'/open','/uc'} else ''
    if re.fullmatch(r'[A-Za-z0-9_-]{10,200}',ident):
        return {'kind':'zip','id':ident,'url':'https://drive.google.com/file/d/'+ident+'/view'}
    raise ValueError('Paste a Drive folder link or the shared file link for a ZIP archive, rather than a computer path.')
