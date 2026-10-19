# 0002: Host liveness with native Streamlit ASGI

Status: accepted for scaffold, 2026-10-19.

## Context

The requested `/api/health` route must run beside the existing dashboard. Installed Streamlit 1.63 provides `st.App`, Starlette routes and an ASGI server.

## Decision

Add `asgi_app.py` wrapping existing `app.py`, with an unauthenticated GET/HEAD `/api/health` route returning only status and no-store caching. Use the same server and port. Declare the already-installed Starlette dependency directly. Use the wrapper in Docker/bundles; retain direct UI launch compatibility.

Keep the literal `st.App(...)` construction in the launcher: Streamlit's static CLI discovery does not follow a custom factory. Verify the real server returns JSON rather than accepting the HTML shell's HTTP 200 as success.

## Alternatives considered

- Separate FastAPI process: rejected because one endpoint does not justify another framework, port and lifecycle.
- Streamlit internal health path only: insufficient for the explicitly requested route.
- Import root `app.py` from the health handler: rejected because health must not access protected storage or run UI code.

## Consequences

The endpoint proves process liveness only. It does not prove database readiness or user flows. Existing shared-password protection remains in the UI; any future API data route needs its own authentication/authorization. ASGI startup and session behavior require verification. See [architecture](../architecture.md).
