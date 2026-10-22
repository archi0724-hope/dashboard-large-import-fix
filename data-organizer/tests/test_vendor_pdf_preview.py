from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import streamlit as st
from streamlit.testing.v1 import AppTest


def test_pdf_preview_uses_original_without_page_extraction_library(tmp_path, monkeypatch):
    dashboard = Path(__file__).resolve().parents[1] / "vendor_dashboard"
    monkeypatch.syspath_prepend(str(dashboard))
    monkeypatch.setenv("VENDOR_DATA_DIR", str(tmp_path))
    for name in ("DATABASE_URL", "APP_PASSWORD", "CLOUD_DEPLOYMENT"):
        monkeypatch.setenv(name, "")
    from storage import Store
    from vendor_core import classify

    output = BytesIO()
    Image.new("RGB", (200, 100), "white").save(output, format="PDF")
    payload = output.getvalue()
    store = Store(tmp_path)
    filename = "Alpha Medical/Cancel Cheque.pdf"
    store.save_document(filename, payload, classify(filename))
    document_id = int(store.documents().iloc[0]["id"])
    page = AppTest.from_file(str(dashboard / "app.py"))
    page.secrets["VENDOR_DATA_DIR"] = str(tmp_path)
    page.secrets["APP_PASSWORD"] = "test-only-password"
    page.session_state["authenticated"] = True
    document = store.documents().iloc[0]
    category = document["types"][0] if document["types"] else "Other documents"
    page.session_state[f"company_category_{document['company_key']}_{category}"] = True
    page.run(timeout=30)
    assert not page.exception

    with patch.dict("sys.modules", {"pypdf": None}), \
            patch("streamlit.pdf", wraps=st.pdf) as viewer:
        next(button for button in page.button if button.label == "👁").click().run(timeout=30)

    assert not page.exception
    viewer.assert_called_once_with(payload)
    assert any("Showing the original PDF" in caption.value for caption in page.caption)
    assert store.read_bytes(document_id) == payload
