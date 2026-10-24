# Organizer preview on Render

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

In hosted Vendor uploads, **Upload ZIP directly** is the default and opens the
browser file picker. The local ZIP path option is available only when
`CLOUD_DEPLOYMENT` is disabled. Hosted sessions with an old local-path selection
return to direct upload automatically.

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
