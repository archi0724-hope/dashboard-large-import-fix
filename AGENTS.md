# Vendor Document Dashboard: project guide

This is the canonical project guide for coding agents. Read it together with the current user request. `CLAUDE.md` points here; keep both consistent. Inspect the implementation before editing and distinguish verified behavior from assumptions.

## What this product does

The dashboard helps a team import vendor documents from ZIP files, identify companies and document categories, and review availability through checklists and counts. Users can review ambiguous files, search extracted catalogue/price information, inspect upload history, and export records or download stored documents.

## Stack

- Python: use 3.12 to match `.github/workflows/ci.yml` and `Dockerfile`. README permits 3.12 or newer; compatibility with other versions needs verification.
- Streamlit: `streamlit[pdf]==1.63.0`; root entry point is `app.py`.
- ASGI entry point: `asgi_app.py` uses native `st.App` and Starlette to serve the existing UI plus public `/api/health` on one port. Direct `app.py` remains UI-only compatible.
- Development quality: pinned Ruff lint/format and mypy strict from `requirements-dev.txt`, scoped to `vendor_dashboard/` and `asgi_app.py`. Legacy modules remain outside this strict surface.
- pandas, openpyxl and xlrd: tabular processing and spreadsheet input/output.
- pypdf: PDF text/preview processing. Pillow and pytesseract support image OCR; OCR also needs an external Tesseract installation, which pip does not provide.
- requests: bounded Drive downloads; psycopg: PostgreSQL access; SQLite: local metadata.
- Plotly is declared in `requirements.txt`. Preserve existing dependency versions and constraints.
- The password gate uses `APP_PASSWORD`; it is a shared team password, not user accounts, roles, or tenant isolation.

Ask before adding dependencies, replacing libraries, changing the stack or access model, or introducing migration tooling. Installing dependencies already used by CI is routine setup. Do not assume an ORM, auth provider, or hosting provider beyond the checked-in implementation.

## Architecture

See [docs/architecture.md](docs/architecture.md) and [docs/decisions/](docs/decisions/). Keep one Python modular monolith and preserve root business-module imports. `vendor_dashboard/web/` contains HTTP composition and health; `domain/`, `services/`, `infrastructure/` and `jobs/` are reserved boundaries with no feature implementations. Future dependencies point web/jobs -> services -> domain, with infrastructure adapters at the edge. `asgi_app.py` launches the wrapper; `app.py` stays the UI script. Runtime dependencies remain in `requirements.txt`, developer tools in `requirements-dev.txt`, and check configuration in `pyproject.toml`.

Managed identity with tenant-scoped memberships, private object storage, a PostgreSQL job table and one worker are documented target designs only. Do not claim that this scaffold implements them or admits unrelated customer organisations safely. Existing shared-password authentication, synchronous imports and storage remain unchanged. `docs/system-design.md` is absent; scale, roles, billing, regions and service targets are provisional/open.

## Application structure

| File | Responsibility |
| --- | --- |
| `app.py` | Streamlit UI, password gate, configuration, cached store, previews, navigation, import/export/backup actions and local vendor search |
| `vendor_core.py` | Company normalization, classification, ZIP iteration and limits, vendor discovery, checklist/filter/count logic |
| `import_service.py` | Import orchestration, per-file outcomes, catalogue extraction, optional original ZIP retention and audit events |
| `drive_import.py` | Public Drive URL/redirect validation, streamed temporary downloads and ZIP checks |
| `storage.py` | SQLite/PostgreSQL operations, document bytes, aliases, product records, archives, audit history, backup/restore and reset |
| `catalogue_extraction.py` | Catalogue/price records from spreadsheets, CSV, text, PDF, DOCX and images |
| `exports.py` | CSV and Excel exports, including catalogue/price workbooks and spreadsheet text handling |
| `tests/` | Root dashboard tests using synthetic data, temporary storage, mocked Drive and Streamlit AppTest |
| `build_deploy_bundle.py` | Source-only deployment ZIP assembly, including catalogue extraction |

`data-organizer/` is a separate application with its own dependencies, tests, and an embedded `vendor_dashboard/` implementation. Do not treat similarly named modules as interchangeable or copy changes between them without tracing a dependency. Root `app.py` also offers an organizer-style view; this does not make it the separate application.

## Data and storage

### Configuration and backend selection

`app.py:setting()` reads Streamlit secrets first, then environment variables. The root app does not automatically load `.env`; Docker Compose explicitly supplies `.env` through `env_file`. Example files contain placeholders only.

- `DATABASE_URL` unset: SQLite at `<VENDOR_DATA_DIR>/vendor_documents.db`; default directory is `vendor_data` beside `app.py`.
- `DATABASE_URL` set: PostgreSQL metadata and document/archive byte payloads (`BYTEA`). Only PostgreSQL URLs are accepted; connections require SSL.
- `VENDOR_DATA_DIR`: local storage override, useful for isolated verification.
- `APP_PASSWORD`: enables the shared password gate. Without it, local access is open.
- `CLOUD_DEPLOYMENT=true`: refuses access if `APP_PASSWORD` is absent; it does not select PostgreSQL or guarantee persistence. The code reads this setting, not the deployment bundle's marker file.

Authentication runs before opening the protected store. Preserve this ordering.

### Bytes, restarts and repeated imports

Local document bytes live in `files/` under the data directory, retained originals in `uploaded_zips/`, and quarantined processing failures in `failed_files/`; `temporary/` is also managed there. SQLite alone is not a complete local backup. Local persistence requires retaining the data directory; hosted ephemeral disks do not survive all redeployments. Docker Compose mounts a named volume at `/app/vendor_data`.

PostgreSQL mode stores document and retained ZIP bytes in the database alongside metadata. Provider retention/backups, live restart retrieval, and hosted redeploy behavior require independent validation; do not claim them from source inspection alone. Failed-file quarantine is local-only.

`save_document()` looks up SHA-256 content hashes across companies before inserting: repeated identical bytes are skipped even when vendor assignment changes. Unreviewed duplicate documents may gain category information, and missing local bytes may be repaired. The schema uniqueness constraint is `(company_key, file_hash)`, so the application-level lookup is not a database-wide unique-hash guarantee under concurrency. Different contents with the same filename remain distinct through hash-based storage names. Product records are deduplicated by document ID and record hash. Repeated retained ZIPs update archive upload counts rather than creating another archive row.

Imports commit through separate store operations, not one batch transaction. Completed writes remain after failures; extraction can fail after a document has been saved. Preserve partial-outcome reporting, retries and duplicate rules unless a change is approved. Audit upload events, stored documents, retained archive rows and vendors are different quantities.

### Archive/resource behavior

Current `ArchiveLimits`: 5,000 entries, 5 GiB cumulative expanded bytes, 128 MiB per document, 256 MiB nested ZIP member size, nesting depth 2, compression ratio limit 1,000. Trace the exact accounting before changing it. Drive has a 5 GiB compressed download limit and streams to temporary disk; browser uploads remain memory-backed with a configured 2,048 MB limit. Local-path/Drive import does not bypass extraction or memory limits.

Original ZIPs are retained only when enabled and no larger than 32 MiB; Drive imports disable retention. Extracted documents are stored separately. Keep source ZIPs outside the app when originals are not retained.

### Backup, restore and migrations

Use **Data & backups → Prepare full data backup**, then download the generated ZIP. The v3 manifest includes vendors, aliases, documents and retained upload archives with integrity-checked bytes. It does not snapshot all database tables: audit history, review queue and failed-file quarantine are not included; product records are re-extracted on restore. Provider/database backups are a separate operational responsibility.

**Restore Checkpoint** merges a dashboard backup into existing data and skips duplicate document contents. It validates members before writes but does not wrap the whole merge in a single transaction. Current restore limits are 10,000 ZIP members and 1 GiB declared expanded size; a generated backup can exceed these limits. Verify a backup's restorability in isolated storage before relying on it.

Reset requires confirmation and permanently deletes dashboard records, archives, history, backups and local runtime artifacts. It creates no automatic backup. Some UI wording still refers to reset backups; do not rely on that wording. Never run reset or restore against production without explicit authorization.

Schema setup/column upgrades occur in `Store.initialize()` and helper methods. `migrate_original()` performs an idempotent local legacy migration and creates a pre-update SQLite backup when legacy tables exist. There is no standalone migration CLI or versioned migration framework. Before a new schema change, propose a minimal versioned approach and explain compatibility, backup and recovery implications.

## Rules

### Working loop

1. Read this guide, `CLAUDE.md`, README, deployment guidance, configuration examples, CI, relevant modules and tests. Identify entry point, current behavior, data flow, test coverage and unverified assumptions.
2. Present findings, smallest useful change, affected files, acceptance criteria, verification commands and dependency/schema/deployment implications. Wait for approval where the task requires it. Approved routine steps do not need repeated confirmation.
3. Check the working tree; preserve user edits and use a task branch in a Git checkout. If the supplied folder has no `.git`, report that limitation rather than inventing branch/revision evidence or initializing Git implicitly.
4. Make focused changes. Avoid unrelated refactoring, formatting, speculative features, new abstractions and removal of pre-existing dead code. Remove unused code introduced by your own change.
5. Verify actual results and review the full diff for secret exposure, data loss, count inconsistencies and missing coverage. Use an independent review if authorized and useful.
6. Update affected documentation and decisions. Report changed files, executed commands/exit statuses/test totals, failures/skips, incomplete work, assumptions, data implications, exact run instructions and remaining risks. Merge or deploy only when authorized.

### Security and integrity

- Never commit credentials, vendor documents, ZIP uploads, exports, databases, backups or `.streamlit/secrets.toml`. Keep configuration examples safe and never print credential-bearing connection strings.
- Preserve authentication before protected operations and data access. Use parameterized database queries.
- Never modify production records, reset databases or perform destructive operations without explicit authorization. Use synthetic documents and isolated test storage.
- Preserve ZIP type/path/traversal/absolute-path, size, entry-count, nesting, ratio, encryption, symlink and corruption safeguards. Enforce relevant bounds while processing, not only during initial inspection. Do not solve large imports by removing limits.
- Preserve Drive HTTPS host/redirect validation, timeouts, bounded download, HTML/sign-in error handling and post-download ZIP validation. A successful HTTP response is not proof of a valid ZIP.
- Do not report import success before required storage operations finish. Explain partial writes and distinguish exposed processed, saved, duplicate, review and issue counts; do not invent unavailable scanned/rejected totals.
- Keep unique-vendor counts, available documents, category document counts, vendors with categories and upload-history counts distinct. Keep checklists, dashboard and exports consistent with source records and duplicate rules.
- Preserve normalization/classification; surface ambiguity and unclassified files. Preserve progress, useful errors, summaries, history and download actions. Check reruns for accidental repeat imports and stale cached results when state behavior changes.
- Verify filtered/empty exports, identifiers, columns, checklists and user-controlled spreadsheet content, including formula interpretation.
- Verify metadata persistence and original-byte persistence separately. For hosted-storage changes, test isolated restart/retrieval behavior and record untested cases.

### Verification requirements

Establish a baseline before behavior changes. Add focused regression coverage when it proves the behavior. For import/storage/checklist/export changes, use a known synthetic fixture with multiple vendors/categories, a missing category, invalid inputs and relevant duplicates.

Full-repository verification requires both application suites in separate environments and processes. CI job `test` installs root requirements plus pytest and runs root `tests/`; job `organizer` installs only `data-organizer/requirements.txt` and runs its complete `tests/` suite with working directory `data-organizer`. Cache keys use each application's requirements. Preserve both jobs and report failures/skips independently; do not narrow collection to conceal failing tests. A single root `pytest` invocation is not the supported combined check because import roots and pandas/pytest pins differ. Never merge the dependency lists. Organizer CI installs Tesseract, English data and DejaVu fonts so its existing OCR tests run; report local OCR skips if those tools are unavailable. See [ADR 0008](docs/decisions/0008-isolated-application-tests.md).

For UI changes, verify the affected interaction using AppTest and a browser where available; an HTTP health response is insufficient. Docker build is relevant for packaging/deployment changes but does not establish runtime import success. For documentation-only work, verify paths/commands without changing application behavior. Never describe unexecuted checks as passing or claim Windows, browser, PostgreSQL or deployment verification without evidence.

## Commands

Run in the directory containing root `app.py` and `requirements.txt`. In this downloaded workspace it is:

`C:\Users\Archi\Downloads\dashboard-large-import-fix-main\dashboard-large-import-fix-main`

### Windows setup and development

```powershell
py -0p
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:APP_PASSWORD = "REPLACE_WITH_A_LONG_LOCAL_PASSWORD"
.\.venv\Scripts\python.exe -m streamlit run asgi_app.py --server.address 127.0.0.1 --server.port 8501
```

Open <http://127.0.0.1:8501>. Set the password before starting Streamlit. Keep real values private. If Python 3.12 is unavailable, report installed versions before selecting another interpreter. Do not recreate an existing virtual environment without inspecting it.

### Checks

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m compileall -q asgi_app.py vendor_dashboard app.py catalogue_extraction.py drive_import.py exports.py import_service.py storage.py vendor_core.py
.\.venv\Scripts\python.exe -m pytest tests -q -ra
```

Organizer setup/checks from the repository root (create the separate environment only if absent):

```powershell
py -3.12 -m venv .venv-organizer-ci
.\.venv-organizer-ci\Scripts\python.exe -m pip install -r data-organizer\requirements.txt
.\.venv-organizer-ci\Scripts\python.exe -m pip check
Push-Location data-organizer
..\.venv-organizer-ci\Scripts\python.exe -m compileall -q app vendor_dashboard hospkart_chat scripts
..\.venv-organizer-ci\Scripts\python.exe -m pytest tests -q -ra
Pop-Location
```

Compileall checks syntax only. CI covers both application suites in isolated jobs and includes the root ASGI wrapper, package and catalogue module in syntax checks. Scaffold CI separately runs Ruff lint/format, mypy strict and root tests. Lint/format/type checking cover only the new package and launcher; do not silently reformat or claim strict coverage for legacy modules. Standalone migration command: **Not configured.** Check `/api/health` with `Invoke-RestMethod http://127.0.0.1:8501/api/health` after launching the wrapper; it is liveness only and opens no store.

Existing packaging commands (only when relevant):

```powershell
docker build -t vendor-dashboard:test .
docker compose up --build
.\.venv\Scripts\python.exe build_deploy_bundle.py
```

Both `Dockerfile` and `build_deploy_bundle.py` include required `catalogue_extraction.py`. `tests/test_deploy_bundle.py` builds and extracts a bundle, then imports its application modules in an isolated subprocess so missing files cannot be supplied by the source checkout. This verifies module packaging, not Streamlit startup or Docker runtime behavior. The bundle writes a ZIP in the parent directory and includes a marker file that does not itself enable the cloud password requirement. See `DEPLOY.md` for explicit settings. No deployment is implied by running checks.

## Architecture decisions

Record significant approved decisions in `docs/decisions/` as ordinary Markdown files. Each decision explains context, chosen approach, alternatives and consequences. Create a decision file when a significant decision is approved; do not fabricate past decisions. Update this guide when approved work changes architecture, configuration, commands or operational behavior. Keep temporary investigation notes out of permanent project memory.
