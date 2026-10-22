# Data Organizer + HospKart BD Chatbot

Run from this directory:

```powershell
python -m pip install -r requirements.txt
python -m app serve --host 127.0.0.1 --port 8000
```

In Vendor → **Catalogue & prices**, use **Upload manual catalogue & prices (.xlsx)** once. Validated medical non-drug rows automatically join the saved catalogue, product search, Vendor AI Assistant retrieval and the existing **Export company-wise catalogue & prices — Excel** download. There is one combined table and export; do not re-upload the generated workbook. Identical workbook contents are skipped on re-upload, including renamed files. Different workbooks append records and do not overwrite existing prices. Invalid/unclear rows go to **Review files** and medicine/drug rows stay excluded. The original workbook is retained with the document files for backup/restore; no separate merged workbook is stored. Each workbook retains the 2 GB/200,000 scanned-row limits. Valid multi-price cells become one catalogue row per price. A failed database import rolls back that workbook's database changes; retry using **Retry catalogue import**.

Manual imports normalize Excel date/time cells and blank optional fields before saving. After an import-code update, refreshing the page reloads outdated cached storage code and retries a selected upload processed by an older version. Other save failures display the exception type; full details are recorded in the server log.

Open <http://127.0.0.1:8000/>. The workspace switcher provides Organizer,
Vendor. The original **QuoteSarthi AI** interface is embedded inside **Vendor AI Assistant** in the Vendor sidebar, including its welcome screen, chat controls, response modes, and export buttons.
Open `/vendor/?assistant=1` directly. Old `/chatbot/` links redirect to this assistant.
The chatbot API is `POST /chatbot/api/chat`, with a JSON body such as
`{"query":"show hospital beds"}`. Its health endpoint is `/chatbot/health`.

HospKart chat reads the same database as the Vendor dashboard, using the
dashboard's `VENDOR_DATA_DIR` and `DATABASE_URL` settings (including Streamlit
secrets). New imports and company corrections are picked up on subsequent
queries (cloud edits refresh within 30 seconds). Answers use uploaded dashboard
catalogue records by default. Vendor AI Assistant displays uploaded-document and
searchable-record counts. This is live data retrieval, with no model retraining.

After Organizer reads files, choose **Create folders & sync to Vendor** (or
**Organize into company folders** on the Source page). Organizer retains each
original unchanged in its company folder, then copies it into the Vendor
dashboard's company document store and extracts supported catalogue/price rows.
The sync runs in the background, reports added, duplicate, and skipped files,
and is safe to repeat. Vendor storage uses the same `VENDOR_DATA_DIR` and
`DATABASE_URL` configuration as the Vendor dashboard and QuoteSarthi.

Download **Organized Vendor Data.zip** for the company-wise original documents.
Download the original source-based **Company Products and Prices.xlsx** from
Organizer, or open Vendor → **Catalogue & prices** to export the newly synced
catalogue and price rows as Excel. Vendor → **Companies & documents** provides
company-scoped original-document ZIP downloads. Missing prices are not inferred.

Document and checklist answers include original filenames. QuoteSarthi searches
readable PDF, DOCX, XLSX, XLS, CSV, TXT, and LOG content from saved company
documents, using bounded text extraction; scanned image-only content may require
OCR. Classification does not verify certificate validity. Cached product
selections are cleared when the source data changes.

To also include a separate reference catalogue, explicitly set
`HOSPKART_INCLUDE_REFERENCE_CATALOGUE=true`. Its default path is
`hospkart_chat/data/hospkart_products.csv`; `HOSPKART_PRODUCT_CSV` can override it.
Reference catalogue files are excluded from source control.

Search uses keyword matching over product names, SKUs, categories and vendors,
then the existing HospKart offer-comparison logic. It ranks up to 3,000 candidate
records per query. This integration does not download embedding models or build
a second vector database. `/chatbot/debug/config` reports source counts.

Products without a usable recorded INR price are listed separately. They cannot
be used for quotations until a price is recorded. Imported stock quantities and
warranties are shown as not recorded. Quotation exports retain source references.
Existing HospKart GST and discount behavior is preserved.

Vendor browser sessions keep separate conversation state and quotation downloads.
Context expires after an hour of inactivity; restarting the app also clears context. Exports
are saved under `hospkart_chat/exports/<session>/`. Session state is held in this
single process; run one worker for this local setup.

Ollama formatting is optional and disabled by default. To enable it, set
`HOSPKART_USE_OLLAMA=true` and `OLLAMA_MODEL` before starting the app. The core
catalogue and quotation functions work without Ollama.

To verify the integration with synthetic data:

```powershell
python -m pytest tests/test_hospkart_integration.py tests/test_vendor_product_search.py tests/test_api_export.py -q
```
