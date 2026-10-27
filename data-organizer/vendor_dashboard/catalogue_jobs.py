"""One bounded manual-import worker per server, independent of Streamlit reruns."""
from __future__ import annotations

import hashlib
import logging
import threading
import time

_lock = threading.RLock()
_jobs = {}
_active = None


def status(key):
    with _lock:
        value = _jobs.get(key)
        return dict(value) if value else None


def start(store, filename, content, *, retry=False):
    global _active
    identity = str(store.data_dir)+'\0'+store.database_url
    key = hashlib.sha256((identity+'\0').encode()+hashlib.sha256(content).digest()).hexdigest()
    with _lock:
        existing = _jobs.get(key)
        if existing and (existing['status'] == 'running' or (existing['status'] == 'failed' and not retry)):
            return key
        if _active and _jobs.get(_active, {}).get('status') == 'running':
            raise ValueError('Another workbook is still importing on this server. Wait for it to finish before starting another.')
        # Keep only bounded job metadata; uploaded bytes are released by the worker.
        if len(_jobs) >= 32:
            for old in list(_jobs):
                if _jobs[old]['status'] != 'running':
                    del _jobs[old]
                    break
        _jobs[key] = {'status':'running','phase':'Opening workbook','processed':0,'updated_at':time.time()}
        _active = key

    def progress(values):
        with _lock:
            _jobs[key].update(values, updated_at=time.time())

    def run():
        try:
            result = store.import_catalogue_workbook(filename, content, progress=progress)
        except Exception as error:
            logging.getLogger(__name__).exception('Background manual catalogue import failed')
            message = str(error) if isinstance(error, (ValueError, RuntimeError, OSError)) else f'Import failed ({type(error).__name__}). Retry; existing saved records are kept.'
            progress({'status':'failed','error':message})
        else:
            progress({'status':'complete','result':result,'phase':'Saved'})

    thread = threading.Thread(target=run, name='manual-catalogue-import', daemon=True)
    thread.start()
    thread.join(timeout=0.15)  # Small uploads finish immediately; large ones continue in the background.
    return key
