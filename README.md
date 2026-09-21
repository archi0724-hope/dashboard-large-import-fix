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
- Give the sales team a separate password with access to catalog search only.

## Sales catalog assistant

1. In **Upload documents**, import company-folder ZIPs from your computer or Google Drive. For loose files, specify the company in Optional settings. Files should be classified as **Catalogue** or **Price**; correct their assignment in **Review files** if needed.
2. Open **Catalog setup** to download a CSV price-list template and check extraction status. Replace the fictional template examples with real vendor data. Click **Refresh catalog search**, or let the first search index pending catalogs automatically.
3. Open **Sales assistant** and enter a requirement such as:

   ```text
   20 office chairs under INR 5000 each
   500 5ml syringes
   ```

4. Review matching vendors, specifications, listed prices, quantities, source locations and catalog upload dates. Download the comparison or open the original catalog evidence.

Each message starts a new search. Separate products with new lines or semicolons; quantity defaults to one piece. For packs, use `10 packs of 100 syringes`. This is local keyword/specification matching with common furniture and product synonyms; it needs no external AI key and does not send catalogs to an AI service.

### Catalog formats and price meaning

- **CSV / XLSX:** one product per row, with `Product`, `Price`, `Currency` and `Unit` columns for price comparison. Optional columns: `Specifications`, `Category`, `Brand`, `Model`, `Pack size`, `MOQ`, `Stock`, `Contact`, `Valid until`, `Terms`. The downloadable template shows the format. Stock and MOQ must use the stated selling unit; dates use `YYYY-MM-DD`.
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
