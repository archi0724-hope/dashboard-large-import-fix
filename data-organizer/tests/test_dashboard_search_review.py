"""Regressions for search coverage, document removal, and review navigation."""
import ast
from io import BytesIO
import json
from pathlib import Path
import re
from types import SimpleNamespace
import unicodedata

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

DASHBOARD = Path(__file__).resolve().parents[1] / 'vendor_dashboard'


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(DASHBOARD))
    from vendor_dashboard.storage import Store
    return Store(tmp_path / 'vendors')


@pytest.fixture
def search_functions():
    tree = ast.parse((DASHBOARD / 'app.py').read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith('vendor_ai_')]
    synonyms = next(node for node in tree.body if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'VENDOR_AI_SYNONYMS' for target in node.targets))
    namespace = dict(pd=pd, Path=Path, re=re, json=json, BytesIO=BytesIO, unicodedata=unicodedata,
        st=SimpleNamespace(session_state={}, cache_data=lambda **kwargs: lambda function: function))
    exec(compile(ast.Module(body=[synonyms, *nodes], type_ignores=[]), str(DASHBOARD/'app.py'), 'exec'), namespace)
    return SimpleNamespace(**namespace)


def test_rank_requires_actual_matches_and_retains_every_source(search_functions):
    rows = [dict(title='Medical glove', text='sterile glove latex', priority=5, score_hint=20,
                 source='Uploaded documents', result_id=f'document:{index}') for index in range(10)]
    rank = search_functions.vendor_ai_rank
    assert rank('nonexistentzz', rows) == []
    assert rank('show all', rows) == []
    assert len(rank('sterile glove', rows)) == 10
    assert rank('sterile needle', rows) == []


def test_document_search_reads_after_first_three_pdf_pages(search_functions):
    from reportlab.pdfgen.canvas import Canvas
    data = BytesIO()
    canvas = Canvas(data)
    for number in range(5):
        canvas.drawString(40, 700, 'LatePageNeedle' if number == 4 else 'First page')
        canvas.showPage()
    canvas.save()
    text, warning = search_functions.vendor_ai_extract_text('source.pdf', data.getvalue())
    assert 'LatePageNeedle' in text and not warning


def test_image_search_uses_ocr_and_reports_missing_engine(search_functions, monkeypatch):
    from PIL import Image
    import pytesseract
    data = BytesIO()
    Image.new('RGB', (40, 40), 'white').save(data, format='PNG')
    monkeypatch.setattr(pytesseract, 'image_to_string', lambda *args, **kwargs: 'ScannedNeedle')
    assert search_functions.vendor_ai_extract_text('scan.png', data.getvalue()) == ('ScannedNeedle', '')
    def missing(*args, **kwargs):
        raise pytesseract.TesseractNotFoundError()
    monkeypatch.setattr(pytesseract, 'image_to_string', missing)
    assert 'Tesseract is unavailable' in search_functions.vendor_ai_extract_text('scan.png', data.getvalue())[1]


def test_delete_removes_only_selected_upload_and_pending_rows(store):
    from vendor_core import classify
    for name in ['GST.txt', 'PAN Card.txt']:
        store.save_document('Alpha Medical/'+name, name.encode(), classify('Alpha Medical/'+name))
    documents = store.documents()
    ident = int(documents.iloc[0].id)
    other = int(documents.iloc[1].id)
    store.stage_product_records(ident, [{'product_name':'Sterile glove', 'price':10}])
    before = store.search_index_signature()
    assert store.delete_document(ident)
    assert not store.delete_document(ident)
    assert store.read_bytes(ident) is None and store.read_bytes(other)
    assert store.search_index_signature() != before
    with store.connection() as db:
        assert db.execute('SELECT COUNT(*) AS n FROM vdd_product_import_rows').fetchone()['n'] == 0
        assert db.execute('SELECT COUNT(*) AS n FROM vdd_product_imports').fetchone()['n'] == 0


def test_correction_invalidates_search_signature(store):
    from vendor_core import classify
    store.save_document('Alpha Medical/GST.txt', b'SyntheticGST', classify('Alpha Medical/GST.txt'))
    ident = int(store.documents().iloc[0].id)
    before = store.search_index_signature()
    store.correct_document(ident, 'Beta Medical', ['PAN Card'])
    assert store.search_index_signature() != before


def test_unified_search_finds_invoices_and_long_documents(store, search_functions):
    from vendor_core import classify
    store.save_document('Alpha Medical/notes.txt', b'x '*5000+b' LateTextNeedle', classify('Alpha Medical/notes.txt'))
    from app.purchase_invoices import SCHEMA
    record = {'fields':{'supplier':'InvoiceNeedle', 'invoice_number':'PI-1'}, 'items':[], 'raw_text':'ScannedInvoiceNeedle'}
    with store.connection() as db:
        db.execute(SCHEMA)
        db.execute('INSERT INTO vdd_purchase_invoices VALUES(?,?,?,?,?,?,?)',
                   ('invoice-1','hash','invoice.pdf',json.dumps(record),json.dumps(record['fields']),json.dumps(record).casefold(),'2026-11-19'))
    assert any(row.get('document_id') for row in search_functions.vendor_ai_search(store, 'LateTextNeedle'))
    assert any(row.get('invoice_id') == 'invoice-1' for row in search_functions.vendor_ai_search(store, 'ScannedInvoiceNeedle'))
    assert search_functions.vendor_ai_search(store, 'absentneedlezz') == []


def test_review_shows_classified_files_and_opens_preview(store, monkeypatch, admin_ui):
    from vendor_core import classify
    store.save_document('Alpha Medical/GST.txt', b'Synthetic original', classify('Alpha Medical/GST.txt'))
    monkeypatch.setenv('VENDOR_DATA_DIR', str(store.data_dir))
    monkeypatch.setenv('DATABASE_URL', '')
    app = AppTest.from_file(str(DASHBOARD/'app.py'))
    app.session_state['page'] = 'Review files'
    app.run(timeout=30)
    assert not app.exception
    assert app.checkbox(key='review_show_all').value is True
    assert app.selectbox(key='review_selected_doc').options
    assert any('Synthetic original' in str(item.value) for item in app.code)
    ident = int(store.documents().iloc[0].id)
    app.text_input[0].set_value('Beta Medical')
    app.multiselect[0].set_value(['PAN Card'])
    app.button(key=f'FormSubmitter:review_{ident}-Save correction').click().run()
    assert not app.exception
    assert store.documents().iloc[0].company_name == 'Beta Medical'
    assert store.documents().iloc[0].types == ['PAN Card']


def test_shared_search_finds_drive_and_saved_worksheet_rows(store, monkeypatch):
    from openpyxl import Workbook
    from app import served_workbook
    from search_sources import results
    from app.drive.index_store import DriveIndex
    monkeypatch.setattr(served_workbook, 'library', lambda: SimpleNamespace(store=store))
    book = Workbook()
    book.active.append(['Supplier', 'Product'])
    book.active.append(['WorksheetNeedle', 'Pump'])
    output = BytesIO()
    book.save(output)
    saved = served_workbook.save_workbook('invoices.xlsx', output.getvalue())
    found, warnings = results(store, 'WorksheetNeedle')
    assert len(found) == 1 and found[0]['workbook_id'] == saved['id'] and not warnings
    index = DriveIndex(store)
    with store.connection() as db:
        record = {'company':'DriveNeedle', 'product':'Pump', 'source_url':'https://drive.google.com/file/d/synthetic/view'}
        db.execute('INSERT INTO vdd_drive_records(record_key,folder_id,entry_key,kind,company,product,search_text,record_json) VALUES(?,?,?,?,?,?,?,?)',
                   ('r','shared','e','product','DriveNeedle','Pump','driveneedle pump',json.dumps(record)))
    found, warnings = results(store, 'DriveNeedle')
    assert len(found) == 1 and found[0]['source'] == 'Drive library' and not warnings


def test_company_row_deletes_multicategory_file_with_confirmation(store, monkeypatch, admin_ui):
    import streamlit as st
    from vendor_core import classify
    store.save_document('Alpha Medical/GST.txt', b'Synthetic original', classify('Alpha Medical/GST.txt'))
    ident = int(store.documents().iloc[0].id)
    store.correct_document(ident, 'Alpha Medical', ['GST', 'PAN Card'])
    monkeypatch.setenv('VENDOR_DATA_DIR', str(store.data_dir))
    monkeypatch.setenv('DATABASE_URL', '')
    original_expander = st.expander
    def open_expander(label, expanded=False, **kwargs):
        return original_expander(label, expanded=True, **kwargs)
    monkeypatch.setattr(st, 'expander', open_expander)
    app = AppTest.from_file(str(DASHBOARD/'app.py')).run(timeout=30)
    assert not app.exception
    buttons = [button for button in app.button if str(button.key).startswith('delete_document_')]
    assert len(buttons) == 2
    app.button(key=buttons[0].key).click().run()
    assert not app.exception and store.read_bytes(ident)
    confirm = next(button for button in app.button if str(button.key).startswith('confirm_delete_document_'))
    app.button(key=confirm.key).click().run()
    assert not app.exception and store.documents().empty
