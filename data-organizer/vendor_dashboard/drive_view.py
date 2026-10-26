"""Read the shared Drive index inside Vendor; never copy original bytes."""
from __future__ import annotations

import hashlib
import json
import logging
import re

import pandas as pd
import streamlit as st
from vendor_core import classify, company_key

from app.drive.index_store import DriveIndex


def company_source_path(path):
    """Ignore generated upload containers when detecting company folders."""
    return '/'.join(part for part in path.replace('\\', '/').split('/') if not re.fullmatch(r'upload-[a-f0-9]{8,}', part, re.IGNORECASE))


@st.cache_data(ttl=20, max_entries=8, show_spinner=False)
def inventory(identity, _store):
    index = DriveIndex(_store)
    selected = index.source_selection()
    # Older indexes predate the saved source-selection setting.
    with _store.connection() as db:
        if selected:
            rows = db.execute("SELECT * FROM vdd_drive_entries WHERE folder_id=?", (selected['scope'],)).fetchall()
            total = db.execute('SELECT count(*) AS n FROM vdd_drive_records WHERE folder_id=?', (selected['scope'],)).fetchone()['n']
        else:
            rows = db.execute("SELECT * FROM vdd_drive_entries").fetchall()
            total = db.execute('SELECT count(*) AS n FROM vdd_drive_records').fetchone()['n']
    result = []
    for row in rows:
        item = dict(row)
        path = item['member_path'] or item['source_name']
        if not item['member_path'] and path.lower().endswith('.zip'):
            continue  # Archive containers are not additional vendor documents.
        decision = classify(company_source_path(path))
        result.append({
            'Detected company': decision.company_name or 'Needs company review',
            'Document types': ', '.join(decision.document_types),
            'File / ZIP member': path, 'Status': item['status'],
            'Source rows': int(item['record_count']), 'Issue': item['error'],
            'Drive original': 'https://drive.google.com/file/d/'+item['drive_id']+'/view',
        })
    return {'items': result, 'source_rows': int(total), 'references': [dict(row) for row in rows]}


def combine_company_documents(vendors, documents, references, aliases=None):
    """Project Drive metadata into the existing checklist schema without file blobs."""
    from pathlib import PurePosixPath

    from app.drive.indexer import safe_member

    vendors = vendors.copy()
    documents = documents.copy()
    documents['drive_url'] = ''
    documents['drive_status'] = ''
    canonical = {row.company_key: row.company_name for row in vendors.itertuples(index=False)}
    alias_keys = {}
    if aliases is not None:
        alias_keys = {row.alias_key: row.canonical_key for row in aliases.itertuples(index=False)
                      if row.status == 'Confirmed' and row.canonical_key in canonical}
    seen = {(str(row.company_key), str(row.file_hash)) for row in documents.itertuples(index=False) if row.file_hash and row.available}
    additions, companies = [], []
    for item in references:
        path = item['member_path'] or item['source_name']
        if not item['member_path'] and path.lower().endswith('.zip'):
            continue
        if not safe_member(path) or not item['drive_id']:
            continue
        decision = classify(company_source_path(path))
        name = decision.company_name or ''
        # Match exact normalized names and confirmed aliases only, never fuzzy matches.
        key = company_key(name)
        key = alias_keys.get(key, key)
        name = canonical.get(key, name)
        digest = item.get('content_hash') or 'drive:'+item['entry_key']
        if (key, digest) in seen:
            continue
        seen.add((key, digest))
        if key and key not in canonical:
            canonical[key] = name
            companies.append({**{column: '' for column in vendors.columns}, 'company_key': key,
                              'company_name': name, 'canonical_id': 'Drive-'+hashlib.sha256(key.encode()).hexdigest()[:12], 'source': 'Google Drive'})
        available = item['status'] not in {'missing', 'deleted', 'blocked'} or (
            item['status'] == 'blocked' and 'exceeds the 128 MB' in item.get('error', ''))
        additions.append({**{column: '' for column in documents.columns},
            'id': 'drive:'+item['entry_key'], 'company_key': key, 'company_name': name,
            'filename': PurePosixPath(path).name, 'original_path': path,
            'types': list(decision.document_types), 'types_json': json.dumps(decision.document_types),
            'available': available, 'needs_review': decision.needs_review or item['status'] != 'indexed',
            'reviewed': 0, 'file_hash': digest, 'size_bytes': item.get('size_bytes', 0),
            'uploaded_at': item.get('updated_at', ''), 'source_batch': item['source_name'],
            'method': 'Google Drive · '+decision.method, 'reason': item.get('error') or decision.reason,
            'drive_url': 'https://drive.google.com/file/d/'+item['drive_id']+'/view', 'drive_status': item['status'],
        })
    if companies:
        vendors = pd.concat([vendors, pd.DataFrame(companies)], ignore_index=True)
    if additions:
        documents = pd.concat([documents, pd.DataFrame(additions)], ignore_index=True)
    documents['available'] = documents['available'].astype(bool)
    documents['needs_review'] = documents['needs_review'].astype(bool)
    documents = documents.sort_values('available', ascending=False, kind='stable').drop_duplicates(['company_key', 'file_hash']).sort_index().reset_index(drop=True)
    return vendors, documents


@st.cache_data(ttl=20, max_entries=24, show_spinner=False)
def source_rows(identity, query, offset, _store):
    index = DriveIndex(_store)
    selected = index.source_selection()
    if not selected:
        with _store.connection() as db:
            scope = db.execute('SELECT folder_id FROM vdd_drive_entries ORDER BY folder_id LIMIT 1').fetchone()
        if not scope:
            return {'total': 0, 'items': []}
        folder = scope['folder_id']
    else:
        folder = selected['scope']
    return index.search(folder, query, offset=offset)


@st.fragment
def render_drive_records(store):
    identity = hashlib.sha256((str(store.data_dir)+store.database_url).encode()).hexdigest()
    st.subheader('Google Drive companies & documents')
    st.caption('Shared records already saved in PostgreSQL. Originals stay in Drive. Company and document types below are detected from source paths and need review; these rows are not approved catalogue products.')
    if st.button('Refresh Drive records', key='drive_vendor_refresh'):
        inventory.clear()
        source_rows.clear()
    try:
        snapshot = inventory(identity, store)
    except Exception:
        logging.getLogger(__name__).exception('Vendor Drive inventory could not be loaded')
        st.warning('Drive records could not be loaded. Saved uploads remain available; retry Refresh Drive records.')
        return
    if not snapshot['items']:
        st.info('No Drive files have been indexed yet. Open Upload documents → Google Drive library to add a link.')
        return
    frame = pd.DataFrame(snapshot['items'])
    companies = frame.loc[frame['Detected company'] != 'Needs company review', 'Detected company'].str.casefold().nunique()
    c1, c2, c3 = st.columns(3)
    c1.metric('Drive companies detected', f'{companies:,}')
    c2.metric('Drive files indexed', f'{int(frame.Status.eq("indexed").sum()):,}')
    c3.metric('Drive source rows', f'{snapshot["source_rows"]:,}')
    query = st.text_input('Search Drive companies, files and document types', key='drive_vendor_files_query')
    filtered = frame
    if query:
        mask = frame[['Detected company', 'File / ZIP member', 'Document types']].astype(str).apply(lambda column: column.str.contains(query, case=False, regex=False)).any(axis=1)
        filtered = frame.loc[mask]
    page_count = max(1, (len(filtered)+99)//100)
    if st.session_state.get('drive_vendor_files_page', 1) > page_count:
        st.session_state['drive_vendor_files_page'] = 1
    page = st.number_input('Drive files page', min_value=1, max_value=page_count, value=1, key='drive_vendor_files_page')
    st.caption(f'{len(filtered):,} matching Drive files. Open a Drive ZIP original and use the listed member path to locate its document. Status and issues show files that are not fully searchable.')
    st.dataframe(filtered.iloc[(page-1)*100:page*100], hide_index=True, width='stretch',
        column_config={'Drive original': st.column_config.LinkColumn('Drive original', display_text='Open in Drive')})
    st.markdown('#### Search extracted Drive rows')
    row_query = st.text_input('Search all Drive source rows', key='drive_vendor_rows_query', placeholder='Company, product, phone, email or document text')
    previous = st.session_state.get('drive_vendor_previous_query')
    if previous != row_query:
        st.session_state['drive_vendor_rows_page'] = 1
        st.session_state['drive_vendor_previous_query'] = row_query
    row_page = st.number_input('Drive rows page', min_value=1, value=1, key='drive_vendor_rows_page')
    try:
        result = source_rows(identity, row_query, (row_page-1)*100, store)
    except Exception:
        logging.getLogger(__name__).exception('Vendor Drive source rows could not be loaded')
        st.warning('Source rows could not be loaded. Retry Refresh Drive records.')
        return
    records = []
    for item in result['items']:
        record = item['record']
        records.append({
            'Company': item['company'] or classify(record.get('member_path') or record.get('source_name', '')).company_name or 'Needs company review', 'Product / type': item['product'] or item['kind'],
            'Source': record.get('member_path') or record.get('source_name', ''),
            'Extracted data': json.dumps(record.get('values') or record.get('text') or record, ensure_ascii=False),
            'Review': record.get('review_status', 'Needs review'),
            'Drive original': record.get('source_url', ''),
        })
    st.caption(f'{result["total"]:,} searchable source rows. Showing at most 100 rows per page; search covers all saved rows.')
    if records:
        st.dataframe(pd.DataFrame(records), hide_index=True, width='stretch',
            column_config={'Drive original': st.column_config.LinkColumn('Drive original', display_text='Open in Drive')})
    else:
        st.info('No source rows on this page. Change the search or return to page 1.')
