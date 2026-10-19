# 0005: Managed identity with application-owned tenant authorization

Status: target design; provider integration spike and role requirements pending, 2026-10-19.

## Context

The existing shared password protects one dataset. Independent customer organisations cannot safely share it. Users, exact role powers, invitation flows and revocation requirements are not confirmed.

## Decision

Retain current authentication in the scaffold. Target managed OIDC through Streamlit, provisionally Clerk as issuer, with issuer/subject identities and PostgreSQL organisation memberships. Validate OIDC callback, logout, provisioning and revocation in an isolated spike. Enforce tenant checks in every service, file, export, job and cache path before customer launch. Owner/member are provisional roles, not implemented permissions.

## Alternatives considered

- Shared team password for SaaS: rejected because it provides neither individual attribution nor customer isolation.
- Custom password/MFA/recovery stack: rejected due to security and support burden.
- Provider-only role claims: insufficient for application-owned membership revocation and reliable server-side tenant scoping.

## Consequences

Identity integration alone does not authorize data access. Existing global deduplication/reset must be redesigned under separately reviewed tenant migrations. No customer access, auth dependency, secret or account is added here. Billing, SSO contracts, regions and compliance obligations remain open. See [architecture](../architecture.md).
