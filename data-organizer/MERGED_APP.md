# Data Organizer + HospKart BD Chatbot

Run from this directory:

```powershell
python -m pip install -r requirements.txt
python -m app serve --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/>. The workspace switcher provides Organizer,
Vendor. The original **QuoteSarthi AI** interface is embedded inside **Vendor AI Assistant** in the Vendor sidebar, including its welcome screen, chat controls, response modes, and export buttons.
Open `/vendor/?assistant=1` directly. Old `/chatbot/` links redirect to this assistant.
The chatbot API is `POST /chatbot/api/chat`, with a JSON body such as
`{"query":"show hospital beds"}`. Its health endpoint is `/chatbot/health`.

HospKart chat reads the same database as the Vendor dashboard, using the
dashboard's `VENDOR_DATA_DIR` and `DATABASE_URL` settings (including Streamlit
secrets). New imports and company corrections are picked up on subsequent
queries (cloud edits refresh within 30 seconds). It also searches the copied HospKart catalogue in
`hospkart_chat/data/hospkart_products.csv`. The source project is preserved.
The copied catalogue is local data and is excluded from source control; include
it when moving the app, or set `HOSPKART_PRODUCT_CSV` to another catalogue.

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
