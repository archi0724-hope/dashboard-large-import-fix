# Production deployment

## Streamlit Community Cloud

1. Create a private repository and copy the application files to its root.
2. Include `app.py`, `vendor_core.py`, `storage.py`, `exports.py`, `import_service.py`, `drive_import.py`, `requirements.txt`, `.streamlit/config.toml`, and `CLOUD_DEPLOYMENT`.
3. Set the Streamlit entrypoint to `app.py`.
4. In Streamlit app settings, add the secrets from `.streamlit/secrets.toml.example`:
   - `APP_PASSWORD`: a long random team password.
   - `DATABASE_URL`: a PostgreSQL connection URL with `sslmode=require`.
5. Deploy and open the app in an incognito window to verify the password gate.

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

## Verification

```powershell
python -m compileall -q app.py drive_import.py exports.py import_service.py storage.py vendor_core.py
python -m pytest tests -q
Invoke-WebRequest http://127.0.0.1:8501
```

Before production use, upload a synthetic ZIP and verify: vendor headcount, Yes/No checklist, document-type counts, upload history, export downloads, backup/restore, and reset confirmation.

## Data rules

- Never commit `vendor_data`, `.streamlit/secrets.toml`, backups, vendor ZIPs, or exported workbooks.
- Keep `APP_PASSWORD` outside source control.
- Use PostgreSQL and provider backups for cloud durability.
- Large direct uploads are limited by Streamlit memory and the configured 2 GB request limit. For larger files, use the local ZIP path or a public Drive ZIP link.
- The application validates ZIP paths, entry counts, expanded size, per-file size, nesting depth, redirects, and download hosts.
