from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import streamlit as st
from reportlab.pdfgen import canvas
from streamlit.testing.v1 import AppTest

from .test_vendor_drive_view import shared


def test_drive_card_has_preview_download_and_dashboard_delete(shared, monkeypatch, admin_ui):
    store, inventory, _ = shared
    with store.connection() as db:
        db.execute("UPDATE vdd_drive_entries SET member_path='Alpha Medical/Catalogue.pdf' WHERE entry_key='alpha'")
    inventory.clear()
    monkeypatch.setenv('VENDOR_DATA_DIR',str(store.data_dir))
    monkeypatch.setenv('DATABASE_URL','')
    output = BytesIO()
    pdf = canvas.Canvas(output)
    pdf.drawString(20,700,'Synthetic original catalogue preview')
    pdf.save()
    payload = output.getvalue()
    page = AppTest.from_file(str(Path(__file__).resolve().parents[1]/'vendor_dashboard/app.py'))
    page.session_state['company_category_alpha medical_Catalogue'] = True
    with patch('app.drive.originals.preview_bytes',return_value=payload) as fetch:
        page.run(timeout=30)
        assert not page.exception
        fetch.assert_not_called()
        assert any(button.label == 'Preview' for button in page.button)
        assert any(button.label == 'Delete' and not button.disabled for button in page.button)
        labels = [item.proto.label for item in page.get('link_button')]
        assert 'Download file' in labels
        assert 'Open original in Drive' not in labels
        assert not any('ZIP member/source:' in item.value for item in page.caption)
        with patch.dict('sys.modules',{'pypdf':None}), patch('streamlit.pdf',wraps=st.pdf) as viewer:
            next(button for button in page.button if button.label == 'Preview').click().run(timeout=30)
        assert not page.exception
        fetch.assert_called_once()
        viewer.assert_called_once_with(payload)
        page.button(key='delete_document_Catalogue_drive:alpha_0').click().run(timeout=30)
        assert any('original in Google Drive stays untouched' in item.value for item in page.warning)
        page.button(key='confirm_delete_document_Catalogue_drive:alpha_0').click().run(timeout=30)
    with store.connection() as db:
        assert db.execute("SELECT count(*) AS n FROM vdd_drive_entries WHERE entry_key='alpha'").fetchone()['n'] == 0


def test_word_original_has_in_dashboard_text_preview(shared,monkeypatch):
    from docx import Document
    store, inventory, _ = shared
    with store.connection() as db:
        db.execute("UPDATE vdd_drive_entries SET member_path='Alpha Medical/Letter.docx' WHERE entry_key='alpha'")
    inventory.clear()
    monkeypatch.setenv('VENDOR_DATA_DIR',str(store.data_dir))
    monkeypatch.setenv('DATABASE_URL','')
    output=BytesIO()
    document=Document()
    document.add_paragraph('Synthetic original company letter')
    document.save(output)
    page=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'vendor_dashboard/app.py'))
    page.session_state['company_category_alpha medical_Other documents']=True
    with patch('app.drive.originals.preview_bytes',return_value=output.getvalue()):
        page.run(timeout=30)
        next(button for button in page.button if button.label=='Preview').click().run(timeout=30)
    assert not page.exception
    assert any('Synthetic original company letter' in item.value for item in page.text)
