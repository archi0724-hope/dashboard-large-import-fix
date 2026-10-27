"""Read catalogue labels from saved source evidence without changing raw rows."""
from __future__ import annotations

import re
from functools import lru_cache


def normalized(value):
    return re.sub(r'[^a-z0-9]', '', str(value).casefold())


COMPANY_HEADERS = ('companyname', 'company', 'vendorname', 'vendor', 'suppliername',
                   'supplier', 'manufacturername', 'manufacturer', 'hospitalname', 'hospital')
# Prefer the actual sale/net price; retain MRP and other prices in the raw values.
PRICE_HEADERS = ('salesprice', 'sellingprice', 'netprice', 'offerprice', 'dealerprice',
                 'price', 'unitprice', 'rate', 'netrate', 'dealerrate', 'salerate',
                 'regularprice', 'mrp', 'maximumretailprice', 'amount')


def source_path(path):
    return '/'.join(part for part in path.replace('\\', '/').split('/')
                    if not re.fullmatch(r'upload-[a-f0-9]{8,}', part, re.IGNORECASE))


@lru_cache(maxsize=512)
def source_company(path):
    # Use the same folder ownership rules as the main company checklist.
    from vendor_dashboard.vendor_core import classify

    return classify(source_path(path)).company_name or ''


def catalogue_labels(record):
    values = record.get('values')
    fields = {normalized(key): str(value).strip() for key, value in values.items()
              if value is not None and str(value).strip()} if isinstance(values, dict) else {}
    company = str(record.get('company') or '').strip()
    if not company:
        company = next((fields[key] for key in COMPANY_HEADERS if key in fields), '')
    if not company:
        company = source_company(record.get('member_path') or record.get('source_name') or '')
    price = next((fields[key] for key in PRICE_HEADERS if key in fields),
                 str(record.get('price') or '').strip())
    return company, price
