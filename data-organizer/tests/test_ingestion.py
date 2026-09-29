import json
from pathlib import Path

import pytest

from app.config import RuntimeConfig
from app.ingestion.base import FileReadError, SourceFile
from app.ingestion.discovery import scan_local_folder
from app.ingestion.encoding import detect_delimiter, detect_encoding
from app.ingestion.extractor import FileExtractor


def extract(settings, cfg, demo_dir, rel):
    sf = next(f for f in scan_local_folder(demo_dir) if f.path == rel)
    ex = FileExtractor(settings, cfg)
    recs = [r for b in ex.extract(sf) for r in b]
    return recs, ex.report


def names(recs):
    return [r.original_name for r in recs if r.original_name]


def test_discovery_is_recursive_and_lists_unsupported(demo_dir):
    files = {f.path: f for f in scan_local_folder(demo_dir)}
    assert "regional/kota_hospitals.xlsx" in files
    assert files["readme_notes.md"].supported is False and files["companies.csv"].supported is True


def test_excel_title_rows_merged_cells_hidden_sheet(settings, cfg, demo_dir):
    recs, rep = extract(settings, cfg, demo_dir, "hospitals_rajasthan.xlsx")
    assert rep.sheets == 3
    main = [b for b in rep.blocks if b.sheet == "Hospitals"][0]
    assert main.header_row == 4 and main.preamble                       # title rows sit above the header
    assert "Old Town Dispensary" in names(recs)                          # hidden sheet is read
    marudhar = [r for r in recs if r.original_name == "Marudhar Multi Speciality Hospital"][0]
    assert marudhar.original_city == "Jodhpur"                           # merged 'Town' cell filled down
    assert any(r.original_name == "Total" for r in recs)                 # nothing silently dropped
    assert json.loads(marudhar.original_data)["Contact No."]             # original columns preserved


def test_different_headers_map_to_same_schema(settings, cfg, demo_dir):
    recs, _ = extract(settings, cfg, demo_dir, "hospitals_rajasthan.xlsx")
    clinics = [r for r in recs if r.source_sheet == "Clinics"]
    assert clinics[0].original_name == "Sawai Man Singh Hospital"        # "Name of Institution"
    assert "Contact Person" in clinics[0].original_data and "Dr. R. Sharma" not in (clinics[0].original_name or "")


def test_legacy_xls(settings, cfg, demo_dir):
    recs, _ = extract(settings, cfg, demo_dir, "legacy_directory.xls")
    assert len(recs) == 4 and recs[0].original_state == "Haryana"


def test_semicolon_cp1252_csv(settings, cfg, demo_dir):
    recs, rep = extract(settings, cfg, demo_dir, "companies.csv")
    assert rep.meta["delimiter"] == ";" and rep.meta["encoding"].lower() in ("cp1252", "windows-1252")
    assert "Café Médical Supplies Co." in names(recs)


def test_pdf_header_carried_to_continuation_page(settings, cfg, demo_dir):
    recs, rep = extract(settings, cfg, demo_dir, "empanelled_hospitals.pdf")
    assert rep.pages == 3
    got = names(recs)
    assert "Western Star Hospital" in got and "Marudhar Multispeciality Hospital" in got   # page 2 has no header row
    assert any(b.header_source == "carried_from_previous_table" for b in rep.blocks)
    western = [r for r in recs if r.original_name == "Western Star Hospital"][0]
    assert western.source_page == 2 and western.original_phone == "9876500161"


def test_docx_table_and_label_paragraphs(settings, cfg, demo_dir):
    recs, _ = extract(settings, cfg, demo_dir, "haryana_facilities.docx")
    got = names(recs)
    assert "Medicare Plus Hospital" in got and "Panipat Eye Care Hospital" in got and "Karnal Heart Institute" in got
    panipat = [r for r in recs if r.original_name == "Panipat Eye Care Hospital"][0]
    assert panipat.original_city == "Panipat" and panipat.original_phone == "9876500181"
    prose = [r for r in recs if r.original_name is None]
    assert prose and all(json.loads(r.original_data)["text_role"] != "entity" for r in prose)   # headings/prose kept, not names


def test_free_text_lines(settings, cfg, demo_dir):
    recs, _ = extract(settings, cfg, demo_dir, "field_notes.txt")
    sms = [r for r in recs if r.original_name and "SMS" in r.original_name][0]
    assert sms.original_city == "Jaipur" and sms.original_phone == "0141-2560101" and "smshospital" in sms.original_website
    aravali = [r for r in recs if r.original_name == "Aravali Care Hospital"][0]
    assert aravali.original_pincode == "302017" and aravali.original_phone == "9876500011"


def test_scanned_image_needs_ocr(settings, cfg, demo_dir):
    from app.ingestion.ocr_reader import ocr_available

    if not ocr_available():
        pytest.skip("tesseract not installed")
    recs, rep = extract(settings, cfg, demo_dir, "scanned_directory.png")
    assert "Eastern Star Hospital" in names(recs) and rep.blocks[0].method == "ocr_image"


def test_corrupt_file_raises_clean_error(settings, cfg, demo_dir):
    sf = next(f for f in scan_local_folder(demo_dir) if f.name == "corrupt_file.xlsx")
    with pytest.raises(FileReadError):
        list(FileExtractor(settings, cfg).extract(sf))


def test_record_ids_are_deterministic(settings, cfg, demo_dir):
    a, _ = extract(settings, cfg, demo_dir, "companies.csv")
    b, _ = extract(settings, cfg, demo_dir, "companies.csv")
    assert [r.record_id for r in a] == [r.record_id for r in b] and len({r.record_id for r in a}) == len(a)


def test_encoding_and_delimiter_detection(tmp_path):
    p = tmp_path / "x.txt"
    p.write_bytes("a;b;c\n1;Café;3\n".encode("cp1252"))
    assert detect_encoding(p)[0] == "cp1252"
    assert detect_delimiter("a;b;c\n1;2;3\n4;5;6") == ";"
    assert detect_delimiter("just some prose\nwith no table at all") is None
