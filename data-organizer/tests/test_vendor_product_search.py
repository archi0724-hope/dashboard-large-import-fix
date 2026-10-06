import importlib.util
from pathlib import Path

import pytest


@pytest.mark.parametrize("query", ["acme", "GLOVE", "sku-123", "sterile"])
@pytest.mark.parametrize("record_type", ["", "catalogue", "price"])
def test_product_search_with_joined_company_names(tmp_path, monkeypatch, query, record_type):
    dashboard = Path(__file__).resolve().parents[1] / "vendor_dashboard"
    monkeypatch.syspath_prepend(str(dashboard))
    spec = importlib.util.spec_from_file_location("vendor_search_storage", dashboard / "storage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    store = module.Store(tmp_path)
    with store.connection() as db:
        db.execute("""INSERT INTO vdd_documents
            (id,company_key,company_name,filename,original_path,file_hash,uploaded_at)
            VALUES(1,'acme','Acme Supplies','catalogue.csv','catalogue.csv','test-doc','2026-10-03')""")
    store.save_product_records(1, [{
        "record_type": "catalogue", "product_name": "Glove", "sku": "SKU-123",
        "description": "Sterile gloves",
    }])

    records = store.product_records(query, record_type)

    if record_type == "price":
        assert records.empty
    else:
        assert records["product_name"].tolist() == ["Glove"]
        assert records["company_name"].tolist() == ["Acme Supplies"]
