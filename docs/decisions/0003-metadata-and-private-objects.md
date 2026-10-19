# 0003: Separate future file objects from metadata

Status: target design; migration not implemented, 2026-10-19.

## Context

The provisional scenario produces 234.4 GiB of documents versus approximately 3 GiB of metadata. Current PostgreSQL stores document bytes; current full dashboard backups materialize them in memory.

## Decision

Preserve current SQLite/filesystem and PostgreSQL payload behavior in this scaffold. Before growing to the scenario, implement managed PostgreSQL metadata plus private R2 Standard objects, tenant-scoped keys, checksums and authorized short-lived URLs. Require versioned migration, verified byte retrieval, recovery drills and an approved rollback window before deleting old payloads.

## Alternatives considered

- Keep all bytes in PostgreSQL indefinitely: simpler atomicity but much larger database backups and storage cost.
- Hosted ephemeral filesystem: rejected for durable records.
- AWS S3: viable if residency/provider needs demand it; R2 is the provisional lower-egress-cost choice, not a compliance conclusion.

## Consequences

Future writes across SQL/object storage need idempotency and reconciliation. Object recovery must align with metadata backups and retention. No bucket, SDK, schema change or data migration is introduced now. Residency, retention and malware scanning needs remain unresolved. See [architecture](../architecture.md).
