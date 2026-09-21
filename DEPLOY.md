# Production deployment

## Streamlit Community Cloud

1. Create a private repository and copy the application files to its root.
2. Include `app.py`, `catalog_search.py`, `catalog_ui.py`, `vendor_core.py`, `storage.py`, `exports.py`, `import_service.py`, `drive_import.py`, `requirements.txt`, `.streamlit/config.toml`, and `CLOUD_DEPLOYMENT`.
3. Set the Streamlit entrypoint to `app.py`.
4. In Streamlit app settings, add the secrets from `.streamlit/secrets.toml.example`:
   - `APP_PASSWORD`: a long random team password.
   - `SALES_PASSWORD`: a different password for the sales team; this grants catalog search and downloads only.
   - `DATABASE_URL`: a PostgreSQL connection URL with `sslmode=require`.
5. Deploy and open the app in an incognito window to verify both passwords. An administrator sees the complete workspace; a sales login sees only the catalog assistant.
6. Import vendor catalogs/price lists and inspect **Catalog setup**. Share the deployed URL with `?view=sales` appended, for example `https://your-app.streamlit.app/?view=sales`.

`DATABASE_URL` is required for durable cloud storage. Streamlit cloud disk is temporary and must not be used for production records.

## Local Windows deployment

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:APP_PASSWORD = "use-a-long-local-password"
.\.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501
```

Open `http://127.0.0.1:8501`.

For access by colleagues on the same trusted office network, configure both passwords and run with `--server.address 0.0.0.0`. Share `http://YOUR-COMPUTER-LAN-IP:8501/?view=sales`; the computer must remain on and Windows Firewall must permit the port on that private network. For access outside the office, use an HTTPS hosted deployment rather than sharing a localhost address.

Use **Catalog setup** to review unsupported/scanned files before relying on search coverage. No vendor website crawler or live inventory feed is configured; searches use imported catalogs and price lists. Sources can be refreshed by importing updated files. Older offers for the same vendor/product/specifications remain visible with a superseded-listing flag and are excluded from recommendations. Conflicting listings uploaded at the same time require vendor confirmation. Provide validity dates and review renamed products or changed specifications when updating price lists.

## Verification

```powershell
python -m compileall -q app.py catalog_search.py catalog_ui.py drive_import.py exports.py import_service.py storage.py vendor_core.py
python -m pytest tests -q
Invoke-WebRequest http://127.0.0.1:8501
```

Before production use, upload a synthetic ZIP and verify: vendor headcount, Yes/No checklist, document-type counts, upload history, export downloads, backup/restore, and reset confirmation.

Also verify sales sign-in, multiple product requirements, price/currency/pack comparisons, source downloads and sales access restrictions. Backups contain the catalog source documents; the search index rebuilds after restore. No production vendor documents are included in automated tests or deployment bundles.

## Data rules

- Never commit `vendor_data`, `.streamlit/secrets.toml`, backups, vendor ZIPs, or exported workbooks.
- Keep `APP_PASSWORD` outside source control.
- Use PostgreSQL and provider backups for cloud durability.
- Large direct uploads are limited by Streamlit memory and the configured 2 GB request limit. For larger files, use the local ZIP path or a public Drive ZIP link.
- The application validates ZIP paths, entry counts, expanded size, per-file size, nesting depth, redirects, and download hosts.
