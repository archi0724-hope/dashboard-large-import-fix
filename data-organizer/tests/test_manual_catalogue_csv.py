import csv
from io import StringIO

import pytest

from tests.test_vendor_manual_catalogue_merge import catalogue_page


def content(delimiter=',', encoding='utf-8-sig'):
    text = StringIO(newline='')
    writer = csv.writer(text, delimiter=delimiter)
    writer.writerows([
        ['Company Name', 'Product Name', 'SKU', 'Price', 'Currency'],
        ['Alpha Medical', 'Wheelchair', 'W1', '5,000', 'INR'],
        ['Beta Medical', 'Surgical forceps, stainless steel', 'F1', '€4,000 + GST' if encoding == 'cp1252' else '₹4,000 + GST', 'INR'],
        ['Alpha Medical', 'Amoxicillin capsules', 'D1', '25', 'INR'],
    ])
    return text.getvalue().encode(encoding)


@pytest.mark.parametrize(('delimiter','encoding'), [(',', 'utf-8-sig'), (';', 'utf-16'), ('\t', 'cp1252')])
def test_csv_import_keeps_review_rows_original_bytes_and_shared_excel(tmp_path, delimiter, encoding):
    from exports import catalogue_price_workbook_bytes, read_manual_catalogue_workbook
    from storage import Store

    source = content(delimiter, encoding)
    approved, review, excluded = read_manual_catalogue_workbook(source, 'vendor-export.CSV')
    assert len(approved) == len(review) == excluded == 1
    assert review.iloc[0].product_name == 'Surgical forceps, stainless steel'
    store = Store(tmp_path)
    result = store.import_catalogue_workbook('vendor-export.CSV', source)
    assert result == {'added':1,'review':1,'excluded':1,'duplicates':0}
    again = store.import_catalogue_workbook('renamed.csv', source)
    assert again['added'] == again['review'] == 0 and again['duplicates'] == 2
    assert store.read_bytes(int(store.documents().iloc[0].id)) == source
    records = store.catalogue_export_records()
    assert len(records) == 2
    expected = '€4,000 + GST' if encoding == 'cp1252' else '₹4,000 + GST'
    assert records.loc[records.catalogue_status == 'review','source_price_text'].iloc[0] == expected
    assert catalogue_price_workbook_bytes(records, include_review_details=True).startswith(b'PK')


def test_csv_requires_company_and_product_headers_instead_of_inventing_a_company():
    from exports import read_manual_catalogue_workbook

    with pytest.raises(ValueError, match='Include Company Name and Product Name'):
        read_manual_catalogue_workbook(b'Product,Price\nWheelchair,100\n', 'prices.csv')


def test_csv_uses_existing_row_limit(monkeypatch):
    import exports

    monkeypatch.setattr(exports, 'MAX_MANUAL_CATALOGUE_ROWS', 1)
    with pytest.raises(ValueError, match='200,000 scanned product-row limit'):
        exports.read_manual_catalogue_workbook(content(), 'prices.csv')


def test_csv_upload_widget_accepts_and_saves_a_csv(tmp_path, monkeypatch):
    from storage import Store

    page = catalogue_page(tmp_path, monkeypatch)
    page.file_uploader(key='catalogue_manual_workbook').set_value(('export.csv', content(), 'text/csv')).run()
    assert not page.exception and not page.error
    assert page.session_state['manual_catalogue_import_result']['added'] == 1
    assert Store(tmp_path).product_record_count(status='all') == 2
