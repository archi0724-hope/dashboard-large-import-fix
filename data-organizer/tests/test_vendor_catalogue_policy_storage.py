import importlib.util
from pathlib import Path


def create_store(tmp_path, monkeypatch):
    dashboard = Path(__file__).resolve().parents[1] / "vendor_dashboard"
    monkeypatch.syspath_prepend(str(dashboard))
    spec = importlib.util.spec_from_file_location("vendor_policy_storage", dashboard / "storage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    store = module.Store(tmp_path)
    with store.connection() as db:
        db.execute("""INSERT INTO vdd_documents
            (id,company_key,company_name,filename,original_path,file_hash,uploaded_at)
            VALUES(1,'acme','Acme Medical','products.xlsx','products.xlsx','policy-test','2026-10-03')""")
    return store


def test_saved_catalogue_only_exposes_approved_non_drug_rows(tmp_path, monkeypatch):
    store = create_store(tmp_path, monkeypatch)
    store.save_product_records(1, [
        {"record_type": "price", "product_name": "Medical examination gloves", "price": "125"},
        {"record_type": "price", "product_name": "Amoxicillin 500 mg capsules", "price": "25"},
        {"record_type": "catalogue", "product_name": "Office chair"},
        {"record_type": "price", "product_name": "Medical ventilator"},
    ])

    assert store.product_record_count() == 1
    assert store.product_records()["product_name"].tolist() == ["Medical examination gloves"]
    assert store.product_classification_counts() == {
        "approved": 1, "review": 2, "excluded": 1,
    }
    assert set(store.product_review_records()["product_name"]) == {
        "Office chair", "Medical ventilator",
    }


def test_existing_rows_are_cleaned_once_by_policy_migration(tmp_path, monkeypatch):
    store = create_store(tmp_path, monkeypatch)
    store.save_product_records(1, [{
        "record_type": "catalogue",
        "product_name": "Sterile examination gloves",
    }])
    with store.connection() as db:
        db.execute("DELETE FROM vdd_meta WHERE key='catalogue_policy_version'")
        db.execute(
            "UPDATE vdd_product_records SET product_name='  Sterile   examination gloves  ', "
            "catalogue_status='review' WHERE document_id=1"
        )

    assert store.apply_catalogue_policy() == 1
    assert store.product_records()["product_name"].tolist() == ["Sterile examination gloves"]
    assert store.apply_catalogue_policy() == 0
