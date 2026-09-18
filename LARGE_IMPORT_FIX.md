# Large ZIP import fix

Prepared for archi0724/dashboard-vender- using repository source fetched on 2026-09-15.

## Using the updated dashboard

Open Upload documents, leave Google Drive ZIP (large files) selected, paste the ZIP's Drive link, and click Save documents. Keep the tab open until the summary appears. The link must permit a download without signing in; this importer cannot access private account-only files. Do not change sensitive file sharing unless appropriate for your data.

The server downloads in 1 MiB chunks to temporary disk, checks the ZIP, then imports documents individually. Google Drive's large-file confirmation form is handled when supplied. Permission failures, quota errors, incomplete downloads and non-ZIP responses produce a readable error. Temporary files are removed on normal completion or exceptions. Server restarts can interrupt the job; retrying uses existing document deduplication. This is not a durable background job or resumable network transfer.

## Limits and storage behavior

- Drive download: 5 GiB; expanded batch: 5 GiB; 5,000 files; 128 MiB per document. Existing path, encryption, nesting and compression-ratio safeguards remain.
- Browser uploads: 2 GB per file. Large browser uploads are still RAM-backed in Streamlit; use Local ZIP path or Drive when the browser cannot hold the file comfortably.
- Drive originals are not copied into Uploaded ZIPs. Browser ZIP originals are retained only up to 32 MiB. Keep original ZIPs at the source. Extracted documents and checklist records use the existing store.
- Temporary disk must fit the compressed ZIP. Local document storage also needs space for extracted documents. DATABASE_URL continues to govern durable PostgreSQL storage; this change does not make temporary Render disk durable.
- Transfer speed and database writes depend on Drive, server resources and database latency. The patch does not promise an instant import or remove hosting resource limits.

## Validation

The included tests cover streaming without getvalue(), Drive confirmation, redirects, invalid links, download size checks, incomplete payloads, cleanup, duplicate imports and the Drive UI flow, alongside the existing suite. Tests use simulated Drive responses and local SQLite, not the user's vendor data or production PostgreSQL.

A separate local synthetic uncompressed ZIP of 1,080,033,280 expanded bytes (1,030 documents) was iterated successfully with 105.5 MiB peak process RSS. Fixture creation plus archive iteration took 3.45 seconds. This is an archive-reader check, not an end-to-end production benchmark.

## Applying

From a clean checkout of the repository, inspect and apply dashboard-large-import.patch:

```sh
git apply --check /path/to/dashboard-large-import.patch
git apply /path/to/dashboard-large-import.patch
python -m pip install -r requirements.txt pytest
PYTHONPATH=. python -m pytest tests -q
git add app.py vendor_core.py import_service.py drive_import.py requirements.txt .streamlit/config.toml tests/test_app.py tests/test_drive_import.py LARGE_IMPORT_FIX.md
git commit -m "Stream large Drive ZIP imports through disk and bound upload memory"
git push origin main
```

The package also contains replacement source files. If git apply reports a conflict, reconcile against the latest repository rather than overwriting newer work. GitHub reported push:false for this session; no remote commit or deployment was performed. Render runtime verification still needs the selected workspace and a deployment of this change.

Streamlit uploader behavior: https://docs.streamlit.io/knowledge-base/using-streamlit/where-file-uploader-store-when-deleted
