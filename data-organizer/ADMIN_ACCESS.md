# Administrator login

The combined app starts with an email/password login at /. Normal users choose **Continue as viewer**. The A profile icon in the top-right opens admin sign-in or sign-out. There is one configured administrator and no self-registration.

Set ADMIN_EMAIL and ADMIN_PASSWORD privately in data-organizer/.env for local runs, or in the host environment for deployment. Streamlit secrets take precedence if configured. The local generated credentials are in secrets/admin-credentials.txt, which is excluded from Git. Do not include .env or secrets in deployment source bundles.

Admin can import, approve, edit, change settings and reset data. Viewers can browse, search, download and use the assistant. Export generation remains available. The API rejects all other writes before opening storage; Vendor editing pages and the destructive callback also check the admin session. The old APP_PASSWORD and DASHBOARD_TOKEN gates have been removed from this combined app. No extra upload password is required. Signed upload tokens are internal transport/ownership proofs.

Sessions last eight hours and are revoked by sign-out, credential changes or a server restart. They are kept in memory in the single ASGI process; run one worker. Cookies are HttpOnly and SameSite Strict, and Secure on HTTPS. Login failures are throttled per client address and across the process. Use HTTPS for hosted access.

Forgot password shows a recovery contact instruction; no email is sent. To recover access, change ADMIN_PASSWORD in the private server settings and restart the process.

Run from data-organizer with the existing isolated environment:

    ..\.venv-organizer-ci\Scripts\python.exe -m app serve

Open http://127.0.0.1:8000/. Local changes do not deploy or restart an existing server. The standalone root app.py is a separate application and retains its existing team gate. There are no dependency or schema changes.

## Hostinger Mail password

Set ADMIN_AUTH_PROVIDER=hostinger and ADMIN_EMAIL to the one permitted mailbox. The login form sends its password over HTTPS to this app; the server verifies it with Hostinger at imap.hostinger.com:993 using validated TLS and a 10-second timeout. Only LOGIN and LOGOUT are used; messages are not opened. Mailbox passwords are never persisted, logged or included in session records. Other emails are rejected before contacting Hostinger, and provider failures never fall back to the old dashboard password. ADMIN_PASSWORD is ignored for login in this mode.

The email password must be accepted by Hostinger IMAP. Recover a forgotten password through Hostinger Mail. Existing dashboard sessions last up to eight hours; changing the mailbox password alone does not immediately revoke an already issued dashboard session. Sign out or restart this app to revoke it sooner. Provider or allowed-email configuration changes revoke existing sessions.

Hostinger connection reference: https://www.hostinger.com/support/1575756-how-to-get-email-account-configuration-details-for-hostinger-email/
