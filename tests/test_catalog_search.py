from datetime import date, timedelta
from io import BytesIO

from openpyxl import Workbook
import pytest

from catalog_search import (CATALOG_TEMPLATE, catalog_documents, extract_catalog, find_offers,
                            load_catalog, number, parse_requirements, refresh_catalog, tokens)
from storage import Store
from vendor_core import classify


def save_catalog(store, vendor, price=2500, **fields):
    import csv
    from io import StringIO
    record = {"Product": "Office chair", "Specifications": "Mesh back", "Category": "Furniture",
              "Price": price, "Currency": "INR", "Unit": "each", "MOQ": 1, "Stock": 100, **fields}
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=record)
    writer.writeheader()
    writer.writerow(record)
    path = f"{vendor}/Price list.csv"
    store.save_document(path, output.getvalue().encode(), classify(path, forced_company=vendor))


def indexed_entries(store):
    refresh_catalog(store, store.documents())
    return load_catalog(store, store.documents())[0]


def test_multi_item_requirements_keep_quantity_size_and_budget_separate():
    chair, syringe = parse_requirements("I need 20 office chairs under INR 5,000 each and 500 5 ml syringes")
    assert chair.terms == {"office", "chair"}
    assert (chair.quantity, chair.budget, chair.currency) == (20, 5000, "INR")
    assert syringe.terms == {"5ml", "syringe"}
    assert syringe.quantity == 500
    assert parse_requirements("5 ml syringes")[0].quantity == 1
    pack = parse_requirements("10 packs of 100 syringes")[0]
    assert pack.quantity == 10 and pack.unit == "pack of 100" and pack.terms == {"syringe"}
    assert parse_requirements("Can you show me 20 office chairs?")[0].quantity == 20
    natural = parse_requirements("We are looking for 20 chairs.")[0]
    assert natural.quantity == 20 and natural.terms == {"chair"}
    assert "sterile" not in tokens("non-sterile syringe")


def test_lowest_comparable_vendor_and_quantity_total(tmp_path):
    store = Store(tmp_path)
    save_catalog(store, "Alpha Furniture", 2500)
    save_catalog(store, "Beta Furniture", 2100)
    save_catalog(store, "Over Budget", 9000)
    entries = indexed_entries(store)
    offers = find_offers(entries, parse_requirements("20 office chairs under INR 3000 each")[0])
    assert [offer["vendor"] for offer in offers] == ["Beta Furniture", "Alpha Furniture"]
    assert offers[0]["lowest"] and offers[0]["total"] == 42000
    assert not offers[1]["lowest"]
    assert offers[0]["location"] == "CSV, row 2"
    assert offers[0]["filename"] == "Price list.csv"


@pytest.mark.parametrize("value", ["From 500", "100-200", "100 / box", "NaN", "Inf", "=2+3", "-100", ""])
def test_ambiguous_or_invalid_prices_are_never_ranked(value):
    assert number(value) is None


def test_currency_units_moq_stock_expiry_and_quotes_not_misrepresented(tmp_path):
    store = Store(tmp_path)
    save_catalog(store, "Valid Vendor", 2500)
    save_catalog(store, "USD Vendor", 10, Currency="USD")
    save_catalog(store, "Pack Vendor", 10, Unit="box", **{"Pack size": 100})
    save_catalog(store, "Unknown Pack", 5, Unit="box")
    save_catalog(store, "MOQ Vendor", 5, MOQ=100)
    save_catalog(store, "Low Stock Vendor", 5, Stock=2)
    save_catalog(store, "No Stock Vendor", 5, Availability="Out of stock")
    save_catalog(store, "Expired Vendor", 5, **{"Valid until": (date.today() - timedelta(days=1)).isoformat()})
    save_catalog(store, "Unknown Currency", 5, Currency="")
    save_catalog(store, "Quote Vendor", "On request")
    offers = find_offers(indexed_entries(store), parse_requirements("20 office chairs under INR 3000 each")[0])
    assert offers[0]["vendor"] == "Valid Vendor"
    assert sum(offer["eligible"] for offer in offers) == 1
    assert all(offer["total"] is None for offer in offers[1:])
    assert not any(offer["lowest"] for offer in offers)
    assert any("expired" in offer["status"] for offer in offers)


def test_different_specifications_and_models_do_not_get_best_price_label(tmp_path):
    store = Store(tmp_path)
    save_catalog(store, "Mesh Vendor", 2500)
    save_catalog(store, "Wood Vendor", 1000, Specifications="Wood")
    offers = find_offers(indexed_entries(store), parse_requirements("office chairs")[0])
    assert len(offers) == 2 and not any(offer["lowest"] for offer in offers)
    save_catalog(store, "Different Model Vendor", 500, Model="A2")
    offers = find_offers(indexed_entries(store), parse_requirements("office chairs")[0])
    assert not any(offer["lowest"] for offer in offers)


def test_explicit_single_line_text_prices_require_currency_and_unit():
    entries, _ = extract_catalog("Catalog.txt", b"Office chair | INR 2,500 | each\nSyringe rate: INR 4.50 / piece\nTable model 2500\nDesk | 3000 | each")
    priced = [entry for entry in entries if entry["price"] is not None]
    assert len(priced) == 2
    assert [entry["price"] for entry in priced] == [2500, 4.5]
    assert priced[0]["product"] == "Office chair" and priced[0]["unit"] == "each"


def test_text_catalogs_supply_evidence_without_guessing_price():
    entries, notes = extract_catalog("Catalog.txt", b"5ml sterile syringes\nPack size 100\nItem code 4500")
    assert len(entries) == 1 and not notes
    assert entries[0]["price"] is None
    assert entries[0]["kind"] == "excerpt"
    assert "Item code 4500" in entries[0]["evidence"]


def test_xlsx_sheets_dates_and_contact_provenance():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Consumables"
    sheet.append(["Product", "Price INR", "Unit", "Valid until", "Contact"])
    sheet.append(["5ml Syringe", 4.5, "pcs", date(2030, 1, 1), "sales@example.com"])
    second = workbook.create_sheet("Furniture")
    second.append(["Item", "Price", "Currency", "Unit"])
    second.append(["Chair", 2500, "INR", "each"])
    output = BytesIO()
    workbook.save(output)
    entries, notes = extract_catalog("Price.xlsx", output.getvalue())
    assert not notes and len(entries) == 2
    assert entries[0]["valid_until"] == "2030-01-01"
    assert entries[0]["location"] == "Consumables, row 2"
    assert entries[0]["contact"] == "sales@example.com"
    assert entries[0]["currency"] == "INR"


def test_index_is_persistent_idempotent_and_filters_corrections(tmp_path):
    store = Store(tmp_path)
    save_catalog(store, "Alpha Furniture")
    documents = store.documents()
    assert load_catalog(store, documents)[2] == 1
    assert refresh_catalog(store, documents)["updated"] == 1
    reopened = Store(tmp_path)
    assert refresh_catalog(reopened, documents)["updated"] == 0
    assert len(load_catalog(reopened, documents)[0]) == 1
    document_id = int(documents.iloc[0].id)
    store.correct_document(document_id, "Renamed Furniture", ["Price"])
    assert load_catalog(store, store.documents())[0][0]["vendor"] == "Renamed Furniture"
    store.correct_document(document_id, "Renamed Furniture", ["GST"])
    assert not load_catalog(store, store.documents())[0]


def test_missing_file_and_unassigned_documents_not_exposed(tmp_path):
    store = Store(tmp_path)
    save_catalog(store, "Alpha Furniture")
    store.save_document("Catalog.csv", CATALOG_TEMPLATE.encode(), classify("Catalog.csv"))
    assert len(catalog_documents(store.documents())) == 1
    entries = indexed_entries(store)
    assert len(entries) == 1
    for file in store.files_dir.iterdir():
        file.unlink()
    assert not load_catalog(store, store.documents())[0]


def test_mixed_sensitive_documents_are_not_sales_visible(tmp_path):
    store = Store(tmp_path)
    save_catalog(store, "Alpha Furniture")
    doc_id = int(store.documents().iloc[0].id)
    store.correct_document(doc_id, "Alpha Furniture", ["Catalogue", "GST"])
    assert catalog_documents(store.documents()).empty


def test_reset_deletes_derived_index(tmp_path):
    store = Store(tmp_path)
    save_catalog(store, "Alpha Furniture")
    assert indexed_entries(store)
    store.reset_data("RESET HISTORY")
    assert not store.catalog_index()
    assert not load_catalog(store, store.documents())[0]
    assert store.reset_status()["clean"]


def test_backup_restores_catalog_sources_and_rebuilds_index(tmp_path):
    original = Store(tmp_path / "original")
    save_catalog(original, "Alpha Furniture")
    indexed_entries(original)
    restored = Store(tmp_path / "restored")
    restored.restore_backup(original.backup_bytes())
    assert load_catalog(restored, restored.documents())[2] == 1
    assert indexed_entries(restored)[0]["price"] == 2500


def test_newer_vendor_price_prevents_recommending_stale_cheap_quote(tmp_path):
    store = Store(tmp_path)
    save_catalog(store, "Alpha Furniture", 1000)
    old = indexed_entries(store)[0]
    old["uploaded_at"] = "2025-01-01T00:00:00+00:00"
    newer = {**old, "price": 4000, "uploaded_at": "2026-09-01T00:00:00+00:00", "evidence": "Updated price: INR 4000"}
    offers = find_offers([old, newer], parse_requirements("10 office chairs under INR 2000 each")[0])
    assert len(offers) == 1
    assert not offers[0]["eligible"] and "Older listing" in offers[0]["status"]


def test_unsupported_format_and_blank_pdf_report_limits():
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    output = BytesIO()
    writer.write(output)
    entries, notes = extract_catalog("Catalog.pdf", output.getvalue())
    assert not entries and any("OCR" in note for note in notes)
    entries, notes = extract_catalog("Catalog.png", b"fake-scan")
    assert not entries and notes


def test_template_has_product_and_price_metadata():
    entries, notes = extract_catalog("Price.csv", CATALOG_TEMPLATE.encode())
    assert len(entries) == 2 and not notes
    assert entries[1]["price"] == 4.5 and entries[1]["unit"] == "each"


@pytest.mark.parametrize("prompt", ["", "show best price", "0 chairs", "x" * 4001])
def test_unusable_requirements_explain_what_to_fix(prompt):
    with pytest.raises(ValueError):
        parse_requirements(prompt)
