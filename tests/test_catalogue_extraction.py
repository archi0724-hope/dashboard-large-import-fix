from io import BytesIO

import pandas as pd

from catalogue_extraction import extract_records
from import_service import import_documents
from storage import Store


class Upload:
    def __init__(self, name, payload):
        self.name = name
        self._payload = payload

    def getvalue(self):
        return self._payload


def workbook_bytes():
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame([
            ["Supplier catalogue", None, None],
            ["Product code", "Product name", "Price"],
            ["G-1", "Nitrile gloves", "₹ 120.50"],
        ]).to_excel(writer, sheet_name="Products", index=False, header=False)
        pd.DataFrame([
            ["Notes"],
            ["Wheelchair"],
        ]).to_excel(writer, sheet_name="More products", index=False, header=False)
    return output.getvalue()


def test_extracts_all_sheets_and_detects_header_row():
    kind, records = extract_records("Alpha Medical price list.xlsx", workbook_bytes())

    assert kind == "price"
    assert {record["product_name"] for record in records} >= {"Nitrile gloves"}
    assert any(record["source_sheet"] == "Products" and record["price"] == 120.5 for record in records)
    assert all(record["extraction_confidence"] == "high" for record in records)


def test_extracts_structured_catalogue_fields_from_csv():
    content = (
        "Product,Code,Brand,Specification,Pack Size,Price,GST,Availability\n"
        "Surgical Gloves,SG-100,Acme,Latex sterile powder-free,Box of 100,850,12%,In stock\n"
    ).encode()

    kind, records = extract_records("Acme surgical catalogue.csv", content)

    assert kind == "price"
    assert records[0]["product_name"] == "Surgical Gloves"
    assert records[0]["sku"] == "SG-100"
    assert records[0]["brand"] == "Acme"
    assert records[0]["pack_size"] == "Box of 100"
    assert records[0]["gst"] == 12


def test_import_persists_records_and_restart_is_duplicate_safe(tmp_path):
    upload = Upload("Alpha Medical price list.xlsx", workbook_bytes())
    first = import_documents(Store(tmp_path), [upload], forced_company="Alpha Medical", retain_archive=False)

    assert first.saved_files == 1
    assert len(Store(tmp_path).product_records()) == 2

    second = import_documents(Store(tmp_path), [upload], forced_company="Alpha Medical", retain_archive=False)

    assert second.duplicate_files == 1
    assert len(Store(tmp_path).product_records()) == 2


def test_backup_restore_reextracts_product_records(tmp_path):
    upload = Upload("Alpha Medical price list.xlsx", workbook_bytes())
    store = Store(tmp_path)
    import_documents(store, [upload], forced_company="Alpha Medical", retain_archive=False)
    backup = store.backup_bytes()

    restored = Store(tmp_path / "restored")
    result = restored.restore_backup(backup)

    assert result["restored"] == 1
    assert len(restored.product_records()) == 2


def test_store_startup_backfills_documents_imported_before_extraction(tmp_path):
    upload = Upload("Alpha Medical price list.xlsx", workbook_bytes())
    store = Store(tmp_path)
    import_documents(store, [upload], forced_company="Alpha Medical", retain_archive=False)
    with store.connection() as db:
        db.execute("DELETE FROM vdd_product_records")

    restarted = Store(tmp_path)

    assert len(restarted.product_records()) == 2


def test_legacy_product_frame_can_be_displayed_with_new_columns():
    legacy = pd.DataFrame([{
        "company_name": "Alpha Medical",
        "record_type": "price",
        "product_name": "Surgical Gloves",
        "sku": "SG-100",
        "unit": "Box",
        "price": 850,
        "currency": "INR",
        "source_sheet": "Products",
    }])
    required = [
        "company_name", "record_type", "product_name", "sku", "brand", "specification",
        "pack_size", "unit", "price", "mrp", "gst", "currency", "source_file",
        "source_sheet", "source_page", "extraction_confidence",
    ]

    display = legacy.reindex(columns=list(dict.fromkeys([*legacy.columns, *required])), fill_value="")

    assert list(display[required].columns) == required
    assert display.loc[0, "product_name"] == "Surgical Gloves"
    assert display.loc[0, "brand"] == ""
