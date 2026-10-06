# Data Organizer

This workspace now includes the HospKart BD chatbot connected to the Vendor
catalogue. Run `python -m app serve` and open <http://127.0.0.1:8000/> to switch
between Organizer and Vendor. HospKart chat is inside Vendor AI Assistant. See [MERGED_APP.md](MERGED_APP.md)
for the shared setup, API routes and catalogue behavior.

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

| Page | What it's for |
|---|---|
| **Overview** | The 6-step workflow, live progress, and charts once you have data. |
| **Source & files** | Choose a local folder / Google Drive folder / upload / demo data, scan it, and inspect exactly how each file was understood (detected header row, column → field mapping, sample rows) *before* committing to a full run. |
| **Preview (dry run)** | Runs the real pipeline on a small sample in a throw-away database — nothing is saved — so you can sanity-check the standardisation first. |
| **Manual review** | One card at a time: the record found vs. the suggested master entity, a full evidence checklist, and Accept / Reject / New entity. Decisions are remembered (`user_decisions`) and auto-applied to future files. Keyboard: `A` accept, `R` reject, `N` new entity, `J`/`K` next/previous. |
| **Master data** | Every resolved entity: aliases, contact info, conflicting data flagged, full record-level traceability, rename / merge. |
| **Quality & duplicates** | Duplicate groups (exact name+phone, exact name+registration ID, identical rows, near-duplicate entities) — reported, never auto-deleted — plus data-quality findings and processing errors. |
| **Export** | Pick formats (xlsx/csv/json/parquet) and optional grouping (by state/district/city/type/category/source/year) into sub-folders, then download. |
| **Settings** | Matching thresholds, automatic-matching / manual-review toggles, OCR, and reset. |
| **Logs** | Tail of the five log files (application, extraction, matching, errors, audit). |

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
