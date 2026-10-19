# 0001: Preserve Python and use a modular monolith

Status: accepted for scaffold, 2026-10-19.

## Context

The root dashboard already has tested import, classification, storage and export modules. The team is small, demand is provisional and no system-design document exists. A rewrite would put working behavior at risk.

## Decision

Keep Python 3.12 and Streamlit 1.63.0. Retain root modules and add `vendor_dashboard` boundaries for web, domain, services, infrastructure and jobs. Only web health composition is implemented now. One codebase and release serves future web and worker processes.

## Alternatives considered

- React/TypeScript rewrite: deferred because it duplicates UI/business integration and introduces another build toolchain.
- Microservices: rejected because there are no independently owned domains or measured scaling needs.
- Immediate move of all root files: deferred to avoid breaking tests, entry points and deployment artifacts.

## Consequences

Current import paths stay compatible. Reserved packages contain no product features. Future code should follow web/jobs -> services -> domain dependencies, with infrastructure adapters at the boundary. Revisit the UI only after measurable limitations. See [architecture](../architecture.md).
