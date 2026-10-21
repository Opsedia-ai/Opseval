# ADR-0047: Server mode on Postgres, the Langfuse model

- **Status:** Proposed
- **Date:** 2026-10-21
- **Deciders:** Opseval maintainers (the owner chose the database and the Langfuse model on
  2026-10-21; ATTENTION D14)

## Context

Opseval stores everything in files. The catalogue is a folder of YAML, runs are folders under
`reports/`, and the console (ADR-0029) is a loopback-only view over both. That suits one
engineer and CI. It does not suit a team in which a QA lead or a product owner adds scenarios
from the console (D14), several people share one instance, and a release manager wants to know
which version of a scenario ran.

The owner asked to move to a database and to follow Langfuse's model. Langfuse self-hosts as:

- **two application containers**: web, for the UI and the API, and an asynchronous worker;
- **Postgres** for transactional data, **ClickHouse** for high-volume traces and scores,
  **Redis/Valkey** for the queue and cache, and **S3/blob storage** for raw events and large
  objects;
- **organisation → project tenancy**, with project API keys (a public and a secret key);
- **prompts with immutable versions and labels**. `production` is what an SDK gets when it names
  no label, `latest` is maintained automatically, and protected labels are set only by admins.

The architecture document (doc 05) already planned server mode on Postgres, an object store and
workers, sharing one pipeline with local mode through a storage interface (ADR-0005). This ADR
fixes how that is built, and in what order.

## Decision

### The model, mapped

| Langfuse | Opseval server mode |
|---|---|
| web container (UI + API) | `opseval serve`: the console API (ADR-0029/0031) on the database |
| worker container | `opseval worker`: runs suites taken from the queue |
| Postgres (transactional) | Postgres: tenancy, users, keys, the catalogue, runs, verdicts |
| ClickHouse (traces, scores) | **Not used.** Evaluation volumes are tens of runs a day, not hundreds of events a second (doc 05 §4), and Postgres keeps self-hosting one database |
| Redis/Valkey (queue, cache) | **Postgres as the queue** (`SELECT … FOR UPDATE SKIP LOCKED`) behind a queue interface, with Redis left as a later implementation if a live event bus needs it |
| S3/blob store | A blob store for run records and reports: a filesystem volume by default, S3-compatible optional |
| organisation → project | the same, with `project_id` on every project-scoped row |
| project API keys (pk/sk) | the same, for CI and the CLI (`opseval run --server …`) |
| prompt versions + labels | **catalogue items** (scenarios, suites, profiles) with immutable numbered versions and labels; `production` by default, `latest` is always the newest version, protected labels for admins and owners |
| headless initialisation | `opseval db init --project org/project` |

### This change: the foundation and the versioned catalogue

- **Postgres through SQLAlchemy 2 (async) and asyncpg.** It is an optional `server` extra, so
  the base install, the CLI and the container image (ADR-0038) stay free of it, and an
  import-linter contract keeps the whole run path from importing it. The driver is asyncpg
  (Apache-2.0) because psycopg is LGPL, which the dependency licence allowlist does not admit.
- **Migrations are Alembic, configured in code and shipped in the package.** `opseval db
  upgrade` migrates an installed Opseval's database without a checkout or an `alembic.ini`. The
  version table is `opseval_schema_version`, so the database can be shared. A test compares the
  migrations with the models, so they cannot drift (as `opseval schemas --check` does for the
  JSON Schemas).
- **`OPSEVAL_DATABASE_URL` is a secret, and its host is allowlisted** like every other
  destination (FR-SAF-01). Postgres has no TLS-only URL scheme, so a host other than localhost
  needs `ssl=require`, `verify-ca` or `verify-full`; `prefer` and `allow` can fall back to plain
  text, so they are refused. Errors name only the scheme and host.
- **The catalogue is stored the way Langfuse stores prompts.**
  - `opseval catalogue push FOLDER --project org/project [--label L]` checks the folder with
    the loader's own code, exactly as `opseval validate` does. A scenario a suite includes is
    checked through that suite, with its defaults applied. One problem anywhere stores nothing.
  - Each changed file becomes a new version; the file is kept exactly as written, comments
    included. Pushes to one project are serialised, so version numbers never collide.
  - A push with `--label` moves the label to every pushed item, and takes it off items the folder
    no longer has, so a label always names one consistent catalogue.
  - `catalogue label` promotes one version, `history` and `list` show where each label points,
    and `latest` cannot be set by hand.
- **The database adds storage and history, never a second loader.**
  - `opseval catalogue pull FOLDER --label production` writes a label back out as an ordinary
    folder, which `opseval run` and `opseval validate` take as they take any other.
  - It writes only into a new or empty folder: a stale file left there would otherwise run as
    part of the label.
  - Stored paths are checked again on the way out, so nothing written to the database by other
    means can place a file outside the folder.
- **Hardening on the way in.** YAML anchors and aliases are refused, because a few hundred bytes
  of nested aliases expand to gigabytes once stored. Files over 256 KB, links pointing outside
  the folder and folders without a `profile.yaml` are refused too.
- **Development and CI run a real Postgres.**
  - `make db-up` starts the compose service on 127.0.0.1 only.
  - The database tests skip on a laptop without `OPSEVAL_TEST_DATABASE_URL`, but fail in CI. There
    a pinned Postgres service is part of the `check` job and of the release workflow, so they
    can never be skipped silently where they are the proof.

### The steps that follow, in order

1. **The console on the database catalogue.** `serve --database-url` lists, edits and labels
   versions; the editor's save becomes a new version. Then the **guided scenario form** (D14),
   built on the profile-aware defaults and the per-check settings it needs (I15).
2. **Runs in the database.** A `RunStore` implementation (ADR-0005 amendment) keeps runs,
   attempts, scores and verdicts in Postgres and records and reports in the blob store.
   `opseval runs import` moves existing run folders in.
3. **Accounts and access**, in the open-source edition per doc 02:
   - local accounts (argon2), with sign-up off by default and headless initialisation;
   - organisation roles (owner, admin, member, viewer), and protected labels;
   - project API keys, and `opseval run --server URL --suite smoke --label production` for CI;
   - OIDC login.

   SSO enforcement, SCIM, fine-grained RBAC and the audit-log viewer stay in `ee/`.
4. **Workers.** Runs started from the console or the API go onto the Postgres queue, and
   `opseval worker` processes run them. Cancel and live progress come from the database, not
   from threads in the web process.
5. **Deployment.**
   - Compose with web, worker, Postgres and an optional MinIO, then a Helm chart, `/metrics` and
     health checks.
   - Agent credentials stored per project are encrypted with `OPSEVAL_ENCRYPTION_KEY`, as
     Langfuse encrypts integration secrets.

Local mode stays: the CLI on folders and run folders keeps working, and any server-mode
catalogue can be pulled back to a folder for Git or for a CI job with no server.

## Consequences

- One database to operate, not four. The cost is no ClickHouse-scale analytics; if evaluation
  volumes ever demand them, the queue and blob store are interfaces, and an OLAP store can be
  added behind the run store.
- Catalogue files keep their exact text, so a scenario edited in the console and pulled back to
  Git shows a clean diff.
- Two kinds of state in server mode, the database and the blob store, both behind interfaces
  that local mode implements with files.
- Records stay in files for now: this change moves only the catalogue. Runs, accounts and
  workers are the steps above, each its own ADR amendment or ADR.
- Tests need Docker or a Postgres to run the server-mode suite locally (`make db-up`); CI has
  one.
