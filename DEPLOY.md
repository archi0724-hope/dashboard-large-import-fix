# Production deployment

## Streamlit Community Cloud

1. Create a private repository and copy the application files to its root.
2. Include `asgi_app.py`, `app.py`, the `vendor_dashboard/` package, `vendor_core.py`, `storage.py`, `exports.py`, `catalogue_extraction.py`, `import_service.py`, `drive_import.py`, `requirements.txt`, and `.streamlit/config.toml`. The source bundle includes the wrapper and package files.
3. Set the Streamlit entrypoint to `asgi_app.py` for the UI plus `/api/health`. Direct `app.py` remains a UI-only compatibility entry point.
4. In Streamlit app settings, add the secrets from `.streamlit/secrets.toml.example`:
   - `APP_PASSWORD`: a long random team password.
   - `DATABASE_URL`: a PostgreSQL connection URL with `sslmode=require`.
   - `CLOUD_DEPLOYMENT`: `true`, to require a configured password before access. A file named `CLOUD_DEPLOYMENT` does not enable this setting.
5. Deploy and open the app in an incognito window to verify the password gate.

`DATABASE_URL` is required for durable cloud storage. Streamlit cloud disk is temporary and must not be used for production records.

The app stores both metadata and original document/retained ZIP bytes in PostgreSQL and requires SSL connections. Verify retrieval after an isolated restart/redeploy before production use; database provider backups are not configured by this repository. Settings come from Streamlit secrets before environment variables; the root app does not automatically load `.env`. Docker Compose explicitly reads `.env`.

Both `Dockerfile` and `build_deploy_bundle.py` include `catalogue_extraction.py`, which `import_service.py` requires. The deployment-bundle regression test verifies imports from extracted source without access to the checkout. A successful image build alone does not verify application startup. The bundle's cloud marker is insufficient without the explicit setting above.

## Local Windows deployment

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:APP_PASSWORD = "use-a-long-local-password"
.\.venv\Scripts\python.exe -m streamlit run asgi_app.py --server.address 127.0.0.1 --server.port 8501
```

Open `http://127.0.0.1:8501`.

## Verification

Run these commands from the directory containing root `app.py`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m compileall -q asgi_app.py vendor_dashboard app.py catalogue_extraction.py drive_import.py exports.py import_service.py storage.py vendor_core.py
.\.venv\Scripts\python.exe -m pytest tests -q -ra
```

Repository-wide CI also runs the complete `data-organizer/tests` suite in a separate job, with its own requirements and working directory; root-only success does not establish CI success. Do not merge requirements or collect both applications in one Python process. Follow the separate-environment PowerShell commands in [README.md](README.md) when verifying both applications locally. Organizer CI installs Tesseract/English data and fonts for OCR tests. Ruff lint/format and mypy strict check the new package and ASGI launcher, not all legacy modules. `GET /api/health` returns `{"status":"ok"}` for liveness only; an HTTP response does not verify database readiness, authentication, import, counts or exports. See [docs/architecture.md](docs/architecture.md) for planned SaaS hosting and its unimplemented components.

Before production use, upload a synthetic ZIP and verify: vendor headcount, Yes/No checklist, document-type counts, upload history, export downloads, backup/restore, and reset confirmation.

Run destructive checks only in isolated storage. Verify unauthenticated access and sign-in, then persistence/retrieval after a restart. Prepare and download a dashboard backup before any authorized reset: reset permanently deletes data and creates no backup. Checkpoint restore merges with existing data, re-extracts products, and omits original audit history/review queue/failed-file quarantine; it is limited to 1 GiB declared expanded data and 10,000 members. Confirm restorability before depending on a checkpoint. See [AGENTS.md](AGENTS.md) for the full storage and verification contract.

## Data rules

- Never commit `vendor_data`, `.streamlit/secrets.toml`, backups, vendor ZIPs, or exported workbooks.
- Keep `APP_PASSWORD` outside source control.
- Use PostgreSQL and provider backups for cloud durability.
- Browser uploads are memory-backed with a configured 2,048 MB upload limit. Local-path and Drive imports reduce whole-archive memory usage but still enforce extraction limits; Drive downloads are capped at 5 GiB. Original ZIP retention is limited to 32 MiB when enabled, and disabled for Drive imports. Keep source ZIPs separately.
- The application validates ZIP paths, entry counts, expanded size, per-file size, nesting depth, redirects, and download hosts.
