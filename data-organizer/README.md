# Data Organizer

This workspace now includes the HospKart BD chatbot connected to the Vendor
catalogue. Run `python -m app serve` and open <http://127.0.0.1:8000/> to switch
between Organizer and Vendor. HospKart chat is inside Vendor AI Assistant. See [MERGED_APP.md](MERGED_APP.md)
for the shared setup, API routes and catalogue behavior.

Vendor company-document previews display the original PDF inline when first-page
extraction is unavailable. The download always contains the original file.

A production-ready pipeline and dashboard that takes a messy pile of files — Excel, legacy `.xls`, CSV, PDF,
Word, plain text, scanned images — and turns them into one clean, traceable master list of entities (hospitals,
companies, or any other kind of organisation you choose), with every name variant recorded and every record
traceable back to its exact source file, sheet/page and row.

Nothing is ever guessed into existence and nothing is ever silently thrown away. Uncertain matches go to a
manual review queue instead of being merged automatically; your decisions there are remembered and applied to
every file you process afterwards.

## What it does, in one paragraph

Point it at a folder (local or a read-only Google Drive folder). It discovers every file, extracts every row
into a raw, fully-traceable staging layer (original values untouched), cleans and normalises names/addresses/
phones/emails/registration IDs, matches records to master entities using a layered scoring system (exact
mappings → identifiers → fuzzy name + corroborating evidence → optional AI opinion for genuinely ambiguous
cases only), flags anything it isn't sure about for a human, finds duplicates without removing anything, and
exports a full set of Excel/CSV/JSON/Parquet reports plus a processing report. Everything is resumable: stop it
at any point and it picks up exactly where it left off; adding new files never redoes finished work.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                 # optional - sensible defaults work out of the box

# Safe first run: generates ~11 synthetic sample files (Excel, .xls, CSV, PDF, Word, text, a scanned
# image, a corrupt file...) with deliberately messy name variants, runs the full pipeline on them,
# and writes the output files. Nothing from your own data is touched.
python -m app demo

# Then open the dashboard to see the results and review the couple of uncertain matches:
python -m app serve
# -> http://127.0.0.1:8000
```

Once you're comfortable with what it produces, point it at your real files from the dashboard's **Source & files**
page (a local folder, a Google Drive folder, or drag-and-drop upload), or from the command line:

```bash
python -m app run --input /path/to/your/files --export
```

### Isolated tests and CI

Run tests from this `data-organizer` directory with an environment installed from this application's `requirements.txt`. The root Streamlit dashboard has different pandas/pytest pins and its own `app.py`; do not install both requirement lists in the same environment or collect both suites in one pytest process. Root CI job `organizer` runs this complete test suite separately from dashboard job `test`.

PowerShell commands from `data-organizer` (create the separate environment only if absent):

```powershell
py -3.12 -m venv ..\.venv-organizer-ci
..\.venv-organizer-ci\Scripts\python.exe -m pip install -r requirements.txt
..\.venv-organizer-ci\Scripts\python.exe -m pip check
..\.venv-organizer-ci\Scripts\python.exe -m pytest tests -q -ra
```

Put Tesseract with English language data on PATH for OCR coverage. CI installs it and DejaVu fonts on Ubuntu; missing local OCR tools cause explicit test skips that must be reported. CI does not install LibreOffice or claim legacy `.doc` converter verification.

### System requirements

- Python 3.12 (see `requirements.txt` for pinned package versions)
- `tesseract-ocr` — only needed to read scanned PDFs/images (`apt install tesseract-ocr` /
  `brew install tesseract`). Without it, scanned files are listed with a warning instead of failing.
- `libreoffice` — only needed to read legacy `.doc` files (converts a temporary copy to `.docx`; your original
  is never touched). Without it, `.doc` files are reported as unsupported.
- Or just use Docker (below), which includes both.

### Docker

```bash
cp .env.example .env
mkdir -p input                        # put your files here (mounted read-only)
docker compose up --build
# -> http://127.0.0.1:8000
```

## The dashboard

**Extract products & download Excel** on the Organizer dashboard reads the
currently selected input files and creates `Company Products and Prices.xlsx`.
It runs in the background and shows file progress. The workbook contains
**Products & Prices**, **Company Contacts**, and **Needs Review**, sorted by
company, product name and SKU. Price and MRP stay separate; specifications and
pack sizes are retained. Unclear amounts, missing fields, OCR rows, conflicting
prices and possible duplicates are flagged. Original documents are unchanged.
Organizer uploads and ZIP extraction have no application file-size, total-size,
or file-count cap. Large Excel exports continue onto numbered sheets instead of
dropping rows when an individual worksheet reaches Excel's format limit.

The Organizer dashboard uses three steps: **Import**, **Read files**, and
**Company folders**. Search companies directly on the dashboard, open a company
to see its source records, and download the company folders as a ZIP.

Excel (`.xlsx`, `.xls`, `.xlsm`), PDF, JPG/JPEG and PNG are read in their existing
formats. Scanned PDFs and images use Tesseract OCR; the dashboard shows whether
image reading is ready. CSV, Word, text, JSON and other supported image formats
also use their existing readers. Legacy `.doc` files require LibreOffice.

Company folders contain copies of the original documents, with their contents
and extensions preserved. A file containing several companies is copied in full
to each matching company folder. Different files with the same filename are kept
with a distinguishing suffix. Files with no identified company remain available
for review instead of being discarded. Company record reports are separate from
these original documents.

| Page | What it's for |
|---|---|
| **Overview** | The 6-step workflow, live progress, and charts once you have data. |
| **Source & files** | Choose a local folder / Google Drive folder / upload / demo data, scan it, and inspect exactly how each file was understood (detected header row, column → field mapping, sample rows) *before* committing to a full run. |
| **Preview (dry run)** | Runs the real pipeline on a small sample in a throw-away database — nothing is saved — so you can sanity-check the standardisation first. |
| **Manual review** | One card at a time: the record found vs. the suggested master entity, a full evidence checklist, and Accept / Reject / New entity. Decisions are remembered (`user_decisions`) and auto-applied to future files. Keyboard: `A` accept, `R` reject, `N` new entity, `J`/`K` next/previous. |
| **Catalogue approval** | Upload an Excel/CSV price list or WhatsApp TXT/ZIP export for an existing vendor, review and correct extracted rows, then explicitly approve or reject each row. Approved products become searchable by the Vendor dashboard and assistant; price changes are retained in an audit history. |
| **Master data** | Every resolved entity: aliases, contact info, conflicting data flagged, full record-level traceability, rename / merge. |
| **Quality & duplicates** | Duplicate groups (exact name+phone, exact name+registration ID, identical rows, near-duplicate entities) — reported, never auto-deleted — plus data-quality findings and processing errors. |
| **Export** | Pick formats (xlsx/csv/json/parquet) and optional grouping (by state/district/city/type/category/source/year) into sub-folders, then download. |
| **Settings** | Matching thresholds, automatic-matching / manual-review toggles, OCR, and reset. |
| **Logs** | Tail of the five log files (application, extraction, matching, errors, audit). |

### Catalogue approval and price history

The Organizer's **Catalogue approval** page writes to the same local SQLite /
configured PostgreSQL store used by the Vendor dashboard. Select an existing
vendor ID and upload a `.csv`, `.xlsx`, `.xls`, WhatsApp `.txt`, or `.zip`
containing WhatsApp text exports (100 MB maximum). Common product, SKU, price,
unit, pack-size, and category headers are recognized; individual fields can be
corrected before approval. WhatsApp extraction only pairs a product and one
price found in the same message; ambiguous messages are skipped instead of
guessing across chat messages. Extracted chat rows are marked medium confidence
and still require review. The original source file remains in the Vendor store.

Rows stay out of catalogue search until explicitly approved. Repeated uploads
for the same vendor are idempotent. Matching product/SKU/package rows show the
existing price; approving a changed price updates the searchable record and
adds a source-linked entry to price history. Exact repeats are marked duplicate,
and rows that match multiple listings must be corrected or rejected. The page
offers import metrics, recent price history, a cleaned import CSV, and a sorted
approved-catalogue search with an Excel download. Approval writes directly to
the shared Vendor product store, so published products are available in Vendor
search without a separate synchronization job.

On the Vendor dashboard's **Catalogue & prices** page, the **Manually upload an
Excel catalogue or price list** panel lets you choose an existing vendor and
upload `.xlsx`, `.xls`, or `.csv` (up to 100 MB). Clicking **Upload and merge
into shared catalogue** publishes detected rows immediately to the same
catalogue that feeds Vendor product search and the company-wise Excel export.
Existing products matched by SKU/product, pack, and unit are updated in place;
changed prices are added to price history, and repeat uploads are deduplicated.
Rows that match multiple existing listings are not published automatically:
they remain in Organizer catalogue approval for correction. The uploaded
workbook is retained as the source file and is not overwritten; download the
generated company-wise workbook to get the integrated catalogue.

Catalogue rows extracted from the Vendor dashboard's existing document-upload
workflow are also staged for approval. Older product records remain visible for
backward compatibility. Authentication is shared at dashboard level: set
`DASHBOARD_TOKEN` for Organizer API access and `APP_PASSWORD` for the Vendor
dashboard before exposing the service; neither setting provides per-vendor
accounts or role-based access control.

Approved catalogue rows provide searchable reference data and price history;
they do not automatically train a machine-learning model. No scheduled model
evaluation, retraining, model promotion, or rollback workflow exists yet. Raw
or unapproved uploads are never treated as training labels.

## Command line

```
python -m app demo                    # generate synthetic files, run everything, export (safe first run)
python -m app serve                   # dashboard + API (http://127.0.0.1:8000 by default)
python -m app scan     --input DIR    # list files
python -m app inspect  --input DIR    # per-file structure report (no records written)
python -m app dry-run  --input DIR    # small sample through the real pipeline, nothing saved
python -m app run      --input DIR --export
python -m app reviews  --input DIR    # list pending manual reviews
python -m app drive-auth              # one-time Google Drive OAuth (needs secrets/client_secret.json)
python -m app reset [--all]           # clear matching results (or everything)
python -m app benchmark --records 50000
```

## How matching works (the important part)

Records are assigned to a master entity in this order, cheapest and most certain first:

1. **Your verified mappings** — anything you confirmed in Manual Review, applied instantly and automatically
   to every future occurrence of that name (optionally scoped to "this name in this city" or "this name
   anywhere").
2. **Identical earlier record** — an exact repeat of a record already matched in this run.
3. **Unique identifiers** — a shared GSTIN / CIN / Udyam / PAN (with checksum validation), phone, e-mail, or
   website domain is very strong evidence and can override a mediocre name match.
4. **Fuzzy name + corroborating evidence** — RapidFuzz scoring on normalised names (with initialism detection,
   e.g. "SMS Hospital" ↔ "Sawai Man Singh Hospital"), blocked for speed by shared tokens/identifiers plus an
   optional TF-IDF character n-gram nearest-neighbour pass to catch typos. **Name similarity alone never
   triggers an automatic merge** — it always needs at least one corroborating fact (city, address, phone,
   e-mail, registration ID...). Conflicting cities/states/identifiers, or a "distinguishing" word (east/west/
   branch/unit-2 …) present on only one side, hard-cap the score and force a match to manual review regardless
   of how the auto-merge threshold is configured.
5. **An optional LLM opinion** — only ever asked about pairs that are already ambiguous after step 4, only the
   two names/cities/addresses are sent (never whole files or other columns), and the verdict is **advisory by
   default**: the pair still goes to manual review unless you explicitly turn on `AI_AUTO_ACCEPT` *and* the
   resolver's own evidence rules are also satisfied.

A record that is still ambiguous gets its own provisional entity and a review item; every other record sharing
that same ambiguous name is folded into the same review item (`record_count`) rather than creating duplicate
review rows. The confidence bands (**Very High / High / Possible Match / Manual Review / Probably Different**)
and every threshold are configurable in Settings.

## Project structure

```
app/
  ingestion/      file discovery, format-specific readers (xlsx/xls/csv/txt/pdf/docx/json/images+OCR),
                  header detection, column → universal-schema mapping, free-text parsing, raw record extraction
  cleaning/       name normalisation (safe vs. ambiguous abbreviations), place/gazetteer normalisation,
                  phone/email/website/GSTIN-CIN-Udyam-PAN validation, per-record cleaning + quality flags
  matching/       name similarity, blocking index + TF-IDF retrieval, confidence scoring, entity aggregation,
                  the resolver orchestrator, duplicate detection, manual-review service, learned mappings
  database/       DuckDB schema and repository (all persistence goes through here)
  drive/          read-only Google Drive source (OAuth or service account)
  export/         Excel/CSV/JSON/Parquet writers, grouped exports, the processing report
  dashboard/      static single-page dashboard (no build step: plain HTML/CSS/JS + Chart.js)
  pipeline.py     end-to-end orchestrator: discover → extract → clean → resolve → duplicates → quality
  services.py     application service layer shared by the API and the CLI
  main.py         FastAPI app (JSON API + serves the dashboard)
  cli.py          `python -m app ...`
config/           abbreviations, entity-type profiles, place/gazetteer data, schema column aliases (JSON,
                  editable without touching code)
data/             raw/staging cache, the DuckDB database, demo input, output (all git-ignored)
scripts/          synthetic demo-data generator (also used as a benchmark data generator)
tests/            pytest suite (cleaning, ingestion, matching rules, full pipeline runs, API, export, Drive/AI)
```

## Data safety

- Source files are **read-only** at every step; the output folder is checked at startup to make sure it can
  never overlap the input folder.
- The raw layer keeps every original value forever, in full — cleaning and matching only ever *add* columns.
- Nothing is ever deleted automatically. Duplicates are reported as groups with a suggested primary; merges
  only happen when you (or a saved decision) say so, and a merge is reversible in that the absorbed entity's
  data is kept (`merged_into`), never dropped.
- Every processing failure is isolated to its file/sheet/page/row, logged with full context, and never stops
  the rest of the run.
- Google Drive access uses the `drive.readonly` scope only — the app can list and download, never write, move,
  or delete anything in your Drive.

## Testing

```bash
pytest                     # ~65 tests: cleaning rules, every file format, matching rules from the spec
                            # (including the SMS Hospital / Sawai Man Singh Hospital example), full pipeline
                            # runs (idempotent re-runs, incremental new-file runs, stop/resume), the API,
                            # exports, and the Drive/AI integrations (mocked, no network calls)
python -m app benchmark --records 50000     # throughput on synthetic data
```
