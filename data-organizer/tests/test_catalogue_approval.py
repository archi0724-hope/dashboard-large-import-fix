from __future__ import annotations

import importlib
from pathlib import Path

import pandas as pd
import pytest

from .test_api_export import client


@pytest.fixture
def vendor_store(tmp_path, monkeypatch):
    vendor_dir = Path(__file__).resolve().parents[1] / "vendor_dashboard"
    monkeypatch.syspath_prepend(str(vendor_dir))
    storage = importlib.import_module("storage")
    vendor_core = importlib.import_module("vendor_core")
    store = storage.Store(tmp_path / "vendor-data")
    store.upsert_vendors(pd.DataFrame({"company_name": ["Acme Supplies"]}))
    vendor = store.vendors().iloc[0]
    yield store, vendor_core.Decision("Acme Supplies", ("Price",), "test", "review required")


def add_import(store, decision, filename, content, product_name, sku, price, source_row):
    added = store.save_document(filename, content, decision, source_batch="approval test")
    assert added
    document_id = store.document_id_for(filename, content, "Acme Supplies")
    result = store.stage_product_records(document_id, [{
        "record_type": "price",
        "product_name": product_name,
        "sku": sku,
        "price": price,
        "currency": "INR",
        "pack_size": "box of 100",
        "unit": "box",
        "source_file": filename,
        "source_row": source_row,
    }])
    return result


def test_catalogue_rows_are_not_searchable_until_approved_and_price_changes_are_audited(vendor_store):
    store, decision = vendor_store
    first = add_import(store, decision, "prices.csv", b"first catalogue", "Surgical gloves", "SG-100", 125, 2)
    assert first["staged"] == 1
    assert store.product_records("SG-100").empty

    pending = store.product_import_rows(first["import_id"])[0]
    approved = store.decide_product_import_row(pending["id"], "approve")
    assert approved["status"] == "approved"
    assert store.product_records("SG-100")["price"].tolist() == [125]

    duplicate = store.stage_product_records(
        store.document_id_for("prices.csv", b"first catalogue", "Acme Supplies"),
        [{"record_type": "price", "product_name": "Surgical gloves", "sku": "SG-100",
          "price": 125, "currency": "INR", "pack_size": "box of 100", "unit": "box", "source_row": 2}],
    )
    assert duplicate["staged"] == 0

    revised = add_import(store, decision, "prices-revised.csv", b"revised catalogue",
                         "Surgical gloves", "SG-100", 140, 2)
    revised_row = store.product_import_rows(revised["import_id"])[0]
    assert revised_row["existing_match"]["price"] == 125
    store.edit_product_import_row(revised_row["id"], {"price": "140"})
    result = store.decide_product_import_row(revised_row["id"], "approve")

    assert result["status"] == "approved"
    assert store.product_records("SG-100")["price"].tolist() == [140]
    history = store.product_price_history(product_record_id=result["product_record_id"])
    assert [(entry["old_price"], entry["new_price"], entry["source_file"]) for entry in history] == [
        (125, 140, "prices-revised.csv")
    ]


def test_api_upload_requires_existing_vendor_and_returns_rows_to_review(client, tmp_path, monkeypatch):
    from app import main
    main.catalogue_store.cache_clear()
    monkeypatch.setenv("VENDOR_DATA_DIR", str(tmp_path / "api-vendor-data"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    store = main.catalogue_store()
    main.catalogue_store.cache_clear()

    store.upsert_vendors(pd.DataFrame({"company_name": ["Acme Supplies"]}))
    vendor_id = store.vendors().iloc[0].canonical_id
    monkeypatch.setattr(main, "catalogue_store", lambda: store)

    invalid_vendor = client.post(
        "/api/catalogue/imports",
        data={"vendor_id": "unknown"},
        files={"file": ("price-list.csv", b"Product Name,SKU,Price\nGloves,G1,125\n", "text/csv")},
    )
    assert invalid_vendor.status_code == 422

    response = client.post(
        "/api/catalogue/imports",
        data={"vendor_id": vendor_id},
        files={"file": ("price-list.csv", b"Product Name,SKU,Price\nGloves,G1,125\n", "text/csv")},
    )
    assert response.status_code == 200
    assert response.json()["records_extracted"] == 1
    assert client.get("/api/catalogue/imports").json()["items"][0]["pending"] == 1
    assert store.product_records("G1").empty

    rows = client.get(f"/api/catalogue/imports/{response.json()['import_id']}/rows").json()["items"]
    published = client.post(f"/api/catalogue/import-rows/{rows[0]['id']}/decide", json={"action": "approve"})
    assert published.status_code == 200
    assert store.product_records("G1")["price"].tolist() == [125]
