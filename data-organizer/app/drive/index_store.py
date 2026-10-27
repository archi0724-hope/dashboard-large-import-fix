"""Shared, resumable Drive inventory and unverified extracted records (no file blobs)."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime


def now():
    return datetime.now(UTC).isoformat()


TRACE_FIELDS = {'source_name', 'member_path', 'source_url', 'row', 'page', 'part', 'review_status'}


def record_identity(payload, company, entry, content_hash, record_key):
    record = json.loads(payload)
    if record.get('kind') == 'needs_ocr':
        return record_key  # Each image-only page still needs individual review.
    owner = company.strip().lower() if company else content_hash or entry
    content = {key:value for key,value in record.items() if key not in TRACE_FIELDS}
    value = json.dumps([owner,content],sort_keys=True,ensure_ascii=True)
    return hashlib.sha256(value.encode()).hexdigest()


class DriveIndex:
    def __init__(self, store):
        self.store = store
        self._totals_cache = {}
        with store.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS vdd_drive_sources (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
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
            if store.cloud:
                columns = {row['column_name'] for row in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='vdd_drive_records'").fetchall()}
                for column in ('dedup_identity','dedup_owner'):
                    if column not in columns:
                        db.execute(f"ALTER TABLE vdd_drive_records ADD COLUMN IF NOT EXISTS {column} TEXT NOT NULL DEFAULT ''")
            else:
                columns = {row['name'] for row in db.execute('PRAGMA table_info(vdd_drive_records)').fetchall()}
                for column in ('dedup_identity','dedup_owner'):
                    if column not in columns:
                        db.execute(f"ALTER TABLE vdd_drive_records ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            db.execute('CREATE INDEX IF NOT EXISTS vdd_drive_records_entry ON vdd_drive_records(entry_key)')
            db.execute('''CREATE TABLE IF NOT EXISTS vdd_drive_jobs (
                folder_id TEXT PRIMARY KEY, status TEXT NOT NULL, message TEXT NOT NULL,
                updated_at TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '')''')
            if not store.cloud:
                self.cache_record_identities(db)

    def source_selection(self):
        with self.store.connection() as db:
            row = db.execute("SELECT value FROM vdd_drive_sources WHERE key='selection'").fetchone()
        return json.loads(row['value']) if row else None

    def select_source(self, selection):
        with self.store.connection() as db:
            db.execute("INSERT INTO vdd_drive_sources(key,value) VALUES('selection',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(selection),))

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
                self._totals_cache.clear()
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
        self._totals_cache.clear()
        with self.store.connection() as db:
            if records is None:
                db.execute('UPDATE vdd_drive_entries SET status=?,error=?,updated_at=? WHERE entry_key=?',
                           (status,error[:1000],now(),key))
            else:
                db.execute('''UPDATE vdd_drive_entries SET status=?,error=?,content_hash=?,record_count=?,updated_at=?
                              WHERE entry_key=?''', (status,error[:1000],content_hash,records,now(),key))
                self.cache_record_identities(db,key)

    def reset_records(self, key):
        self._totals_cache.clear()
        with self.store.connection() as db:
            db.execute('DELETE FROM vdd_drive_records WHERE entry_key=?', (key,))

    def set_size(self, key, size):
        with self.store.connection() as db:
            db.execute('UPDATE vdd_drive_entries SET size_bytes=? WHERE entry_key=?',(size,key))

    def save_records(self, entry, records):
        self._totals_cache.clear()
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
            self.cache_record_identities(db,entry['entry_key'])

    def existing(self, key):
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM vdd_drive_entries WHERE entry_key=?', (key,)).fetchone()
        return dict(row) if row else None

    def pending(self, folder):
        with self.store.connection() as db:
            return [dict(row) for row in db.execute('''SELECT * FROM vdd_drive_entries
                WHERE folder_id=? AND status IN ('pending','failed','reading')
                ORDER BY CASE WHEN status='failed' THEN 1 ELSE 0 END,
                         CASE WHEN member_path='' THEN 1 ELSE 0 END,drive_id,size_bytes,entry_key''', (folder,)).fetchall()]

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
        totals = self.record_totals(folder)
        return {**(dict(row) if row else {'status':'idle','message':'Ready to index the connected folder.'}),
                'counts':counts,'records':totals['unique_records'],'raw_records':totals['raw_records'],
                'entries':sum(counts.values())}

    def owner_expression(self):
        trim = 'btrim' if self.store.cloud else 'trim'
        return f"CASE WHEN r.company<>'' THEN lower({trim}(r.company)) ELSE coalesce(nullif(e.content_hash,''),r.entry_key) END"

    def identity_expression(self, db):
        # Inserts and content-hash updates refresh this cache in the same transaction.
        # An uncached legacy row stays distinct until backfilled, never hidden by guesswork.
        return "coalesce(nullif(r.dedup_identity,''),r.record_key)"

    def cache_record_identities(self, db, entry=None):
        computed = self.computed_identity_expression(db)
        owner = self.owner_expression()
        condition = ' AND r.entry_key=?' if entry is not None else ''
        db.execute(f'''UPDATE vdd_drive_records AS r SET dedup_identity=({computed}),dedup_owner=({owner})
            FROM vdd_drive_entries e WHERE e.entry_key=r.entry_key
            AND (r.dedup_identity='' OR r.dedup_owner<>({owner})){condition}''',
            (entry,) if entry is not None else ())

    def computed_identity_expression(self, db):
        if not self.store.cloud:
            db.connection.create_function('vdd_record_identity',5,record_identity,deterministic=True)
            return 'vdd_record_identity(r.record_json,r.company,r.entry_key,e.content_hash,r.record_key)'
        # PostgreSQL JSON operations reject NUL escapes. Two different replacements
        # preserve NUL positions without equating them to a real replacement character.
        return '''CASE WHEN r.kind='needs_ocr' THEN r.record_key ELSE md5(json_build_array(
            CASE WHEN r.company<>'' THEN lower(btrim(r.company)) ELSE coalesce(nullif(e.content_hash,''),r.entry_key) END,
            (SELECT json_object_agg(j.key,j.value ORDER BY j.key)::text
             FROM json_each(replace(r.record_json,chr(92)||'u0000',chr(92)||'ufffd')::json) j
             WHERE j.key NOT IN ('source_name','member_path','source_url','row','page','part','review_status')),
            CASE WHEN strpos(r.record_json,chr(92)||'u0000')>0 THEN
             (SELECT json_object_agg(j.key,j.value ORDER BY j.key)::text
              FROM json_each(replace(r.record_json,chr(92)||'u0000',chr(92)||'ue000')::json) j
              WHERE j.key NOT IN ('source_name','member_path','source_url','row','page','part','review_status'))
            ELSE NULL END
            )::text) END'''

    def record_totals(self, folder=None):
        cached = self._totals_cache.get(folder)
        if cached and time.monotonic()-cached[0] < 30:
            return cached[1]
        with self.store.connection() as db:
            identity = self.identity_expression(db)
            where = ' WHERE r.folder_id=?' if folder is not None else ''
            params = (folder,) if folder is not None else ()
            row = db.execute(f'''SELECT count(*) AS raw_records,count(DISTINCT {identity}) AS unique_records
                FROM vdd_drive_records r LEFT JOIN vdd_drive_entries e ON e.entry_key=r.entry_key{where}''',params).fetchone()
        result = {key:int(row[key]) for key in ('raw_records','unique_records')}
        self._totals_cache[folder] = (time.monotonic(),result)
        return result

    def search(self, folder, query='', entries=False, offset=0, limit=100, kind=''):
        if kind not in {'', 'product'}:
            raise ValueError('Unsupported record kind.')
        table = 'vdd_drive_entries' if entries else 'vdd_drive_records'
        fields = "lower(source_name || ' ' || member_path || ' ' || status || ' ' || error)" if entries else 'search_text'
        params = (folder,'%'+query.casefold()[:200]+'%')
        condition = 'folder_id=? AND '+fields+' LIKE ?'
        if kind and not entries:
            condition += ' AND kind=?'
            params += (kind,)
        with self.store.connection() as db:
            if entries:
                total = db.execute(f'SELECT count(*) AS n FROM {table} WHERE {condition}',params).fetchone()['n']
                rows = db.execute(f'''SELECT * FROM {table} WHERE {condition}
                    ORDER BY source_name,member_path LIMIT ? OFFSET ?''',
                    (*params,min(100,limit),max(0,offset))).fetchall()
            else:
                identity = self.identity_expression(db)
                condition = 'r.'+condition.replace(' AND kind=', ' AND r.kind=')
                groups = f'''SELECT min(r.record_key) AS record_key,count(*) AS copies
                    FROM vdd_drive_records r LEFT JOIN vdd_drive_entries e ON e.entry_key=r.entry_key
                    WHERE {condition} GROUP BY {identity}'''
                total = db.execute(f'SELECT count(*) AS n FROM ({groups}) grouped',params).fetchone()['n']
                rows = db.execute(f'''WITH grouped AS ({groups}) SELECT r.*,g.copies FROM grouped g
                    JOIN vdd_drive_records r ON r.record_key=g.record_key
                    ORDER BY r.company,r.product,r.record_key LIMIT ? OFFSET ?''',
                    (*params,min(100,limit),max(0,offset))).fetchall()
        result = [dict(row) for row in rows]
        for item in result:
            item['url'] = f"https://drive.google.com/file/d/{item.get('drive_id','')}/view" if entries else ''
            if not entries:
                item['record'] = json.loads(item.pop('record_json'))
                item.pop('search_text',None)
        return {'total':int(total),'items':result}
