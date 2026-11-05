"""Private, content-addressed originals; no storage credentials reach browsers."""
from __future__ import annotations

import os
import re
from pathlib import Path


class OriginalStorageUnavailable(ValueError):
    pass


def _client():
    endpoint = os.environ.get('VENDOR_ORIGINAL_STORAGE_ENDPOINT', '')
    bucket = os.environ.get('VENDOR_ORIGINAL_STORAGE_BUCKET', '')
    access = os.environ.get('VENDOR_ORIGINAL_STORAGE_ACCESS_KEY', '')
    secret = os.environ.get('VENDOR_ORIGINAL_STORAGE_SECRET_KEY', '')
    if not all((endpoint, bucket, access, secret)):
        return None, ''
    if not re.fullmatch(r'https://br-[A-Za-z0-9-]+\.storage\.[a-z0-9.-]+\.neon\.tech', endpoint):
        raise OriginalStorageUnavailable('Original document storage is not configured correctly.')
    import boto3
    from botocore.config import Config
    client = boto3.client('s3', endpoint_url=endpoint,
        aws_access_key_id=access, aws_secret_access_key=secret,
        region_name=os.environ.get('VENDOR_ORIGINAL_STORAGE_REGION', 'ap-southeast-1'),
        config=Config(signature_version='s3v4', s3={'addressing_style': 'path'},
                      connect_timeout=10, read_timeout=30, retries={'max_attempts': 2}))
    return client, bucket


def download_saved_original(digest: str, target: Path, limit: int) -> bool:
    if not re.fullmatch(r'[a-fA-F0-9]{64}', digest):
        return False
    client, bucket = _client()
    if client is None:
        return False
    body = None
    try:
        response = client.get_object(Bucket=bucket, Key='sha256/'+digest.lower())
        body = response['Body']
        if int(response.get('ContentLength', 0)) > limit:
            raise OriginalStorageUnavailable('This original exceeds the preview size limit. Use Download file.')
        total = 0
        with target.open('wb') as output:
            while chunk := body.read(min(256*1024, limit-total+1)):
                total += len(chunk)
                if total > limit:
                    raise OriginalStorageUnavailable('This original exceeds the preview size limit. Use Download file.')
                output.write(chunk)
        if 'ContentLength' in response and total != int(response['ContentLength']):
            raise OriginalStorageUnavailable('The original download was incomplete. Please retry.')
        return True
    except Exception as error:
        target.unlink(missing_ok=True)
        code = str(getattr(error, 'response', {}).get('Error', {}).get('Code', ''))
        if code in {'NoSuchKey', '404', 'NotFound'}:
            return False
        if isinstance(error, OriginalStorageUnavailable):
            raise
        raise OriginalStorageUnavailable('Saved original storage could not be read. Please retry.') from None
    finally:
        if body is not None:
            body.close()

# ZIP reads share the existing bounded seek/read implementation.
from collections import OrderedDict
import io
from .range_zip import DriveRangeFile


class PrivateArchiveRangeFile(DriveRangeFile):
    def __init__(self, client, bucket, key):
        io.RawIOBase.__init__(self)
        self.client, self.bucket, self.key = client, bucket, key
        self.position, self.transferred = 0, 0
        self.cache = OrderedDict()
        self.size = int(client.head_object(Bucket=bucket, Key=key)['ContentLength'])
        if not 0 < self.size <= 5*1024**3:
            raise OriginalStorageUnavailable('Saved original archive exceeds the supported size.')

    def _range(self, start, end):
        response = self.client.get_object(Bucket=self.bucket, Key=self.key, Range=f'bytes={start}-{end}')
        body = response['Body']
        try:
            if response.get('ContentRange') != f'bytes {start}-{end}/{self.size}':
                raise OriginalStorageUnavailable('Saved archive returned an invalid partial read.')
            data = body.read(end-start+2)
            if len(data) != end-start+1:
                raise OriginalStorageUnavailable('Saved archive returned an incomplete partial read.')
            self.transferred += len(data)
            return data, self.size
        finally:
            body.close()

    def close(self):
        if not self.closed:
            close = getattr(self.client, 'close', None)
            if close:
                close()
        io.RawIOBase.close(self)


def saved_archive_source(drive_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{10,200}', drive_id):
        return None
    client, bucket = _client()
    if client is None:
        return None
    try:
        return PrivateArchiveRangeFile(client, bucket, 'archives/'+drive_id+'.zip')
    except Exception as error:
        close = getattr(client, 'close', None)
        if close:
            close()
        code = str(getattr(error, 'response', {}).get('Error', {}).get('Code', ''))
        if code in {'NoSuchKey', '404', 'NotFound'}:
            return None
        if isinstance(error, OriginalStorageUnavailable):
            raise
        raise OriginalStorageUnavailable('Saved original archive could not be read. Please retry.') from None
