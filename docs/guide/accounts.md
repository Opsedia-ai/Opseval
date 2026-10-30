# Accounts and access (preview)

`opseval serve` has two ways of deciding who may use it ([ADR-0050](../adr/0050-accounts-and-access.md)).
Which one you get depends on what is configured, never on the address it listens on.

| | Shared-token mode | Accounts mode |
|---|---|---|
| When | no database and no public URL | `OPSEVAL_DATABASE_URL` or `--public-url` is set |
| Who | whoever has the token `serve` printed | people who sign in, and project API keys |
| Where | loopback only | loopback, or beyond it behind TLS |
| Roles | none: the token may do anything | owner, admin, member, viewer |

Shared-token mode is the laptop tool of [ADR-0031](../adr/0031-securing-the-console-api.md),
unchanged. The rest of this page is about accounts mode.

!!! note "Preview"
    One `opseval serve` process serves one task of one project (`--project`, `--task`).
    Invitations and password resets by email, and a members page in the console, are not built
    yet: use `opseval admin` or the API.

## Start it

Accounts live in the server-mode database ([Server mode](server-mode.md)), so set that up first.
Sign-up is off: the first owner is made from the command line, where whoever runs it already
holds the database's credentials.

```bash
pip install 'opseval[server]'
export OPSEVAL_DATABASE_URL=postgresql://opseval:…@127.0.0.1:5433/opseval
export OPSEVAL_ALLOWED_HOSTS=127.0.0.1

opseval db upgrade
opseval db init --project acme/support-bot
opseval admin create-user you@example.com --org acme --role owner     # asks for a password
opseval serve --project acme/support-bot --catalogue ./release
```

Open the console and sign in. There is no token in the link: the shared token is off in
accounts mode, and a request carrying one is refused.

A password is never a command-line argument (anyone on the machine can read those): it is asked
for, or read from standard input with `--password-stdin`. It needs at least 12 characters and
is stored as an argon2id hash.

## Roles

A person has one role in an organisation, which applies to all of its projects. A project
membership may only **lower** it for one project (raising it is an Enterprise feature).

| Role | Can |
|---|---|
| viewer | read runs, results and the catalogue |
| member | that, plus edit the catalogue, start and cancel runs, move unprotected labels |
| admin | that, plus move protected labels, manage members and API keys |
| owner | that, plus grant or change `owner` |

```bash
opseval admin set-role ana@example.com --org acme --role member
opseval admin set-role ana@example.com --org acme --role member \
  --project acme/support-bot --project-role viewer     # lower, on this project only
opseval admin set-role ana@example.com --org acme --role none   # remove
```

An organisation always keeps one owner, and nobody changes their own role from the console.
Someone signed in with no role in the served project sees no data, and is told why.

The console shows only what the server says you may do (`GET /api/v1/auth/session` returns your
capabilities). Hiding a button is a courtesy: the server refuses the request either way.

## API keys

Keys belong to a project and are what CI and scripts use. An admin creates them on the console's
**API keys** page, or:

```bash
opseval admin create-key --project acme/support-bot --name ci \
  --scope runs:read --scope runs:start --created-by you@example.com
```

The secret is printed once and stored only as a hash. Send it as
`Authorization: Bearer pk-oe-…:sk-oe-…` (or HTTP basic auth, public key as the user).

| Scope | Allows |
|---|---|
| `runs:read` | read runs, results and the catalogue |
| `runs:start` | start and cancel runs |
| `catalogue:write` | edit scenarios, suites and imports |
| `catalogue:label` | move **unprotected** labels |
| `label:protected` | move protected labels such as `production` |

- A key never does more than the person who created it **can do now**: demote them and the key
  loses what they lost, at its next request. A disabled creator's keys hold nothing, and
  deleting a person deletes their keys. Only `opseval admin create-key --system` makes a key
  with nobody behind it, and it holds at most what a member could.
- `label:protected` is never implied by another scope, only an admin or owner can grant it, and
  the grant is an audit event.
- A **system key** (`--system`, for Opseval's own automation) can never hold `label:protected`:
  the API, the CLI and the database all refuse it. Automation never moves `production`.
- `--task` limits a key to some tasks. Asked about any other, it gets 404, not 403.
- `opseval admin revoke-key pk-oe-… --project acme/support-bot` ends a key at once.

A request with an `Authorization` header is judged by that header alone: a wrong, expired or
revoked key is 401 even when the browser also has a valid session.

## Protected labels

`production` is always protected: only an admin or owner, or a key holding `label:protected`,
may move it. An admin can protect other labels with
`PUT /api/v1/catalogue/labels/protection`, by name: the protection holds even before any item
carries the label. A `catalogue:label` key or a member trying to move a
protected label gets 403 `PROTECTED_LABEL`.

## Sessions and CSRF

Behind an https public URL the session is a `__Host-opseval-session` cookie: `Secure`,
`HttpOnly`, `SameSite=Lax`, no `Domain`. On plain-http loopback (no https public URL), where a
`Secure` cookie is dropped by some browsers and a `__Host-` name requires `Secure`, it is
`opseval-session`: still `HttpOnly`, `SameSite=Lax` and host-only, without `Secure`. The OIDC
sign-in's cookie follows the same rule (`__Host-opseval-oidc` or `opseval-oidc`). A new one is issued at every sign-in and the old one ends; signing out deletes it.
It ends after `OPSEVAL_SESSION_IDLE_MINUTES` (120) unused, and `OPSEVAL_SESSION_MAX_HOURS` (24)
after sign-in in any case. Only a hash of it is stored.

Every write made with the cookie needs a same-origin `Origin` and the session's
`X-CSRF-Token` (the console sends it). Requests with an API key are exempt.

Sign-in is rate-limited per account and per address, with a growing wait after a few failures
(429 with `Retry-After`). Each attempt is counted before its password is checked, so a burst of
parallel sign-ins gets no more tries than the allowance. Starting an OIDC sign-in is limited
per address too, and a sign-in not finished within ten minutes is dropped. The limiter is per process: see the note under deployment.

## Single sign-on (OIDC)

```bash
export OPSEVAL_OIDC_ISSUER=https://accounts.example.com
export OPSEVAL_OIDC_CLIENT_ID=opseval
export OPSEVAL_OIDC_CLIENT_SECRET=…            # a secret, redacted like every other
export OPSEVAL_ALLOWED_HOSTS=127.0.0.1,accounts.example.com
```

Register `https://<your public URL>/api/v1/auth/oidc/callback` as the redirect URI at the
identity provider. Sign-in uses the authorization code flow with PKCE, `state` and `nonce`; the
ID token's signature (asymmetric algorithms only), issuer, audience, expiry and nonce are
checked. The issuer must be allowlisted and https, or the server refuses to start; every
endpoint its discovery document names must be too, or the sign-in is refused before any
redirect.

The issuer is compared exactly, as the identity provider names itself: `https://idp/` and
`https://idp` are different issuers, so copy it from the provider's discovery document.

Email is never used to match a sign-in to a user. Link a person to their identity when creating
them, later from the command line, or from the members API (`oidcSubject`):

```bash
opseval admin create-user ana@example.com --org acme --role member --no-password \
  --oidc-subject 00u1abcd…          # the ID token's `sub`
opseval admin link-identity bo@example.com --oidc-subject 00u1efgh…   # an existing user
```

A sign-in with an identity nobody linked is refused, and its audit event records the subject so
an admin can link it.

## Beyond loopback

`opseval serve` refuses (exit 3) to listen on anything but loopback unless all of these hold:
accounts mode, `--public-url https://…`, and either a certificate or a trusted proxy.

```bash
# TLS in opseval itself
opseval serve --project acme/support-bot --host 0.0.0.0 \
  --public-url https://opseval.example.com --tls-cert cert.pem --tls-key key.pem

# Behind a reverse proxy that terminates TLS
opseval serve --project acme/support-bot --host 0.0.0.0 \
  --public-url https://opseval.example.com --trusted-proxy 10.0.0.0/24
```

- `Host` must be the public URL's host, and HSTS is sent.
- `Forwarded` and `X-Forwarded-*` are read only from the `--trusted-proxy` addresses
  (`OPSEVAL_TRUSTED_PROXIES`), and ignored from anyone else. The forwarded host must be the
  public host and the forwarded protocol `https`, or the request is 403 `HOST_NOT_ALLOWED`.
  A request that bypassed the proxy over plain http is refused the same way.
- The client address in audit events and rate limits is the one the trusted proxy reports.

!!! note "Several processes"
    Sign-in rate limits and OIDC sign-ins in flight are kept in each process's memory. Behind a
    load balancer with several `opseval serve` processes, use sticky sessions for `/api/v1/auth/oidc/*`,
    and know that each process allows its own sign-in attempts.

## Audit events

Every sign-in (and failed sign-in), sign-out, key created or revoked, role changed, label moved,
and every other successful write is a row in `audit_events`: who (user, key, or `cli`), what,
on which organisation, project and task, when, and from which address. Viewing and exporting
them in the console is an Enterprise feature; the rows are yours in the database.
