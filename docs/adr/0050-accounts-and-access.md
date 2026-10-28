# ADR-0050: Accounts and access

- **Status:** Proposed
- **Date:** 2026-10-28
- **Deciders:** Opseval maintainers (owner request 2026-10-28)

## Context

`opseval serve` has one shared bearer token and answers on loopback only
([ADR-0031](0031-securing-the-console-api.md)). The
check lives in `server/app.py`: a non-loopback `Host` gets 403 `HOST_NOT_ALLOWED`, and
`serve --host` refuses anything but loopback. That is right for one engineer on a laptop.

Server mode (ADR-0047) is for a team: a QA lead adds scenarios, a product owner reads results,
CI reports runs, an admin promotes `production`. That needs people, roles, keys and a server
reachable beyond the local machine. ADR-0047 step 3 listed the parts. This ADR fixes them,
scoped to the hierarchy organisation → project → task (ADR-0049).

GOVERNANCE.md puts SSO enforcement, fine-grained RBAC, SCIM and audit logs in `ee/`. Accounts,
roles, API keys and OIDC login are core.

## Decision

### Tables (core)

- `users`: email (unique, case-folded), `password_hash` (argon2id, nullable for OIDC-only
  users), `disabled_at`.
- `identities`: `(issuer, subject)` unique, linked to a user. Email is never the join key.
- `sessions`: a random token, stored only as its hash, with `user_id`, a CSRF secret, created,
  last-seen and expiry times.
- `org_memberships`: `(organization_id, user_id, role)`, role one of `owner`, `admin`,
  `member`, `viewer`.
- `project_memberships`: may set a role for one project that is **lower** than the user's org
  role. Raising it above the org role is `ee/`.
- `api_keys`: per project. A public id `pk-oe-…` and a secret `sk-oe-…` shown once and stored
  only as a hash. Scopes (read, run, write catalogue, move labels), an optional list of tasks
  it may touch, `expires_at`, `revoked_at`, `last_used_at`.
- `catalogue_labels.protected`: a protected label (by default `production`) moves only for
  `admin` and `owner`.
- `audit_events`: who (user or key), what, on which org/project/task, when, from where.
  Written in core for every write and every sign-in. The viewer and export are `ee/`.

### Roles

| Role | Can |
|---|---|
| viewer | read runs, results and the catalogue |
| member | that, plus edit the catalogue, start runs, move unprotected labels |
| admin | that, plus move protected labels, manage members and API keys |
| owner | that, plus delete the organisation and transfer ownership |

Every route checks the role it needs on the org, project and task it touches. A key scoped to
task A gets 404 for task B, not 403, so it cannot learn what exists.

### Signing in

- **Passwords:** argon2id (argon2-cffi), with parameters stored in the hash so they can be
  raised later. Login is rate-limited per account and per address.
- **Sessions:** a `__Host-` cookie, `Secure`, `HttpOnly`, `SameSite=Lax`, no `Domain`. A new
  session id is issued at login, and the old one is revoked. Logout deletes the row.
- **CSRF:** a write needs both a same-origin `Origin` and a token header derived from the
  session's CSRF secret. Requests authenticated by an API key carry no cookie and are exempt.
- **OIDC:** authorisation code with PKCE, `state` and `nonce`, from an issuer on an allowlist
  (`OPSEVAL_*` settings, the issuer host also on `OPSEVAL_ALLOWED_HOSTS`, TLS). An unknown
  issuer is refused before any redirect.
- **First users:** sign-up is off. `opseval admin create-user --org o --role owner` creates
  users headlessly. Invitations come from an admin in the console.

### Serving beyond loopback

- `opseval serve` keeps ADR-0031's loopback token mode unchanged when no database is
  configured, or when it binds to loopback.
- A non-loopback bind is refused (exit 3) unless `--public-url https://…` is given **and**
  either a TLS certificate and key or `--trusted-proxy` naming the proxy addresses whose
  forwarded headers are believed.
- With a public URL, the `Host` check compares against that URL's host, HSTS is sent, and the
  bearer-token shortcut is off: only sessions and API keys authenticate.
- Secrets used here (session signing material, OIDC client secret) are `SecretStr` in
  `config.Settings` and appear in `SECRET_FIELDS`.

### What stays in `ee/`

SSO enforcement (no passwords for an organisation), SCIM provisioning, custom roles, raising a
project role above the org role, and the audit viewer and export.

## Consequences

- CI authenticates with a project key and `opseval run --server`
  ([ADR-0058](0058-ci-cd-anywhere.md)). The exit code still comes from the local verdict; a
  refused key is a harness error (exit 3), never a failed agent.
- The commit guard ([ADR-0008](0008-committed-outcome-guard.md)) is unchanged: the scenario
  flag and the operator's start flag are still both required. No role grants `--allow-commit`.
- Running on a public address becomes possible, and so does misconfiguring one. The bind rule
  makes plain HTTP on a public interface impossible rather than discouraged.
- New dependencies (argon2-cffi, an OIDC/JOSE library) must pass `make audit`.
- Tests cover login, cookie flags, CSRF, role scoping (a viewer cannot write; a key for task A
  cannot read task B), protected labels, and refusing a non-TLS public bind.

## Not decided here

- Multi-factor authentication beyond what an OIDC provider enforces.
- Per-user API keys (keys are per project here, as in Langfuse).
- Email delivery for invitations and password resets.
