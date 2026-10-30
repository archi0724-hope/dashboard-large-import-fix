"""Original invoice worksheets displayed directly in the Vendor workspace."""
import hashlib
import html

import streamlit as st

from app.served_workbook import original, save_workbook, workbooks, worksheet_rows


@st.cache_data(show_spinner=False, max_entries=2)
def original_bytes(store_identity, ident):
    record = original(ident)
    return bytes(record['payload'])


def reset_rows():
    st.session_state['served_row_offset'] = 0


def save_uploaded_workbook():
    uploaded = st.session_state.get('served_workbook_upload')
    if uploaded is None:
        return
    st.session_state.pop('served_upload_error', None)
    try:
        if uploaded.size > 20*1024**2:
            raise ValueError('Choose an Excel workbook up to 20 MB.')
        filename = uploaded.name.replace('\\', '/').rsplit('/', 1)[-1]
        if not filename.lower().endswith('.xlsx'):
            raise ValueError('Choose an .xlsx workbook.')
        result = save_workbook(filename, uploaded.getvalue())
        st.session_state['served_workbook_version'] = result['id']
        reset_rows()
    except ValueError as error:
        st.session_state['served_upload_error'] = str(error)
    except Exception:
        st.session_state['served_upload_error'] = 'The workbook could not be saved. Check database capacity and retry.'


def render_served_invoices(store):
    st.title('Served data invoices')
    available = workbooks()['items']
    identity = hashlib.sha256((str(store.data_dir)+store.database_url).encode()).hexdigest()
    if not available:
        st.caption('Original Excel workbook · All worksheets, columns and row order retained.')
        st.info('Add your original served data.xlsx workbook below to show its worksheets here.')
    else:
        identifiers = [record['id'] for record in available]
        selected = st.session_state.get('served_workbook_version', identifiers[0])
        if selected not in identifiers:
            selected = identifiers[0]
            st.session_state['served_workbook_version'] = selected
        record = next(record for record in available if record['id'] == selected)
        st.caption('Original '+record['filename']+' · All worksheets, columns and row order retained.')
        st.download_button('Download original Excel', original_bytes(identity, selected),
                           file_name=record['filename'],
                           mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                           on_click='ignore', key='served_original_download')
        names = [sheet['name'] for sheet in record['worksheets']]
        if st.session_state.get('served_original_sheet') not in names:
            st.session_state.pop('served_original_sheet', None)
            reset_rows()
        sheet = st.selectbox('Original worksheet', names, key='served_original_sheet', on_change=reset_rows)
        query = st.text_input('Search this worksheet', placeholder='Client, invoice, product or any cell',
                              key='served_worksheet_search', on_change=reset_rows)
        offset = st.session_state.get('served_row_offset', 0)
        data = worksheet_rows(selected, sheet, query, offset)
        caption = f"{data['matched']} of {data['total']} rows · {data['columns']} columns · Original worksheet order"
        if data['matched'] > data['limit']:
            caption += f" · Showing {offset+1}–{offset+len(data['items'])}"
        st.write(caption)
        widths = [104, 131, 244, 172, 320]+[160]*max(0, len(data['headers'])-5)
        widths = widths[:len(data['headers'])]
        columns = ''.join(f'<col style="width:{width}px">' for width in widths)
        heading = ''.join('<th scope="col">'+html.escape(value)+'</th>' for value in data['headers'])
        rows = ''.join('<tr data-source-row="'+str(row['source_row'])+'">'+''.join(
            '<td>'+html.escape(value)+'</td>' for value in row['values'])+'</tr>' for row in data['items'])
        st.markdown('''<style>
.served-original{overflow:auto;max-height:720px;border:1px solid #cbd9eb;border-radius:8px;margin-top:4px}
.served-original table{table-layout:fixed;border-collapse:collapse;font-size:13px}
.served-original th{position:sticky;top:0;background:#20547c;color:#fff;text-align:center;padding:20px 10px;border:1px solid #d6e0ef;z-index:1}
.served-original td{color:#008000;background:#fff;padding:10px;border:1px solid #d6e0ef;vertical-align:top;white-space:pre-wrap;overflow-wrap:anywhere}
.served-original tr:nth-child(even) td{background:#e9eff7}
</style>'''+f'<div class="served-original"><table aria-label="Original invoice worksheet" style="width:{sum(widths)}px"><colgroup>{columns}</colgroup><thead><tr>{heading}</tr></thead><tbody>{rows}</tbody></table></div>', unsafe_allow_html=True)
        if not data['matched']:
            st.info('No rows match this search.')
        if data['matched'] > data['limit']:
            previous, following = st.columns(2)
            if previous.button('Previous rows', disabled=offset == 0):
                st.session_state['served_row_offset'] = max(0, offset-data['limit'])
                st.rerun()
            if following.button('Next rows', disabled=offset+data['limit'] >= data['matched']):
                st.session_state['served_row_offset'] = offset+data['limit']
                st.rerun()

    with st.expander('Original workbook versions and upload', expanded=not available):
        if available:
            by_id = {record['id']:record for record in available}
            st.selectbox('Saved workbook', list(by_id), key='served_workbook_version',
                         format_func=lambda ident:by_id[ident]['filename']+' · '+by_id[ident]['created_at'][:10],
                         on_change=reset_rows)
        uploaded = st.file_uploader('Original Excel workbook', type=['xlsx'], max_upload_size=20, key='served_workbook_upload')
        st.button('Save original workbook', disabled=uploaded is None, on_click=save_uploaded_workbook)
        if error := st.session_state.get('served_upload_error'):
            st.error(error)
        st.caption('All original worksheets, columns and row order are kept. Downloads retain the original file. Uploads create versions without adding extracted invoice records.')
    with st.expander('Individual invoice upload and 31-column export'):
        from app.vendor_zip_upload import issue_upload_token
        from hospkart_chat.integration import vendor_setting
        url = '/purchase-invoices#'+issue_upload_token(vendor_setting('APP_PASSWORD'))
        st.link_button('Open individual invoice tools', url)
