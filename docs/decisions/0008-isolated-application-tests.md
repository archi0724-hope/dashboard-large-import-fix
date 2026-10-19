# 0008: Isolate each application's CI environment

Status: accepted for CI fix, 2026-10-19.

## Context

The root dashboard CI installed only its own requirements, then collected the entire repository. Organizer tests import their separate `app` package and require packages such as python-dotenv that the root app does not use. Root pandas 2.2.3 and organizer pandas 2.3.3 are incompatible pins; their pytest versions also differ. Adding individual missing dependencies to the dashboard would not solve the environment boundary.

## Decision

Keep root CI job ID `test`, limit its collection to the complete root `tests/` suite, and add a separate `organizer` job that installs the unchanged organizer requirements and runs its complete `tests/` suite from `data-organizer`. Each job gets its own Python 3.12 environment, requirements-based pip cache and `pip check`. Install Tesseract, English data and DejaVu fonts in organizer CI to exercise existing OCR tests. Preserve Docker and scaffold quality jobs. Full repository verification means both application suites pass in their own environments, with failures and skips reported separately.

## Alternatives considered

- Merge both requirement lists: rejected because exact pandas/pytest pins conflict and import roots differ.
- Install only python-dotenv into the dashboard job: rejected because collection would still require the rest of the separate application stack.
- Ignore organizer tests: rejected because it would conceal uncovered repository behavior.
- Change pytest collection globally with `testpaths`: unnecessary; explicit CI working directories and commands preserve both suites without changing developer collection semantics.

## Consequences

Organizer dependencies take additional CI time and storage, but pip caching is scoped to its requirements. A plain root `python -m pytest` is no longer the supported repository-wide verification command. Windows development needs two virtual environments; production runtime dependencies and application behavior are unchanged. Missing OCR tools locally can cause skips that must remain visible. Linux runner execution, branch protection for the new `organizer` check and Docker verification still require their own evidence; a local test pass is not a GitHub Actions run. This updates the CI collection approach described when ADR 0007 was first written.
