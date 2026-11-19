from app.drive.indexer import extracted_records


def test_drive_images_ocr_into_searchable_unverified_text(tmp_path, monkeypatch):
    import pytesseract
    from PIL import Image
    from app.ingestion import ocr_reader
    monkeypatch.setattr(ocr_reader, 'resolve_tesseract', lambda: 'synthetic-tesseract')
    monkeypatch.setattr(pytesseract, 'image_to_string', lambda *args, **kwargs: 'DriveScanNeedle surgical pump')
    path = tmp_path/'scan.png'
    Image.new('RGB', (80, 80), 'white').save(path)
    rows = list(extracted_records(path, 'Synthetic Medical'))
    assert rows[0]['kind'] == 'document'
    assert rows[0]['extraction_method'] == 'ocr'
    assert 'DriveScanNeedle' in rows[0]['text']


def test_drive_scan_missing_engine_remains_explicit_issue(tmp_path, monkeypatch):
    from app.ingestion import ocr_reader
    monkeypatch.setattr(ocr_reader, 'resolve_tesseract', lambda: '')
    path = tmp_path/'scan.png'
    path.write_bytes(b'synthetic')
    rows = list(extracted_records(path))
    assert rows[0]['kind'] == 'needs_ocr'
    assert 'Tesseract is unavailable' in rows[0]['text']


def test_drive_pdf_scan_uses_rendered_page(tmp_path, monkeypatch):
    import pytesseract
    from reportlab.pdfgen.canvas import Canvas
    from app.ingestion import ocr_reader
    monkeypatch.setattr(ocr_reader, 'resolve_tesseract', lambda: 'synthetic-tesseract')
    monkeypatch.setattr(pytesseract, 'image_to_string', lambda *args, **kwargs: 'PDFScanNeedle')
    path = tmp_path/'scan.pdf'
    canvas = Canvas(str(path))
    canvas.showPage()
    canvas.save()
    rows = list(extracted_records(path))
    assert rows[0]['kind'] == 'document'
    assert rows[0]['extraction_method'] == 'ocr'
    assert rows[0]['text'] == 'PDFScanNeedle'
