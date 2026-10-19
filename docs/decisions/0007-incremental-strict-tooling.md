# 0007: Strict checks for new Python scaffold code

Status: accepted for requested scaffold, 2026-10-19.

## Context

The user requests strict language checking, a linter and a formatter. Existing root Python files are not uniformly typed or formatted. Retrofitting all legacy code would turn a scaffold into a broad refactor.

## Decision

Add development-only Ruff and mypy, keeping pytest. Configure Ruff lint/format and mypy strict for `vendor_dashboard/` and `asgi_app.py`. Keep existing behavior tests and full-repository collection visible. Add focused CI for scaffold quality and root regression tests without altering the existing full CI collection scope.

## Alternatives considered

- TypeScript strict mode: inapplicable without rewriting the Python application.
- Black plus a separate linter: Ruff supplies both requested functions with fewer tools.
- Require all legacy modules to pass strict checking immediately: deferred to focused follow-up changes.

## Consequences

Passing strict checks is explicitly scoped; it is not a claim that the entire repository is typed. New package code must stay in the checked surface. Development dependencies are separate from runtime image dependencies. Existing `data-organizer` collection failures remain reported, not hidden. See [architecture](../architecture.md).
