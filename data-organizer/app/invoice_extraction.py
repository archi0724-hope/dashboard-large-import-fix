"""Bounded purchase-invoice extraction. Values are drafts for human review."""
from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

ALLOWED = {'.pdf','.jpg','.jpeg','.png','.xlsx','.xls','.csv'}
MAX_BYTES = 20 * 1024**2
MAX_ROWS = 10000


def normalized(value):
    return re.sub(r'[^a-z0-9]','',str(value).casefold())


def parse_text(text):
    fields = {'invoice_number':'','invoice_date':'','supplier':'','gstin':'','total':'','currency':''}
    patterns = {
        'invoice_number':r'(?im)^\s*(?:purchase\s+)?invoice\s*(?:no\.?|number|#)\s*[:#-]?\s*([^\n]+)',
        'invoice_date':r'(?im)^\s*(?:invoice\s+)?date\s*[:#-]\s*([^\n]+)',
        'supplier':r'(?im)^\s*(?:supplier|vendor|seller)(?:\s+name)?\s*[:#-]\s*([^\n]+)',
        'gstin':r'(?im)^\s*(?:supplier\s+)?GSTIN\s*[:#-]\s*([A-Z0-9]{15})',
        'total':r'(?im)^\s*(?:grand\s+total|invoice\s+total|total\s+amount|net\s+payable)\s*[:#-]?\s*(?:INR|Rs\.?|₹)?\s*([0-9][0-9,]*(?:\.\d{1,2})?)\s*$',
    }
    for key,pattern in patterns.items():
        match = re.search(pattern,text)
        if match:
            fields[key] = match[1].strip()[:200]
    if fields['total']:
        fields['total'] = fields['total'].replace(',','')
    if re.search(r'\bINR\b|₹',text):
        fields['currency'] = 'INR'
    return fields


def spreadsheet(path):
    if path.suffix.lower() == '.xlsx':
        from .drive.xlsx_stream import sheets
        yield from sheets(path)
    elif path.suffix.lower() == '.xls':
        import xlrd
        book = xlrd.open_workbook(path,on_demand=True)
        try:
            for sheet in book.sheets():
                yield sheet.name,({'row':n+1,'values':sheet.row_values(n)} for n in range(sheet.nrows))
        finally:
            book.release_resources()
    else:
        with path.open(encoding='utf-8-sig',errors='replace',newline='') as source:
            yield 'CSV',({'row':n,'values':row} for n,row in enumerate(csv.reader(source),1))


def extract_sheet(path):
    fields = parse_text('')
    lines,items = [],[]
    aliases = {
        'invoice_number':{'invoiceno','invoicenumber','invoicenum','billno'},
        'invoice_date':{'invoicedate','date','billdate'},
        'supplier':{'supplier','suppliername','vendor','vendorname','seller','sellername'},
        'gstin':{'gstin','suppliergstin','vendorgstin'},
        'total':{'grandtotal','invoicetotal','totalamount','netpayable'},
        'currency':{'currency'},
    }
    item_aliases = {'description':{'description','product','productname','item','itemname','particulars'},
                    'quantity':{'qty','quantity'},'unit_price':{'rate','unitprice','price'},
                    'amount':{'amount','linetotal','total'}}
    count = 0
    text_size = 0
    for name,rows in spreadsheet(path):
        headers = None
        field_headers = None
        for row in rows:
            count += 1
            if count > MAX_ROWS:
                raise ValueError('Invoice spreadsheets support up to 10,000 rows. Split this workbook into individual invoices.')
            values = [str(value).strip() if value is not None else '' for value in row['values']]
            if len(values) > 100:
                raise ValueError('Invoice sheets support up to 100 columns.')
            if not any(values):
                continue
            if text_size < 50000:
                line = ' | '.join(values)[:4000]
                lines.append(line)
                text_size += len(line)
            keys = [normalized(value.rstrip(':')) for value in values]
            if field_headers:
                for field,index in field_headers.items():
                    if index < len(values) and not fields[field]:
                        fields[field] = values[index][:200]
                field_headers = None
            recognized = {field:index for index,key in enumerate(keys) for field,choices in aliases.items() if key in choices}
            if len(recognized) >= 2:
                field_headers = recognized
            all_labels = set().union(*aliases.values())
            for index,key in enumerate(keys[:-1]):
                for field,choices in aliases.items():
                    if key in choices and keys[index+1] not in all_labels and values[index+1] and not fields[field]:
                        fields[field] = values[index+1][:200]
            if headers is None:
                found = {field:next((i for i,key in enumerate(keys) if key in choices),None)
                         for field,choices in item_aliases.items()}
                if found['description'] is not None and any(found[key] is not None for key in ('quantity','unit_price','amount')):
                    headers = found
                continue
            item = {field:values[index] if index is not None and index < len(values) else '' for field,index in headers.items()}
            if item['description'] and normalized(item['description']) not in {'subtotal','total','grandtotal','cgst','sgst','igst'}:
                items.append({**item,'source_sheet':name,'source_row':row['row']})
    text = '\n'.join(lines)[:50000]
    fallback = parse_text(text)
    fields = {key:value or fallback[key] for key,value in fields.items()}
    fields['total'] = fields['total'].replace(',','')
    return {'fields':fields,'items':items,'raw_text':text,'warnings':['Review all extracted values against the original before saving.']}


def image_text(image, work):
    executable = shutil.which('tesseract')
    if not executable and sys.platform == 'win32':
        from .ingestion.ocr_reader import resolve_tesseract
        executable = resolve_tesseract()
    if not executable:
        raise ValueError('Image text extraction needs Tesseract OCR on the server.')
    image.thumbnail((2400,2400))
    target = work / 'ocr.png'
    image.convert('L').save(target)
    result = subprocess.run([executable,str(target),'stdout','--psm','6'],check=False,capture_output=True,timeout=45,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
    if result.returncode:
        raise ValueError('OCR could not read this invoice. Try a clearer image or upload Excel.')
    return result.stdout.decode('utf-8',errors='replace')


def extract(path):
    ext = path.suffix.lower()
    if ext not in ALLOWED or path.stat().st_size > MAX_BYTES:
        raise ValueError('Choose a PDF, JPG, PNG, XLSX, XLS or CSV invoice up to 20 MB.')
    if ext in {'.xlsx','.xls','.csv'}:
        return extract_sheet(path)
    text_parts = []
    with tempfile.TemporaryDirectory(prefix='invoice-ocr-') as folder:
        work = Path(folder)
        if ext == '.pdf':
            import pypdfium2 as pdfium
            with pdfium.PdfDocument(str(path)) as document:
                if len(document) > 20:
                    raise ValueError('Upload invoices of up to 20 pages; split longer documents.')
                for page_number in range(len(document)):
                    with closing(document[page_number]) as page:
                        with closing(page.get_textpage()) as textpage:
                            text = textpage.get_text_range()
                        if not text.strip():
                            width,height = page.get_size()
                            scale = min(2,2400/max(width,height))
                            bitmap = page.render(scale=scale)
                            try:
                                with bitmap.to_pil() as image:
                                    text = image_text(image,work)
                            finally:
                                bitmap.close()
                        text_parts.append(text)
                        if sum(map(len,text_parts)) > 50000:
                            raise ValueError('This invoice has too much text for one review. Split it into smaller invoices.')
        else:
            from PIL import Image
            with Image.open(path) as image:
                if image.width * image.height > 25_000_000:
                    raise ValueError('Invoice image exceeds 25 megapixels. Resize it before uploading.')
                text_parts.append(image_text(image,work))
    text = '\n'.join(text_parts)
    if not text.strip():
        raise ValueError('No readable text found. Upload a clearer scan or Excel file.')
    return {'fields':parse_text(text),'items':[],'raw_text':text[:50000],
            'warnings':['Review extracted text and enter or correct line items before saving.']}


if __name__ == '__main__':
    try:
        print(json.dumps({'ok':True,'draft':extract(Path(sys.argv[1]))}))
    except Exception as error:  # noqa: BLE001 - return a sanitized worker error, never a traceback with document data
        print(json.dumps({'ok':False,'error':str(error) if isinstance(error,ValueError) else 'Extraction failed: '+type(error).__name__}))
