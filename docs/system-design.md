# System design

## Current application

- `app.py` is the Streamlit entry point. It renders the document-management,
  catalog-setup, and sales-assistant workflows.
- `storage.py` is the persistence boundary. It currently uses direct SQL with
  SQLite for local use or PostgreSQL when `DATABASE_URL` is configured.
- Document bytes are stored on local disk with SQLite and in PostgreSQL byte
  columns in the current cloud mode. Catalog search indexes extracted records
  in application code.
- Access currently uses shared administrator and sales passwords. There is no
  organization, user-membership, REST API, ORM, or migration framework.

## Proposed hosted model

The target is a multi-tenant version of the existing vendor-document workflow,
not a rewrite of the Streamlit interface:

- PostgreSQL remains the application database. Supabase Auth owns identities;
  application profiles reference Auth user UUIDs rather than duplicating
  passwords or session state.
- Supabase Storage holds private document and upload-archive objects. The
  database stores object keys and validated metadata, not file contents.
- Organizations own vendors, aliases, upload batches, documents, catalog
  entries, review items, invitations, and audit events. Memberships associate
  Auth users with organizations and roles.
- The server resolves the authenticated user and active organization for each
  operation. Every tenant query is scoped to that organization. PostgreSQL row
  level security is an additional boundary for user-scoped database access;
  privileged service credentials must never be exposed to the browser.
- Schema changes are planned as versioned PostgreSQL SQL migrations. The
  current codebase has no ORM, so this design uses SQL rather than introducing
  an ORM solely to express the schema.

## Migration and rollout constraints

This is a design proposal only. Do not apply it to a live database until the
schema is approved and a migration/backfill plan is reviewed. Existing records
have no organization owner, so a migration must explicitly assign them to a
chosen initial organization, preserve document/object references, verify row
counts and checksums, and retain a restorable pre-migration backup.

Supabase project credentials, storage bucket policy, authentication redirect
URLs, and database connection settings are deployment inputs. They must be
configured outside source control. Production changes and restore tests require
explicit environment access and must not be simulated or reported as completed.
