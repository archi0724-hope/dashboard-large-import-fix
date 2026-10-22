# Vendor Document Dashboard

A Streamlit dashboard for importing vendor document ZIP files, building document checklists, reviewing upload history, and exporting results.

## Features

- Import vendor documents from a local ZIP file or a public Google Drive ZIP link.
- Validate ZIP size, file count, nesting, compression ratio, and file types before import.
- Detect vendors and document categories automatically.
- Review Yes/No checklist status and document-type counts.
- Preview company PDFs inline, using the original PDF if first-page extraction is unavailable. Downloads retain the original file.
- Export checklist data as CSV or Excel workbooks.
- Store local data in SQLite/filesystem or use PostgreSQL for durable hosted storage.

## Run Locally

Requires Python 3.12 or newer. Use Python 3.12 to match CI and Docker. Run commands from the folder containing this README, `app.py`, and `requirements.txt`.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:APP_PASSWORD = "REPLACE_WITH_A_LONG_LOCAL_PASSWORD"
.\.venv\Scripts\python.exe -m streamlit run asgi_app.py --server.address 127.0.0.1 --server.port 8501
```

Open <http://127.0.0.1:8501>.

The existing dashboard is served through a native Streamlit ASGI wrapper. In another PowerShell terminal, verify process liveness:

```powershell
Invoke-RestMethod http://127.0.0.1:8501/api/health
```

Expected HTTP 200 JSON: `{"status":"ok"}`. This route does not open storage or verify database readiness. Direct `streamlit run app.py` still serves the UI, but does not add `/api/health`. The Windows launcher and Docker use `asgi_app.py`.

In this downloaded workspace, run commands from:

`C:\Users\Archi\Downloads\dashboard-large-import-fix-main\dashboard-large-import-fix-main`

See [docs/architecture.md](docs/architecture.md) for the Mermaid diagram, stack alternatives, repository structure, service costs, worker design and risks, and [docs/decisions/](docs/decisions/) for ADRs. `docs/system-design.md` was not supplied; architecture sizing remains provisional. The new `vendor_dashboard/` packages reserve module boundaries, with only HTTP composition/health implemented. Existing root business modules remain authoritative. Tenant accounts, billing, object storage and durable workers are future features, not working parts of this scaffold.

To enable the local password gate, set `APP_PASSWORD` before starting Streamlit:

```powershell
$env:APP_PASSWORD = "use-a-long-local-password"
```

## Test

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m compileall -q asgi_app.py vendor_dashboard app.py catalogue_extraction.py drive_import.py exports.py import_service.py storage.py vendor_core.py
.\.venv\Scripts\python.exe -m pytest tests -q -ra
```

CI tests both applications in separate jobs: `test` runs the root dashboard suite, and `organizer` runs the complete `data-organizer/tests` suite from its own directory with its own requirements. The Docker build remains a separate job. A single root `pytest` invocation is not the supported full-repository check: the applications have different import roots and conflicting pandas/pytest pins. Do not merge their environments or omit either suite when reporting repository-wide results.

To verify the organizer locally, create its separate environment once from the repository root, then run its suite from `data-organizer`:

```powershell
py -3.12 -m venv .venv-organizer-ci
.\.venv-organizer-ci\Scripts\python.exe -m pip install -r data-organizer\requirements.txt
.\.venv-organizer-ci\Scripts\python.exe -m pip check
Push-Location data-organizer
..\.venv-organizer-ci\Scripts\python.exe -m pytest tests -q -ra
Pop-Location
```

Tesseract with English language data must be on PATH for OCR tests. Organizer CI installs `tesseract-ocr`, `tesseract-ocr-eng` and DejaVu fonts on Ubuntu. If OCR tests skip locally because the executable is absent, report those skips; do not claim full OCR verification. Other optional converters such as LibreOffice are not installed by this CI job and are not covered by these tests.

Ruff lint/format and mypy strict cover only `vendor_dashboard/` and `asgi_app.py`; legacy modules are not claimed to pass them. `.github/workflows/scaffold.yml` runs those checks and root regressions independently of both application jobs. Syntax compilation is not a runtime test. See [ADR 0008](docs/decisions/0008-isolated-application-tests.md) for the CI isolation decision.

See [AGENTS.md](AGENTS.md) for the development workflow, module responsibilities, storage rules and verification requirements. `data-organizer/` and its embedded dashboard are separate implementations.

## Deploy

See [DEPLOY.md](DEPLOY.md) for Windows and Streamlit Community Cloud deployment instructions.

For hosted deployment:

- Set `APP_PASSWORD` in the platform's secret settings.
- Set `CLOUD_DEPLOYMENT=true` to refuse access when the password is missing.
- Set `DATABASE_URL` to a PostgreSQL connection string with `sslmode=require`.
- Use `asgi_app.py` as the Streamlit entry point for the dashboard plus `/api/health`.

## Data and Security

- Never commit vendor documents, ZIP uploads, exported workbooks, backups, or `.streamlit/secrets.toml`.
- Keep passwords and database credentials outside source control.
- Use PostgreSQL and provider backups for durable cloud records.
- Google Drive imports must use a link that permits downloading without an interactive sign-in.
- Test with synthetic documents before importing production data.

The root app reads Streamlit secrets before environment variables; it does not automatically load `.env`. Local metadata is in `vendor_data/vendor_documents.db`, with document bytes in `vendor_data/files/`; preserve the whole data directory. `VENDOR_DATA_DIR` overrides this location. PostgreSQL stores both metadata and original document bytes; provider backup and restart behavior need independent verification.

Imports keep completed writes after failures. Identical document contents are skipped by an application-level hash lookup across vendors. Retained ZIPs and upload-history events have separate counts. Originals over 32 MiB are not retained, and Drive imports disable original ZIP retention; keep those source ZIPs yourself.

Use **Data & backups** to prepare/download a backup and merge a dashboard checkpoint. Backup ZIPs omit audit history, the review queue and failed-file quarantine; product records are re-extracted on restore. Restore is limited to 1 GiB expanded data and 10,000 members, so verify restorability in isolated storage. Reset permanently deletes data and creates no automatic backup, despite some stale UI wording. See [AGENTS.md](AGENTS.md) for details and packaging verification.
