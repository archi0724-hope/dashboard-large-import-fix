# 0004: Use one future worker with PostgreSQL jobs

Status: target design; no queue or consumer implemented, 2026-10-19.

## Context

Imports are synchronous and partial writes survive errors. The provisional workload is only 1,200 archives/year, but individual imports can run for minutes and must eventually survive UI disconnects.

## Decision

When durable imports are implemented, use one separate managed worker from the same codebase with concurrency one. Claim jobs atomically with `FOR UPDATE SKIP LOCKED`, leases, heartbeats and commits before processing. Use tenant-scoped idempotency, at-least-once delivery, capped retries, progress checkpoints and explicit partial/failed outcomes. Store input objects before enqueueing. Stop new claims on SIGTERM.

## Alternatives considered

- Celery/Redis: deferred because it adds an operational datastore for a low-volume workload.
- In-process background threads: rejected for durable work because restarts/session lifecycle can abandon tasks.
- One giant import transaction: rejected because it would hold locks and make partial recovery harder across object writes.

## Consequences

No exactly-once claim is made. Retry safety and lease recovery need tests before release. Jobs share database capacity with user queries. The scaffold reserves a package only; current import execution is unchanged. A versioned schema proposal is required for the future table. See [architecture](../architecture.md).
