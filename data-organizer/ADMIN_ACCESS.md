# Administrator login

The combined app starts with an email/password login at /. Normal users choose **Continue as viewer**. The A profile icon in the top-right opens admin sign-in or sign-out. There is one configured administrator and no self-registration.

Set ADMIN_EMAIL and ADMIN_PASSWORD privately in data-organizer/.env for local runs, or in the host environment for deployment. Streamlit secrets take precedence if configured. The local generated credentials are in secrets/admin-credentials.txt, which is excluded from Git. Do not include .env or secrets in deployment source bundles.

Admin can import, approve, edit, change settings and reset data. Viewers can browse, search, download and use the assistant. Export generation remains available. The API rejects all other writes before opening storage; Vendor editing pages and the destructive callback also check the admin session. The old APP_PASSWORD and DASHBOARD_TOKEN gates have been removed from this combined app. No extra upload password is required. Signed upload tokens are internal transport/ownership proofs.

Sessions last eight hours and are revoked by sign-out, credential changes or a server restart. They are kept in memory in the single ASGI process; run one worker. Cookies are HttpOnly and SameSite Strict, and Secure on HTTPS. Login failures are throttled per client address and across the process. Use HTTPS for hosted access.

Forgot password shows a recovery contact instruction; no email is sent. To recover access, change ADMIN_PASSWORD in the private server settings and restart the process.

Run from data-organizer with the existing isolated environment:

    ..\.venv-organizer-ci\Scripts\python.exe -m app serve

Open http://127.0.0.1:8000/. Local changes do not deploy or restart an existing server. The standalone root app.py is a separate application and retains its existing team gate. There are no dependency or schema changes.
