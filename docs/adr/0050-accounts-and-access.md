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
  only as a hash. Scopes (below), an optional list of tasks it may touch, `system` (a key
  Opseval's own automation uses), `expires_at`, `revoked_at`, `last_used_at`.
- `catalogue_labels.protected`: a protected label (by default `production`) moves only for
  `admin` and `owner`, or for a key holding `label:protected`.
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

### API key scopes

| Scope | Allows |
|---|---|
| `runs:read` | read runs, results and the catalogue |
| `runs:start` | start and cancel runs, report a CI run |
| `catalogue:write` | push catalogue versions |
| `catalogue:label` | move **unprotected** labels only; a protected label is refused (403) |
| `label:protected` | move protected labels, such as `production` |

- **`label:protected` is off by default** and is never implied by another scope.
- **Only an org admin or owner can grant it,** and the grant is an audit event.
- **A system key can never hold it.** Keys used by schedules, workers, proposals and regression
  rules ([ADR-0064](0064-continuous-evaluation.md)) are system keys, and the grant is refused.
  Automation never moves `production`.
- A key never has more than the role of the person who created it, checked at each use.

### Signing in

- **Passwords:** argon2id (argon2-cffi), with parameters stored in the hash so they can be
  raised later. Login is rate-limited per account and per address.
- **Sessions:** a `__Host-` cookie, `Secure`, `HttpOnly`, `SameSite=Lax`, no `Domain`. A new
  session id is issued at login, and the old one is revoked. Logout deletes the row.
- **CSRF:** a write needs both a same-origin `Origin` and a token header derived from the
  session's CSRF secret. A request authenticated by an API key is exempt.
- **A key never falls back to a cookie.** A request with an `Authorization` header is
  authenticated by that header alone. An invalid, expired or revoked key is 401, even when the
  same request carries a valid session cookie.
- **OIDC:** authorisation code with PKCE, `state` and `nonce`, from an issuer on an allowlist
  (`OPSEVAL_*` settings, the issuer host also on `OPSEVAL_ALLOWED_HOSTS`, TLS). An unknown
  issuer is refused before any redirect.
- **First users:** sign-up is off. `opseval admin create-user --org o --role owner` creates
  users headlessly. Invitations come from an admin in the console.

### Two modes, chosen by configuration

The mode is decided by what is configured, never by the bind address:

- **Accounts mode** is on when `OPSEVAL_DATABASE_URL` (ADR-0047) or a public URL (`--public-url`) is
  configured. It has users, sessions, roles, CSRF and API keys, also on loopback. The shared
  bearer token is off: only sessions and API keys authenticate.
- **Shared-token mode** (ADR-0031) is only for file mode, with no database and no public URL. It
  stays as it is: loopback only, one bearer token.

Serving beyond loopback:

- A non-loopback bind is refused (exit 3) in shared-token mode, and in accounts mode unless
  `--public-url https://…` is given **and** either a TLS certificate and key or
  `--trusted-proxy` naming the proxy addresses whose forwarded headers are believed.
- With a public URL, the `Host` check compares against that URL's host, and HSTS is sent.
- **Behind a trusted proxy,** `Forwarded` and `X-Forwarded-*` headers are read only from the
  listed addresses and ignored from anyone else. The forwarded host must equal the public URL's
  host and the forwarded proto must be `https`, or the request is 403 `HOST_NOT_ALLOWED`. The
  client address for rate limits and audit events is the one the trusted proxy reports.
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
  cannot read task B), protected labels (`catalogue:label` cannot move `production`; a system
  key cannot be granted `label:protected`), a bad key with a good cookie (401), forwarded
  headers from an untrusted address, the mode chosen by configuration (a database URL on
  loopback is accounts mode), and refusing a non-TLS public bind.

## Not decided here

- Multi-factor authentication beyond what an OIDC provider enforces.
- Per-user API keys (keys are per project here, as in Langfuse).
- Email delivery for invitations and password resets.

## Implementation note (2026-10-30)

Built on `feat/accounts`; the status stays **Proposed** until the maintainers accept it. Where
the build had to choose, it chose as follows.

- **One served task per process.** `opseval serve --project org/project --task t` serves one
  task, and every principal is resolved for that project and task. In accounts mode the run
  store's reads are scoped to the task too (`DbRunStore(task_reads=True)`), so a key scoped to
  another task, or a person with no role in the project, gets 404 on every data route. A task
  switcher in one process is later work (it needs the API's routes to carry the task).
- **The mode.** A database (`OPSEVAL_DATABASE_URL` or `--database-url`) or a public URL
  (`--public-url`, `OPSEVAL_PUBLIC_URL`) is accounts mode. Accounts mode needs both a database
  (the users live there) and `--project`; either missing is exit 3 naming the fix. Nothing else
  changed in shared-token mode, which imports none of the accounts code.
- **One decision.** Every `/api` route is listed in `server.app.ROUTE_NEEDS` with what it
  needs (`public`, `signed_in`, a scope, or `manage`), and one dependency applies
  `accounts.roles.decide` to it. A route missing from the table fails the start, and a request
  reaching one anyway is refused (`NO_POLICY`). A test sends an anonymous request to every
  listed route and expects 401 from all but the sign-in ones.
- **Keys.** `Authorization: Bearer pk-oe-…:sk-oe-…`, or HTTP basic auth with the public key as
  user (as Langfuse's SDKs send it). The secret is 256 random bits stored as its sha256. A key's
  scopes are cut to its creator's **current** role at every use; a key whose creator is gone or
  disabled holds nothing. `manage` (members, keys) is never a key's. The database refuses a
  system key with `label:protected` (`ck_api_keys_system`), as the API and CLI do.
- **Sessions** need no signing secret: the cookie carries 256 random bits, the row only their
  hash. The CSRF token is an HMAC of the session id under a per-session secret, so a token is
  worthless for another session. Idle expiry `OPSEVAL_SESSION_IDLE_MINUTES` (120), absolute
  `OPSEVAL_SESSION_MAX_HOURS` (24). `last_seen_at` is written at most once a minute.
- **Protected labels.** `production` is always protected. An admin can protect another label
  (`PUT /api/v1/catalogue/labels/protection`); the flag is kept on the label's rows and a moved
  label keeps it. The API gained the label routes (`GET`/`PUT /api/v1/catalogue/labels`) so the
  rule has an endpoint; `opseval catalogue label`, run with the database's own credentials,
  is not gated.
- **OIDC.** One issuer (`OPSEVAL_OIDC_ISSUER`, `_CLIENT_ID`, `_CLIENT_SECRET`, `_SCOPES`), its
  host and every endpoint its discovery names allowlisted and https. Sign-up is off and email is
  never the join key, so an identity is linked with `opseval admin create-user --oidc-subject`;
  a refused sign-in's audit event carries the issuer and subject for the admin to link. The
  flow's state, nonce and PKCE verifier are kept in memory, bound to the browser by a
  `__Host-opseval-oidc` cookie, for ten minutes.
- **Audit.** Sign-ins (and failures), sign-outs, key creation (with `grants_label_protected`)
  and revocation, role changes, member creation and label moves are written in the same
  transaction as the change. Every other successful write is recorded after it as
  `"<METHOD> <route>"`; if that write fails it is logged loudly, not undone.
- **The bind rule.** Non-loopback needs accounts mode, `--public-url https://…`, and
  `--tls-cert`/`--tls-key` or `--trusted-proxy`; an https public URL with neither is refused on
  loopback too. uvicorn's own forwarded-header handling is off (`proxy_headers=False`): it would
  believe loopback. With `--trusted-proxy`, a request that did not come through a listed proxy
  must itself be https.
- **Rate limiting** is per process, in memory (`accounts.ratelimit`): several processes behind
  a load balancer each count on their own. Named in `docs/ATTENTION.md`.

Not built here: invitations and password reset by email (no mail delivery), a members page in
the console (the API and `opseval admin` exist), `opseval run --server` (ADR-0058 E2), and the
`ee/` parts listed above.
