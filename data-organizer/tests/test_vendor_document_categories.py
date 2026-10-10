from pathlib import Path

import pandas as pd
import pytest


@pytest.fixture
def category_filter(monkeypatch):
    dashboard = Path(__file__).resolve().parents[1] / "vendor_dashboard"
    monkeypatch.syspath_prepend(str(dashboard))
    from vendor_core import documents_for_category
    return documents_for_category


@pytest.mark.parametrize("category", ["GST", "Catalogue", "Other documents"])
def test_company_without_documents_retains_columns(category_filter, category):
    documents = pd.DataFrame(columns=["id", "types", "available", "filename"])

    selected = category_filter(documents, category)

    assert selected.empty
    assert selected.columns.tolist() == documents.columns.tolist()
    assert int(selected["available"].sum()) == 0


@pytest.mark.parametrize("category, expected_ids, available", [
    ("GST", [1, 2], 1),
    ("Catalogue", [1], 1),
    ("PAN Card", [], 0),
    ("Other documents", [3], 1),
])
def test_category_selection_and_saved_file_count(category_filter, category, expected_ids, available):
    documents = pd.DataFrame([
        {"id": 1, "types": ["GST", "Catalogue"], "available": True},
        {"id": 2, "types": ["GST"], "available": False},
        {"id": 3, "types": [], "available": True},
    ], index=[4, 8, 12])

    selected = category_filter(documents, category)

    assert selected["id"].tolist() == expected_ids
    assert selected.columns.tolist() == documents.columns.tolist()
    assert int(selected["available"].sum()) == available


def test_company_page_without_documents(tmp_path, monkeypatch):
    dashboard = Path(__file__).resolve().parents[1] / "vendor_dashboard"
    monkeypatch.syspath_prepend(str(dashboard))
    monkeypatch.setenv("VENDOR_DATA_DIR", str(tmp_path))
    for name in ("DATABASE_URL", "APP_PASSWORD", "CLOUD_DEPLOYMENT"):
        monkeypatch.setenv(name, "")
    from storage import Store
    from streamlit.testing.v1 import AppTest

    store = Store(tmp_path)
    store.upsert_vendors(pd.DataFrame({"company_name": ["Empty Test Company"]}))
    page = AppTest.from_file(str(dashboard / "app.py")).run(timeout=30)

    assert not page.exception
    assert any("0 documents saved" in caption.value for caption in page.caption)
    assert len(page.expander) >= 11
