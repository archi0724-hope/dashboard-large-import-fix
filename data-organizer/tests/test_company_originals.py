from dataclasses import replace
from pathlib import Path
import zipfile

import pytest

from app.config import RuntimeConfig
from app.ingestion.base import file_sha256
from app.ingestion.discovery import scan_local_folder
from app.ingestion.extractor import FileExtractor
from app.ingestion.ocr_reader import ocr_available
from app.services import AppState, _company_folder_from_source_path


@pytest.fixture
def state(settings, tmp_path):
    local = replace(settings, data_dir=tmp_path / "data", output_dir=tmp_path / "output", log_dir=tmp_path / "logs")
    local.ensure_dirs()
    value = AppState(local, db_path=":memory:")
    value.cfg = RuntimeConfig(local_input_dir=str(tmp_path / "input"), output_dir=str(tmp_path / "output"))
    yield value
    value.repo.close()


def register(state, path, key, source_path):
    state.repo.upsert_source_file(file_key=key, file_name=path.name, local_path=str(path),
                                 source_path=source_path, source_kind="local", ext=path.suffix,
                                 status="processed", size_bytes=path.stat().st_size)


@pytest.mark.parametrize("filename", ["hospitals_rajasthan.xlsx", "legacy_directory.xls", "empanelled_hospitals.pdf", "scanned_directory.png"])
def test_reading_and_company_copy_keep_original_bytes(state, settings, demo_dir, filename):
    if filename.endswith(".png") and not ocr_available(settings.tesseract_cmd):
        pytest.skip("Tesseract is required for real image reading")
    source = demo_dir / filename
    before = file_sha256(source)
    file = next(f for f in scan_local_folder(demo_dir) if f.name == filename)
    extractor = FileExtractor(settings, RuntimeConfig(ocr_enabled=True))
    records = [r for batch in extractor.extract(file) for r in batch]
    assert any(r.original_name for r in records)
    register(state, source, file.file_key, f"Acme/Documents/{filename}")
    result = state.organize_source_files()
    copied = Path(result["folder"]) / "Acme" / filename
    assert copied.is_file()
    assert file_sha256(copied) == before == file_sha256(source)


@pytest.mark.parametrize("extension", [".jpg", ".pdf"])
def test_jpg_and_scanned_pdf_are_read_without_conversion(settings, demo_dir, tmp_path, extension):
    if not ocr_available(settings.tesseract_cmd):
        pytest.skip("Tesseract is required for real scanned-file reading")
    from PIL import Image
    source = tmp_path / ("scan" + extension)
    with Image.open(demo_dir / "scanned_directory.png") as image:
        image.convert("RGB").save(source, resolution=150.0)
    before = file_sha256(source)
    file = next(scan_local_folder(tmp_path))
    extractor = FileExtractor(settings, RuntimeConfig(ocr_enabled=True))
    records = [r for batch in extractor.extract(file) for r in batch]
    assert any("Eastern Star" in (r.original_name or "") for r in records)
    assert file_sha256(source) == before


def test_same_name_and_size_keep_both_originals_and_zip_refreshes(state, tmp_path):
    first = tmp_path / "a" / "catalogue.pdf"
    second = tmp_path / "b" / "catalogue.pdf"
    first.parent.mkdir(); second.parent.mkdir()
    first.write_bytes(b"first original"); second.write_bytes(b"other original")
    assert first.stat().st_size == second.stat().st_size
    register(state, first, "first", "Acme/Documents/catalogue.pdf")
    initial = state.create_structured_zip()
    assert initial["files"] == 1
    register(state, second, "second", "Acme/Catalogue/catalogue.pdf")
    refreshed = state.create_structured_zip()
    assert refreshed["files"] == 2 and not refreshed.get("cached")
    copies = list((state.settings.data_dir / "organized_by_company" / "Acme").glob("*.pdf"))
    assert {p.read_bytes() for p in copies} == {first.read_bytes(), second.read_bytes()}
    assert state.organize_source_files()["files_copied"] == 0
    with zipfile.ZipFile(state.settings.output_dir / refreshed["file"]) as archive:
        assert {archive.read(name) for name in archive.namelist()} == {first.read_bytes(), second.read_bytes()}


def test_organized_originals_and_catalogue_sync_to_vendor(tmp_path):
    from hospkart_chat.integration import open_vendor_store
    from app.vendor_sync import sync_organized_company_files
    from io import BytesIO
    import zipfile
    from openpyxl import load_workbook

    organized = tmp_path / "organized_by_company"
    certificate = organized / "Acme Supplies" / "GST" / "GST certificate.txt"
    shared_certificate = organized / "Beta Medical" / "GST" / "GST certificate.txt"
    catalogue = organized / "Acme Supplies" / "Catalogue" / "Acme price list.csv"
    unmatched = organized / "Work Related Data" / "Unmatched" / "scan.bin"
    certificate.parent.mkdir(parents=True)
    shared_certificate.parent.mkdir(parents=True)
    catalogue.parent.mkdir(parents=True)
    unmatched.parent.mkdir(parents=True)
    certificate_bytes = b"Original GST certificate; do not convert."
    catalogue_bytes = b"Product,Code,Price,Specification\nZX Glove,ZX123,125,Sterile latex examination gloves\n"
    certificate.write_bytes(certificate_bytes)
    shared_certificate.write_bytes(certificate_bytes)
    catalogue.write_bytes(catalogue_bytes)
    unmatched.write_bytes(b"Unmatched original bytes.")

    store = open_vendor_store(str(tmp_path / "vendor"), "")
    from exports import catalogue_price_workbook_bytes

    result = sync_organized_company_files(organized, store)

    assert result["documents_added"] == 4
    assert result["products_added"] == 1
    assert result["unassigned_documents_added"] == 1
    assert result["skipped_count"] == 0
    documents = store.documents()
    original = documents.loc[documents.filename == certificate.name].iloc[0]
    assert store.read_bytes(int(original.id)) == certificate_bytes
    assert original.company_name == "Acme Supplies"
    assert set(documents.loc[documents.filename == certificate.name, "company_name"]) == {
        "Acme Supplies", "Beta Medical",
    }
    unassigned = documents.loc[documents.filename == unmatched.name].iloc[0]
    assert unassigned.company_name == ""
    assert store.read_bytes(int(unassigned.id)) == unmatched.read_bytes()

    company_archive = store.company_zip("Acme Supplies", documents)
    with zipfile.ZipFile(BytesIO(company_archive)) as archive:
        assert certificate_bytes in [archive.read(name) for name in archive.namelist()]

    workbook_bytes = catalogue_price_workbook_bytes(store.product_records())
    workbook = load_workbook(BytesIO(workbook_bytes), data_only=True)
    rows = list(workbook["All catalogue & prices"].values)
    assert any("ZX Glove" in row for row in rows)
    workbook.close()

    repeated = sync_organized_company_files(organized, store)
    assert repeated["documents_added"] == 0
    assert repeated["duplicate_documents"] == 4
    assert repeated["products_added"] == 0


def test_vendor_sync_runs_in_background_and_reports_completion(state, tmp_path, monkeypatch):
    import time

    from hospkart_chat import integration

    store = integration.open_vendor_store(str(tmp_path / "vendor"), "")
    monkeypatch.setattr(integration, "vendor_setting", lambda name, default="": default)
    monkeypatch.setattr(integration, "open_vendor_store", lambda data_dir, database_url: store)

    organized = tmp_path / "organized"
    source = organized / "Acme" / "GST" / "gst.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"Original GST record.")

    started = state.start_vendor_sync(organized)
    assert started["running"] or started["result"] is not None
    deadline = time.monotonic() + 5
    while state.vendor_sync_status()["running"] and time.monotonic() < deadline:
        time.sleep(0.01)

    status = state.vendor_sync_status()
    assert not status["running"]
    assert not status["error"]
    assert status["result"]["documents_added"] == 1


@pytest.mark.parametrize("source, company", [
    ("Acme/Documents/doc.pdf", "Acme"), ("Archive/Acme/doc.pdf", "Acme"),
    ("Vendor Details/Acme/Prices/list.xlsx", "Acme"), ("Documents/doc.pdf", ""),
])
def test_existing_company_folders_are_identified(source, company):
    assert _company_folder_from_source_path(source) == company
