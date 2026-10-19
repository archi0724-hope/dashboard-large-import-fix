# Vendor Document Dashboard

A Streamlit dashboard for importing vendor document ZIP files, building document checklists, reviewing upload history, and exporting results.

## Features

- Import vendor documents from a local ZIP file or a public Google Drive ZIP link.
- Validate ZIP size, file count, nesting, compression ratio, and file types before import.
- Detect vendors and document categories automatically.
- Review Yes/No checklist status and document-type counts.
- Export checklist data as CSV or Excel workbooks.
- Store local data in SQLite/filesystem or use PostgreSQL for durable hosted storage.

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
python -m compileall -q app.py drive_import.py exports.py import_service.py storage.py vendor_core.py
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
