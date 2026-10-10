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


@pytest.mark.parametrize("source, company", [
    ("Acme/Documents/doc.pdf", "Acme"), ("Archive/Acme/doc.pdf", "Acme"),
    ("Vendor Details/Acme/Prices/list.xlsx", "Acme"), ("Documents/doc.pdf", ""),
])
def test_existing_company_folders_are_identified(source, company):
    assert _company_folder_from_source_path(source) == company
