"""Purchase invoice register in the team's requested 31-column format."""
from datetime import date
from decimal import Decimal, InvalidOperation

import xlsxwriter


HEADERS = (
    'Invoice Date', 'Invoice No.', 'Client / Hospital', 'Delivery City / Location',
    'Full Delivery / Billing Address', 'Client Phone', 'Phone Source', 'Item Name',
    'Category', 'Subcategory', 'Brand', 'HSN/SAC', 'GST Rate', 'Quantity', 'Unit',
    'Unit Rate (₹)', 'Taxable Amount (₹)', 'CGST (₹)', 'SGST (₹)',
    'Item Total incl. GST (₹)', 'Invoice Total (₹)', 'Invoice Status', 'Call Status',
    'Client Feedback', 'Product Quality', 'Delivery Experience', 'Would Order Again?',
    'Next Requirement', 'Follow-up Date', 'Notes', 'Source PDF Page',
)
MAX_EXPORT_ROWS = 100_000


class ExportLimitExceeded(ValueError):
    pass


def numeric(value):
    """Keep missing amounts empty, including values whose currency is unknown."""
    if value in ('', None):
        return ''
    try:
        result = Decimal(str(value).replace(',', ''))
        if result.is_finite() and abs(result) <= Decimal('1e15'):
            return float(result)
    except InvalidOperation:
        pass
    return str(value)


def register_rows(record):
    fields = record['fields']
    currency = fields.get('currency', '').strip().upper()
    is_inr = currency in {'INR', '₹', 'RS', 'RS.'}
    for item in record.get('items') or [{}]:
        row = dict.fromkeys(HEADERS, '')
        row['Invoice Date'] = fields.get('invoice_date', '')
        row['Invoice No.'] = fields.get('invoice_number', '')
        row['Item Name'] = item.get('description', '')
        row['Quantity'] = numeric(item.get('quantity'))
        # No client, tax basis, phone, page number or payment status is inferred.
        notes = ['Source: ' + record.get('filename', '')]
        if fields.get('supplier'):
            notes.append('Supplier: ' + fields['supplier'])
        if is_inr:
            row['Unit Rate (₹)'] = numeric(item.get('unit_price'))
            row['Invoice Total (₹)'] = numeric(fields.get('total'))
        else:
            notes.append('Currency: ' + (currency or 'not recorded; amounts not placed in ₹ columns'))
            if item.get('unit_price'):
                notes.append('Recorded unit rate: ' + str(item['unit_price']))
            if fields.get('total'):
                notes.append('Recorded invoice total: ' + str(fields['total']))
        if item.get('amount'):
            notes.append('Recorded line amount (tax basis unspecified): ' + str(item['amount']))
        row['Notes'] = '; '.join(notes)
        yield row


def write_invoice_workbook(path, records):
    """Write to temporary disk, with literal strings and a bounded row count."""
    with xlsxwriter.Workbook(str(path), {
        'constant_memory': True, 'strings_to_formulas': False, 'strings_to_urls': False,
    }) as book:
        sheet = book.add_worksheet('Purchase Invoices')
        heading = book.add_format({'bold': True, 'font_color': 'white', 'bg_color': '#17294F',
                                   'text_wrap': True, 'valign': 'vcenter', 'border': 1})
        text = book.add_format({'valign': 'top', 'text_wrap': True})
        money = book.add_format({'num_format': '#,##0.00', 'valign': 'top'})
        quantity = book.add_format({'num_format': '0.########', 'valign': 'top'})
        dates = book.add_format({'num_format': 'yyyy-mm-dd', 'valign': 'top'})
        sheet.write_row(0, 0, HEADERS, heading)
        sheet.set_row(0, 48)
        sheet.set_column(0, len(HEADERS)-1, 20)
        sheet.set_column(2, 4, 32)
        sheet.set_column(7, 7, 40)
        sheet.set_column(29, 29, 65)
        sheet.freeze_panes(1, 3)
        row_number = 0
        for record in records:
            for values in register_rows(record):
                row_number += 1
                if row_number > MAX_EXPORT_ROWS:
                    raise ExportLimitExceeded('Excel export supports up to 100,000 item rows. Narrow your invoice search and export again.')
                for column, name in enumerate(HEADERS):
                    value = values[name]
                    style = text
                    if name == 'Invoice Date' and value:
                        try:
                            value = date.fromisoformat(value)
                            style = dates
                        except ValueError:
                            pass  # Preserve reviewed date text in other formats.
                    elif name == 'Quantity':
                        style = quantity
                    elif '(₹)' in name:
                        style = money
                    sheet.write(row_number, column, value, style)
        sheet.autofilter(0, 0, row_number, len(HEADERS)-1)
        guide = book.add_worksheet('Export guide')
        guide.set_column(0, 0, 110)
        for index, instruction in enumerate([
            'One row per saved invoice item. Invoices without items still receive one row.',
            'Invoice totals repeat on each item row. Count each invoice once when totalling invoices.',
            'Client / Hospital is not the supplier. Client, delivery, phone, tax, follow-up and PDF-page fields are blank when not captured.',
            'Complete blank tracking columns in Excel. Excel edits are not synced back to the dashboard.',
            'Only confirmed INR amounts appear in ₹ columns. Other or unknown currency amounts are retained in Notes without conversion.',
            'Legacy line Amount has no recorded tax basis. It is retained in Notes, not treated as taxable or GST-inclusive.',
            'Invoice Status is blank: saving a reviewed record does not prove payment, delivery or cancellation status.',
        ]):
            guide.write(index, 0, instruction, text)
            guide.set_row(index, 36)
