"""Shared, resumable Drive inventory and unverified extracted records (no file blobs)."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime


def now():
    return datetime.now(UTC).isoformat()


class DriveIndex:
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS vdd_drive_entries (
                entry_key TEXT PRIMARY KEY, folder_id TEXT NOT NULL, drive_id TEXT NOT NULL,
                source_name TEXT NOT NULL, member_path TEXT NOT NULL DEFAULT '', size_bytes BIGINT NOT NULL,
                version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL DEFAULT '', record_count BIGINT NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS vdd_drive_folder_status ON vdd_drive_entries(folder_id,status)')
            db.execute('''CREATE TABLE IF NOT EXISTS vdd_drive_records (
                record_key TEXT PRIMARY KEY, folder_id TEXT NOT NULL, entry_key TEXT NOT NULL,
                kind TEXT NOT NULL, company TEXT NOT NULL, product TEXT NOT NULL,
                search_text TEXT NOT NULL, record_json TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS vdd_drive_records_folder ON vdd_drive_records(folder_id)')
            db.execute('''CREATE TABLE IF NOT EXISTS vdd_drive_jobs (
                folder_id TEXT PRIMARY KEY, status TEXT NOT NULL, message TEXT NOT NULL,
                updated_at TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '')''')

    def discover(self, entries):
        if not entries:
            return
        with self.store.connection() as db:
            ids = sorted({entry['drive_id'] for entry in entries})
            previous = {row['entry_key']:row['version'] for row in db.execute(
                'SELECT entry_key,version FROM vdd_drive_entries WHERE folder_id=? AND drive_id IN ('+','.join('?' for _ in ids)+')',
                (entries[0]['folder_id'],*ids)).fetchall()}
            changed = []
            params = []
            for entry in entries:
                if entry['entry_key'] in previous and previous[entry['entry_key']] != entry['version']:
                    changed.append((entry['entry_key'],))
                params.append((entry['entry_key'],entry['folder_id'],entry['drive_id'],entry['source_name'],
                     entry.get('member_path',''),entry['size_bytes'],entry['version'],
                     entry.get('status','pending'),entry.get('error',''),now()))
            if changed:
                db.executemany('DELETE FROM vdd_drive_records WHERE entry_key=?',changed)
            db.executemany('''INSERT INTO vdd_drive_entries
                    (entry_key,folder_id,drive_id,source_name,member_path,size_bytes,version,status,error,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(entry_key) DO UPDATE SET
                    size_bytes=CASE WHEN excluded.size_bytes>0 THEN excluded.size_bytes ELSE vdd_drive_entries.size_bytes END,source_name=excluded.source_name,
                    status=CASE WHEN vdd_drive_entries.version=excluded.version THEN vdd_drive_entries.status ELSE excluded.status END,
                    error=CASE WHEN vdd_drive_entries.version=excluded.version THEN vdd_drive_entries.error ELSE excluded.error END,
                    record_count=CASE WHEN vdd_drive_entries.version=excluded.version THEN vdd_drive_entries.record_count ELSE 0 END,
                    version=excluded.version,updated_at=excluded.updated_at''',
                    params)

    def mark(self, key, status, error='', content_hash='', records=None):
        with self.store.connection() as db:
            if records is None:
                db.execute('UPDATE vdd_drive_entries SET status=?,error=?,updated_at=? WHERE entry_key=?',
                           (status,error[:1000],now(),key))
            else:
                db.execute('''UPDATE vdd_drive_entries SET status=?,error=?,content_hash=?,record_count=?,updated_at=?
                              WHERE entry_key=?''', (status,error[:1000],content_hash,records,now(),key))

    def reset_records(self, key):
        with self.store.connection() as db:
            db.execute('DELETE FROM vdd_drive_records WHERE entry_key=?', (key,))

    def set_size(self, key, size):
        with self.store.connection() as db:
            db.execute('UPDATE vdd_drive_entries SET size_bytes=? WHERE entry_key=?',(size,key))

    def save_records(self, entry, records):
        with self.store.connection() as db:
            params = []
            for record in records:
                payload = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
                if len(payload) > 128 * 1024:
                    raise ValueError('A source row exceeds the 128 KB index-row processing limit.')
                key = hashlib.sha256((entry['entry_key'] + payload).encode()).hexdigest()
                params.append((key,entry['folder_id'],entry['entry_key'],record.get('kind','row'),
                     str(record.get('company',''))[:500],str(record.get('product',''))[:500],
                     payload.casefold(),payload))
            db.executemany('''INSERT INTO vdd_drive_records
                    (record_key,folder_id,entry_key,kind,company,product,search_text,record_json)
                    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(record_key) DO NOTHING''',
                    params)

    def existing(self, key):
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM vdd_drive_entries WHERE entry_key=?', (key,)).fetchone()
        return dict(row) if row else None

    def pending(self, folder):
        with self.store.connection() as db:
            return [dict(row) for row in db.execute('''SELECT * FROM vdd_drive_entries
                WHERE folder_id=? AND status IN ('pending','failed','reading')
                ORDER BY CASE WHEN member_path='' THEN 1 ELSE 0 END,size_bytes,entry_key''', (folder,)).fetchall()]

    def state(self, folder, status, message, owner=''):
        with self.store.connection() as db:
            db.execute('''INSERT INTO vdd_drive_jobs(folder_id,status,message,updated_at,owner) VALUES(?,?,?,?,?)
                ON CONFLICT(folder_id) DO UPDATE SET status=excluded.status,message=excluded.message,
                updated_at=excluded.updated_at,owner=excluded.owner''', (folder,status,message[:1000],now(),owner))

    def status(self, folder):
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM vdd_drive_jobs WHERE folder_id=?', (folder,)).fetchone()
            counts = {r['status']:int(r['n']) for r in db.execute(
                'SELECT status,count(*) AS n FROM vdd_drive_entries WHERE folder_id=? GROUP BY status', (folder,)).fetchall()}
            records = db.execute('SELECT count(*) AS n FROM vdd_drive_records WHERE folder_id=?', (folder,)).fetchone()['n']
        return {**(dict(row) if row else {'status':'idle','message':'Ready to index the connected folder.'}),
                'counts':counts,'records':int(records),'entries':sum(counts.values())}

    def search(self, folder, query='', entries=False, offset=0, limit=100):
        table = 'vdd_drive_entries' if entries else 'vdd_drive_records'
        fields = "lower(source_name || ' ' || member_path || ' ' || status || ' ' || error)" if entries else 'search_text'
        params = (folder,'%'+query.casefold()[:200]+'%')
        with self.store.connection() as db:
            total = db.execute(f'SELECT count(*) AS n FROM {table} WHERE folder_id=? AND {fields} LIKE ?',params).fetchone()['n']
            rows = db.execute(f'''SELECT * FROM {table} WHERE folder_id=? AND {fields} LIKE ?
                ORDER BY {'source_name,member_path' if entries else 'company,product,record_key'} LIMIT ? OFFSET ?''',
                (*params,min(100,limit),max(0,offset))).fetchall()
        result = [dict(row) for row in rows]
        for item in result:
            item['url'] = f"https://drive.google.com/file/d/{item.get('drive_id','')}/view" if entries else ''
            if not entries:
                item['record'] = json.loads(item.pop('record_json'))
                item.pop('search_text',None)
        return {'total':int(total),'items':result}
