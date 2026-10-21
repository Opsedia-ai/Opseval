# Server mode (preview)

Server mode keeps the catalogue in Postgres, versioned the way Langfuse versions prompts
(ADR-0047). Each scenario, suite and profile gets numbered, immutable versions, and labels such
as `production` and `staging` point at one version each. You push a catalogue folder in, promote
versions with labels, and pull a label back out as a folder that `opseval run` takes as it is.

Local mode does not change: the CLI on catalogue folders and run folders needs no database.

!!! note "Preview"
    The catalogue is the first part. The console on the database, runs in the database,
    accounts and API keys, and workers follow, in the order ADR-0047 sets out.

## Set up

```bash
pip install 'opseval[server]'      # SQLAlchemy, asyncpg and Alembic
make db-up                         # or any Postgres 14+; listens on 127.0.0.1:5433

export OPSEVAL_DATABASE_URL=postgresql://opseval:opseval@127.0.0.1:5433/opseval
export OPSEVAL_ALLOWED_HOSTS=127.0.0.1

opseval db upgrade                 # create or migrate the schema
opseval db init --project acme/support-bot
```

The database URL is a secret, and its host must be allowlisted like every other destination. A
database host other than localhost must use TLS: add `?ssl=require`, or `verify-ca` /
`verify-full`. `prefer` is refused, because it falls back to plain text.

## Push, label, pull

```bash
# Checked exactly like `opseval validate`; one problem anywhere and nothing is stored.
opseval catalogue push ./my-catalogue --project acme/support-bot --label staging -m "new refund cases"

opseval catalogue list --project acme/support-bot
opseval catalogue history scenario rf-001 --project acme/support-bot

# Promote one version.
opseval catalogue label scenario rf-001 4 production --project acme/support-bot

# Write a label back out (into a new or empty folder) and run it.
opseval catalogue pull ./release --project acme/support-bot --label production
opseval run ./release/suites/smoke.yaml --profile ./release/profile.yaml --target wss://…
```

| Rule | Why |
|---|---|
| An unchanged file adds no version | Pushing the same folder twice changes nothing |
| `--label` moves to every pushed item, and leaves items the folder no longer has | A label always names one consistent catalogue |
| `latest` is always the newest version and cannot be set | It can never be stale |
| `pull` writes only into a new or empty folder | A stale file left there would otherwise run as part of the label |
| YAML anchors and aliases are refused | A few hundred bytes of aliases can expand to gigabytes |

A folder holds `profile.yaml`, `suites/*.yaml` and `scenarios/**/*.yaml`, the layout
`opseval init` writes. Other files are not pushed.
