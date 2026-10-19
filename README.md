# Vendor Document Dashboard

A Streamlit dashboard for importing vendor documents, searching vendor catalogs in a sales chat, comparing product prices, and managing document checklists.

## Features

- Import vendor documents from a local ZIP file or a public Google Drive ZIP link.
- Validate ZIP size, file count, nesting, compression ratio, and file types before import.
- Detect vendors and document categories automatically.
- Review Yes/No checklist status and document-type counts.
- Export checklist data as CSV or Excel workbooks.
- Store local data in SQLite/filesystem or use PostgreSQL for durable hosted storage.
- Search catalog products by requirement, quantity, specifications and budget.
- Compare vendor prices with source references, stock/MOQ checks and CSV downloads.
- Download a formatted Hospkart Excel RFQ with vendor alternatives, GST/HSN, availability, client details and delivery/payment terms.
- Give the sales team a separate password with access to catalog search only.

## Sales catalog assistant

1. In **Upload documents**, import company-folder ZIPs from your computer or Google Drive. For loose files, specify the company in Optional settings. Files should be classified as **Catalogue** or **Price**; correct their assignment in **Review files** if needed.
2. Open **Catalog setup** to download a CSV price-list template and check extraction status. Replace the fictional template examples with real vendor data. Click **Refresh catalog search**, or let the first search index pending catalogs automatically.
3. Open **Sales assistant** and enter a requirement such as:

   ```text
   20 office chairs under INR 5000 each
   500 5ml syringes
   ```

4. Review matching vendors, specifications, listed prices, quantities and **Availability**. Expand **Client and sales executive details (optional)** to enter the RFQ reference, hospital/client, contacts, delivery location and notes, then click **Download Excel RFQ**. CSV comparison and source catalog downloads remain available.

The Excel workbook contains **RFQ items**, **Vendor details** and **RFQ details**. It includes requested items even when no catalog matches them. Multiple vendors appear as alternatives for the same item; their prices are not added into a grand total. Item details include HSN, GST, selling unit, quantity, listed price, estimated amounts, stock/MOQ, lead time, validity, delivery/payment terms and warranty. Vendor details retain contacts, GSTIN and source references from sales-visible catalogs.

Availability distinguishes sufficient listed stock, partial stock, unavailable items, stock listed without a quantity, and unknown stock. This is **vendor catalog availability, not live Hospkart warehouse inventory**. A product appearing in a catalog does not by itself confirm stock. Blank numeric cells and `Not stated` mean information must be confirmed; they do not mean zero.

Each message starts a new search. Separate products with new lines or semicolons; quantity defaults to one piece. For packs, use `10 packs of 100 syringes`. This is local keyword/specification matching with common furniture and product synonyms; it needs no external AI key and does not send catalogs to an AI service.

### Catalog formats and price meaning

- **CSV / XLSX:** one product per row, with `Product`, `Price`, `Currency` and `Unit` columns for price comparison. Optional columns: `Specifications`, `Category`, `Brand`, `Model`/`Item code`, `Pack size`, `MOQ`, `Stock`, `Availability`, `HSN Code`, `GST (%)`, `Tax basis`, `Lead time`, `Delivery terms`, `Payment terms`, `Warranty`, `Contact`, `Email`, `Phone`, `Vendor GSTIN`, `Valid until`, `Terms`. The downloadable template shows the format. Stock and MOQ must use the stated selling unit; dates use `YYYY-MM-DD`.
- **GST / HSN:** enter the vendor's stated rate as `18` or `18%` for 18%; native Excel percentage cells are also supported. Set `Tax basis` to `Inclusive` or `Exclusive`. Store HSN/item codes and phone numbers as text to preserve leading zeros. Missing HSN/GST are never inferred from the product name. The example template leaves tax rates and HSN blank for you to fill from vendor records.
- **Excel estimates:** a valid, comparable price plus an explicit GST rate and tax basis produces the net amount, GST amount and amount including GST. Inclusive prices are split without adding tax twice. Unknown tax, ambiguous units, expired/conflicting listings, insufficient stock or MOQ conflicts leave the corresponding estimates blank. Freight, discounts and other charges are not calculated. The workbook is a snapshot; regenerate it after changing the search quantity.
- **PDF / DOCX / TXT:** searchable text excerpts with page/row references. An explicit single-line offer such as `Office chair | INR 2500 | each` also supplies a listed price. Ambiguous PDF tables remain quote-required evidence.
- Scans and image-only catalogs need an OCR/text version or a structured price list. Legacy `.doc` and `.xls` need conversion. The setup screen reports unsupported or unreadable files.
- Price comparisons require stated currency and selling unit. Bare `$` amounts do not establish a currency. A box/pack also requires its size. Currencies are not converted, and pack prices are not silently treated as piece prices.
- Lowest-price labels compare identical descriptions, specifications, brands/models, currency, unit and price terms across vendors. Conflicting specifications, expired prices, insufficient listed stock and MOQ conflicts are not recommended as cheapest. These are **listed prices**, not live stock checks or binding quotations; unknown tax/delivery charges are not added to estimates.
- Limits per file: 32 MB; PDFs up to 250 pages / 2 million text characters; structured files up to 20,000 rows and XLSX up to 30 sheets. Partial indexing is reported in Catalog setup.

Catalog search uses only available, vendor-assigned files whose classifications contain exclusively Catalogue and/or Price. Mixed catalog/KYC documents must be separated before exposing them to sales. The search index persists in the same database and follows current vendor assignments. Backups retain the original source files; the derived index rebuilds on the first search after restore. Reset deletes the index with the source data.

### Sales team access

Set distinct `APP_PASSWORD` (administrator) and `SALES_PASSWORD` (sales) secrets. Sales users can search, export comparisons and download catalog sources; they cannot navigate to uploads, KYC records, corrections, backups or reset controls.

Share `https://YOUR-DASHBOARD-ADDRESS/?view=sales` and the sales password through your normal private channel. A localhost address is accessible only on the machine running the app. See [DEPLOY.md](DEPLOY.md) for hosting and office-network access. The URL selects the screen; the password determines the role.

## Run Locally

Requires Python 3.12 or newer.

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501
```

Open <http://127.0.0.1:8501>.

To enable the local password gate, set `APP_PASSWORD` before starting Streamlit:

```powershell
$env:APP_PASSWORD = "use-a-long-local-password"
```

## Test

```powershell
python -m compileall -q app.py catalog_search.py catalog_ui.py drive_import.py exports.py import_service.py storage.py vendor_core.py
python -m pytest tests -q
```

## Deploy

See [DEPLOY.md](DEPLOY.md) for Windows and Streamlit Community Cloud deployment instructions.

For hosted deployment:

- Set `APP_PASSWORD` in the platform's secret settings.
- Set `DATABASE_URL` to a PostgreSQL connection string with `sslmode=require`.
- Use `app.py` as the Streamlit entry point.

## Data and Security

- Never commit vendor documents, ZIP uploads, exported workbooks, backups, or `.streamlit/secrets.toml`.
- Keep passwords and database credentials outside source control.
- Use PostgreSQL and provider backups for durable cloud records.
- Google Drive imports must use a link that permits downloading without an interactive sign-in.
- Test with synthetic documents before importing production data.
