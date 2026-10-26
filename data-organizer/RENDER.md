# Organizer preview on Render

## Purchase invoices and large collections

`/purchase-invoices` provides upload → extraction → review/edit → save → search.
Both dashboards link to it. It accepts PDF, JPG, PNG, XLSX, XLS and CSV, one
invoice per file, up to 20 MiB; PDFs up to 20 pages and spreadsheets up to
10,000 rows / 100 columns. A single isolated extraction worker runs with a
150-second timeout. Textless PDF pages and images use the installed Tesseract.
Drafts are private to the upload session and expire on eviction or server restart.
Users must confirm review, supplier and invoice number before saving. Line items
from PDF/images may need manual entry. Extraction does not calculate missing
taxes or totals. Original upload bytes are deleted after extraction; keep originals
in Drive or on the source computer. Reviewed records and source-text copies are
shared in the additive `vdd_purchase_invoices` PostgreSQL table; duplicate source
hashes keep the first reviewed record. Legacy Vendor reset/backup does not include
this separate table. Use PostgreSQL backups for these records.
Vendor's **Purchase Invoice** sidebar page embeds this same workflow, and its
summary card counts confirmed records from the shared PostgreSQL invoice table.
Local screenshot counts are not copied or fabricated during deployment.

Vendor's **Companies & documents** combines uploaded documents and shared Drive
references in one company search, selector, Yes/No checklist and summary cards.
Only exact normalized names and confirmed aliases merge companies; known equal
content hashes within a company count once. Archive containers and unsafe paths
are excluded. Drive originals stay in Drive: company categories offer source
links and member paths, and company ZIPs contain uploaded files plus a Drive-link
CSV register. Excel exports retain those source links and processing status.
Drive classification/extraction issues remain visible, and source rows are never
automatically approved as catalogue products. **Catalogue & prices** retains the
paginated source-row search. The Drive metadata cache lasts 20 seconds; Refresh
companies and documents explicitly reloads it. These read views do not copy file
blobs or mutate the original upload and Drive index tables.

For the 50+ GB collection, originals stay in Drive and searchable records live in
PostgreSQL. There is no fixed total collection byte cap in the Drive indexer;
processing safeguards and database space still apply. Local direct uploads no
longer have a fixed 2 GiB transport cap: they must fit actual temporary server space.
Vendor → Upload documents → Google Drive library embeds the authenticated shared
library, including Start/Resume, progress, issues and search. It replaces the old
Google Drive ZIP download form, so existing sessions cannot send a 10+ GB archive
through the obsolete 5 GiB full-download path. Put new archives in the connected
folder before starting/resuming. Users can paste a folder or ZIP file link directly in the embedded library and
choose **Use link and start indexing**. The normalized selection persists in
`vdd_drive_sources`; all selections add to the same shared library scope. Switching
sources preserves existing records. Pause an active job before switching to a
different source. Direct ZIP links use range reads without a folder scan; the
file must be an ordinary, readable ZIP. The form does not upload files to Drive.
Drive range reads support ZIP64 offsets beyond 50 GiB without fetching the entire
archive. This is covered by a sparse virtual ZIP64 test, not a full 50 GB production
import. The existing 5,000-entry ZIP cap, 16 MiB directory/read cap and 128 MiB
per-document extraction cap still apply. Split larger inventories into ordinary
independent ZIPs, not multipart `.z01` archives. Source originals stay in Drive,
and derived records must fit the database quota. OCR-only and unsupported files
are listed as issues rather than represented as fully extracted. No promise of
fully searching an arbitrary 50 GB archive on the free preview is made.

Deploy branch `deploy/organizer` using Docker context `data-organizer` and
Dockerfile `data-organizer/Dockerfile`. The app listens on port 8000; set
`PORT=8000`. `/` opens the Organizer dashboard, `/vendor/` opens Vendor,
and `/healthz` is a public liveness check without database access.

Keep `DASHBOARD_TOKEN` set privately in Render. The Organizer dashboard prompts
for this token and remembers it for the browser session. Use `APP_PASSWORD` for
the embedded Vendor dashboard and `CLOUD_DEPLOYMENT=true` so its password gate
cannot be bypassed by omitting a password. These are shared team credentials.

The free preview uses temporary local DuckDB and local document/cache storage.
Redeploys or restarts can lose processing state, uploads, organized copies and
exports. Google Drive retains the source files, but is not the live DuckDB
database. Do not use the preview as the only copy of your records. A persistent
production setup requires a paid disk and all relevant data paths on it.
The free plan's 512 MB memory can also limit OCR and large-file processing.

In hosted Vendor uploads, **Upload ZIP from computer → Choose ZIP files** opens a
dedicated browser picker with transfer progress and background-import progress.
It registers a job with a small request, then sends at most 8 MiB per request to
a temporary disk file rather than storing the whole ZIP in Streamlit's RAM.
The existing importer reads that file and saves
documents to the shared Vendor store. Original ZIP retention is disabled for
this path; keep the original ZIP on your computer. Temporary uploads are removed
after import or failure. Completed document writes survive an interrupted import
in PostgreSQL, and retrying the ZIP uses the existing duplicate rules.

Upload sessions use a three-hour scoped token signed with `APP_PASSWORD`; the
password itself is not sent to the upload page. The token travels in the URL
fragment when opening the page and in a request header for API calls, not in
logged API query strings. One ZIP may upload/import at a time on this instance.
The service checks actual available disk before and during upload. There is no
fixed transport byte cap; archive entry, expanded-byte, member-size, traversal and nested
ZIP safeguards remain unchanged. Disk-backed transfer does not increase free
server CPU, per-document processing memory, or the database's storage quota.

The local ZIP path option is available only when `CLOUD_DEPLOYMENT` is disabled.
Hosted sessions with an old local-path selection return to direct upload.
`/vendor-upload` defaults to the shared Drive import; `?source=computer` opens
the local picker. Organizer → Upload vendor documents embeds this same page.
An Organizer sign-in can request a scoped upload session through the authenticated
`POST /api/vendor-uploads/session`; passwords are never sent to the page.
Clients choose and persist an upload ID before registering through `POST /begin`.
Registration is idempotent; `PUT /{id}/chunk?offset=...` accepts only the current
confirmed offset, and `POST /{id}/complete` starts one import after all bytes arrive.
Lost chunk acknowledgements reconcile against the status endpoint before retrying;
neither retries nor double completion append duplicate bytes or start two workers.
After a page reload, choose the same unchanged ZIP to resume its transfer. An idle
unfinished transfer can be cancelled to release its disk file and upload slot.
Signed upload sessions renew without changing their owning nonce, including a
24-hour renewal grace for a recently expired signed proof. A fresh dashboard link
does not replace the scope owning an unfinished transfer in that tab.
Progress polling retries empty/502/503/504 replies.
The scoped token and job ID stay in tab session storage so reloads can resume
tracking a background import. Server redeploys still lose in-memory job progress;
previously committed records remain in PostgreSQL and duplicate retries are safe.

## PostgreSQL document and catalogue storage

Set `DATABASE_URL` privately in Render, using a PostgreSQL URL with TLS enabled,
then deploy. The embedded Vendor dashboard and Organizer's catalogue approval
and sync features use this database. Metadata and original document bytes are
stored together in PostgreSQL. The `psycopg[binary]` driver is included in the
Organizer requirements.

This setting does not migrate files already uploaded to local storage. Export
and verify a full Vendor backup before switching an app that contains local
records, then restore it explicitly into the new database if needed.

Organizer's extraction progress, raw cache, generated folders, logs and DuckDB
processing database remain local; `DATABASE_URL` does not persist them. A paid
disk is still required to preserve that workspace across redeployments.

## Connect one public Google Drive folder

### Shared Drive library (originals stay in Drive)

Open **Google Drive library** in Organizer or **Search all Google Drive data**
in Vendor. These open the same authenticated `/drive-library`, backed by three
additive PostgreSQL tables (`vdd_drive_entries`, `vdd_drive_records`,
`vdd_drive_jobs`). Existing stored documents are preserved. Originals are not
copied into PostgreSQL. A temporary single-document copy is removed after
indexing. Library rows include their original Drive URL, archive member, sheet,
row/page and review status. They do not automatically become approved catalogue
offers or verified vendor records. Different sources may contain repeated rows.

**Index all files / resume** discovers the connected folder, reads ZIP directories
using bounded HTTPS byte ranges (including ZIP64 archives), then extracts supported
documents one at a time. This avoids downloading entire multi-gigabyte ZIPs.
The range reader refuses a server that returns the whole file instead of the
requested range. ZIP directories are limited to 16 MiB reads and 5,000 entries.
Documents remain limited to 128 MiB. Original upload/import guards are unchanged.

XLSX extraction streams XML rows and uses temporary SQLite for shared strings,
instead of loading entire workbooks into a DataFrame. CSV, XLS, PDF text, DOCX,
TXT/log and JSON lines can also be indexed. PDF image-only pages and image files
are marked as needing OCR. Native Google documents, 7z and nested ZIP containers
are shown as unsupported; unsafe/encrypted/oversized members are reported.
`finished_with_issues` never means that every file's content was extracted.

Cloud Vendor startup no longer automatically backfills old document bytes when
the published catalogue is empty. Existing cloud files can be explicitly
re-imported or indexed through this library; local legacy migration is retained.

Rows commit in batches of 200. Resume skips unchanged completed archive members
and idempotently retains saved rows after interruption. Loose public files are
re-fetched and SHA-256 checked on an explicit refresh. A PostgreSQL advisory lock
prevents two indexers processing a folder concurrently. Worker execution runs in
the app process; after a server restart, press Resume. The rows, inventory and
progress remain in PostgreSQL. The preview pauses near 850 MiB database usage,
leaving headroom under the configured 1 GiB quota. Larger extracted indexes can
still require more database storage. This is an explicit refresh, not a continuous
Drive mirror. Deleted Drive sources are not automatically removed from the index.

The library uses the Organizer dashboard token or a short-lived signed token
issued after Vendor sign-in. The latter is passed in a URL fragment, removed
immediately by the library page and sent in a request header. Keys and credentials
are never included in the library index. A Vendor backup/reset currently covers
the existing Vendor document store; the derived Drive library can be rebuilt
independently from its originals and is not included in those backup archives.

Set `GOOGLE_DRIVE_FOLDER_URL` in Render's private environment settings. Both
dashboards use this source. When no valid Google credential file exists, the
app reads publicly shared folder listings and downloads through Google HTTPS
endpoints without a Google sign-in, service-account key, or saved browser cookies.
Keep public folder permissions at Viewer unless editing is actually intended.

In Organizer, open **Import files → Google Drive**. **Scan folder** refreshes
supported original documents (up to 128 MB each); downloaded content hashes
detect edits even when names are unchanged. ZIP files appear in the inventory
as unsupported containers until explicitly imported. **Show files in connected
folder** lists ZIP choices; **Import ZIP into Organizer** downloads and extracts
one archive into uploads, then selects that local source. Read files and use
**Create folders & sync to Vendor** to share originals and catalogue records.
ZIP imports allow 2 GiB download, 5 GiB expanded, 5,000 entries, and 128 MB per
document. Unsafe paths are skipped and reported. Nested ZIPs remain containers.

In Vendor, choose **Upload documents → Google Drive ZIP** and select an archive
from **Choose ZIP from connected Drive folder**, then **Save documents**. This
uses existing Vendor deduplication and archive guards. Folder listings refresh
after 30 seconds. Importing is explicit; the app does not continuously mirror
Drive changes or write results back to Drive. Vendor writes are shared through
the configured PostgreSQL database. Organizer's processing workspace remains
temporary on a free service. Native Google Docs/Sheets require authorized Drive
access; public mode lists them as unsupported rather than silently omitting them.

## Connect one private Google Drive folder

The app already supports read-only private Drive folder scans. The Drive home
page and a Google account email do not identify or authorize a folder.

For a server deployment, use the existing service-account option:

1. In a Google Cloud project you control, enable Google Drive API and create a
   service account with a JSON key. Keep the key private.
2. In the desired Google account, share the specific source folder with the
   service account's email as **Viewer**. Do not make the folder public.
3. In Render's **Environment → Secret Files**, add
   `google-service-account.json` containing the key JSON. It is mounted at
   `/etc/secrets/google-service-account.json`; it is not part of the Docker image.
4. Set `GOOGLE_SERVICE_ACCOUNT_FILE=/etc/secrets/google-service-account.json`
   and `GOOGLE_DRIVE_FOLDER_URL` to the actual folder link, then **Save and
   deploy**. The deployed preview already sets the service-account file path;
   the key and folder still need to be supplied.
5. Open Organizer, enter your private `DASHBOARD_TOKEN`, choose Google Drive on
   **Source & files**, and scan the folder. Use a small synthetic folder first.

The code requests `drive.readonly`. It reads source files without moving,
modifying or deleting them. Its cached copies and generated folders live on the
server. It does not write organized results back to Google Drive.
Everyone with the dashboard token shares this Drive connection.

OAuth is also supported: create a Desktop OAuth client and run
`python -m app drive-auth` locally, authorizing the intended account once.
Treat the generated token JSON as a secret. Configure `GOOGLE_TOKEN_FILE` to a
writable private token file; the app refreshes and writes this file, so a
read-only secret mount cannot be used directly for OAuth token persistence.

Live Drive access cannot be verified until the actual folder is shared and
credentials are configured. Connection in a chat does not connect the deployed
server. Never put keys, tokens or Google passwords into the repository or chat.
