from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import streamlit as st
from reportlab.pdfgen import canvas
from streamlit.testing.v1 import AppTest

from .test_vendor_drive_view import shared


def test_drive_card_has_lazy_eye_preview_and_download_like_saved_documents(shared, monkeypatch):
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
        assert any(button.label == '👁' for button in page.button)
        labels = [item.proto.label for item in page.get('link_button')]
        assert 'Download file' in labels
        assert 'Open original in Drive' not in labels
        assert not any('ZIP member/source:' in item.value for item in page.caption)
        with patch.dict('sys.modules',{'pypdf':None}), patch('streamlit.pdf',wraps=st.pdf) as viewer:
            next(button for button in page.button if button.label == '👁').click().run(timeout=30)
        assert not page.exception
        fetch.assert_called_once()
        viewer.assert_called_once_with(payload)
