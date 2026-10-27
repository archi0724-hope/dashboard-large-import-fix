import json

from app.drive.catalogue_fields import catalogue_labels
from app.drive.indexer import table_records


def test_saved_polar_rows_get_company_and_sales_price_without_changing_source():
    record = {'kind': 'product', 'company': '', 'price': '',
              'member_path': 'organized_by_company/Polar Automation/POLAR AUTOMATION PRICE LIST.csv',
              'values': {'Product Name': 'Linear actuator', 'Regular Price': '14080', 'Sales Price': '12800'}}
    before = json.dumps(record, sort_keys=True)
    assert catalogue_labels(record) == ('Polar Automation', '12800')
    assert json.dumps(record, sort_keys=True) == before


def test_company_folder_matches_main_checklist_and_ignores_upload_wrapper():
    record = {'member_path': 'organized_by_company/upload-d164ceacc2b0/A.S.F. UNIVERSAL LLP/ASF PRICE LIST.xlsx',
              'values': {'PRODUCT NAME': 'Slit Lamp SL20i', 'PRICE': '75000/-'}}
    assert catalogue_labels(record) == ('A.S.F. UNIVERSAL LLP', '75000/-')


def test_explicit_row_company_wins_and_unknown_source_is_not_invented():
    assert catalogue_labels({'company': 'Beta Medical', 'member_path': 'Alpha Medical/catalogue.csv',
                             'price': '0'}) == ('Beta Medical', '0')
    assert catalogue_labels({'source_name': 'catalogue.csv'}) == ('', '')
    assert catalogue_labels({'values': {'Vendor Name': 'Beta Medical', 'Net Price': 0}}) == ('Beta Medical', '0')


def test_new_extraction_prefers_sale_over_mrp_independent_of_column_order():
    record, = table_records([['Product Name', 'MRP', 'Regular Price', 'Sales Price'],
                             ['Linear actuator', '15000', '14080', '12800']])
    assert record['kind'] == 'product' and record['price'] == '12800'
    assert record['values']['MRP'] == '15000'
