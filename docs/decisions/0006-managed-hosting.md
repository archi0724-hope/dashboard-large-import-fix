# 0006: Prefer managed hosting and minimal diagnostics

Status: target recommendation; no resources provisioned, 2026-10-19.

## Context

A solo/small team needs repeatable deployments and recoverable storage with little infrastructure maintenance. Existing Community Cloud instructions do not provide a durable worker deployment design.

## Decision

Target one Render web process, one future worker and managed PostgreSQL in the same chosen region, with private external objects and provider logs/metrics. Start with single instances, shared releases and no autoscaling promises. Use redacted logs and request/job identifiers; add an external error tracker only when provider diagnostics are inadequate. Keep billing/email services deferred until their workflows are specified.

## Alternatives considered

- Kubernetes/self-managed VPS: rejected because patching, networking, database recovery and orchestration outweigh the benefit.
- Community Cloud as the complete SaaS platform: retain as an existing UI deployment option, but it does not describe the intended worker topology.
- Multiple regions/active-active databases: deferred until residency and availability requirements justify them.

## Consequences

The architecture budget is provisional and excludes overages, workspace upgrades and additional backup copies. No deployment action is authorized by writing this ADR. Profile imports before selecting actual capacity and run recovery drills before making service commitments. See [architecture](../architecture.md) for pricing sources and limitations.
