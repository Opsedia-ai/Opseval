# Server mode (preview)

Server mode keeps the catalogue and the runs in Postgres, the catalogue versioned the way
Langfuse versions prompts (ADR-0047) and each run's files in a blob store (ADR-0051). Each scenario, suite and profile gets numbered, immutable versions, and labels such
as `production` and `staging` point at one version each. You push a catalogue folder in, promote
versions with labels, and pull a label back out as a folder that `opseval run` takes as it is.

Local mode does not change: the CLI on catalogue folders and run folders needs no database.

!!! note "Preview"
    The catalogue and the runs are built. The console's project and task views, accounts and
    API keys, and workers follow, in the order ADR-0047 sets out.

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
database host other than localhost must use verified TLS: add `?ssl=verify-full`, and
`&sslrootcert=/path/ca.pem` when the server's certificate comes from a private CA (as most cloud
providers' do). `require` is refused, because it accepts any certificate. Only the `ssl`,
`sslmode` and `sslrootcert` parameters are accepted.

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
| `latest` is the catalogue as last pushed, and only a push moves it | A scenario deleted from the folder never comes back with it |
| Every file a suite selects must be under `scenarios/` with a `.yaml` name | Otherwise a pulled suite would run fewer scenarios than `validate` saw |
| `pull` writes only into a new or empty folder | A stale file left there would otherwise run as part of the label |
| YAML anchors and aliases are refused | A few hundred bytes of aliases can expand to gigabytes |
| Datasets are stored byte for byte, at most `OPSEVAL_MAX_DATASET_BYTES` (5 MB) each, and pulled only under `datasets/` | A pulled label runs the rows it was validated with (ADR-0060) |

A folder holds `profile.yaml`, `suites/*.yaml` and `scenarios/**/*.yaml`, the layout
`opseval init` writes, plus `datasets/**/*.csv` and `datasets/**/*.jsonl` for scenarios with a
`dataset:` (each an item of kind `dataset`, named by its path). Other files are not pushed.

## Tasks

A project holds **tasks** (ADR-0049). A task is one evaluation target: one agent endpoint (its
profile) and one modality, with its own scenarios, suites and labels. A chat agent and a voice
line can then share one project, with one set of members and keys, and keep separate catalogues.
One catalogue folder is one task.

Every project has a `default` task, and every command uses it when `--task` is left out, so the
commands above work unchanged.

```bash
# Create a task (kind: chat | agentic | voice | batch;
# modality: text | audio_file | audio_stream | telephony | webrtc).
opseval db init --project acme/support-bot --task voice-line --kind voice --modality audio_stream

opseval catalogue push ./voice-catalogue --project acme/support-bot --task voice-line --label staging
opseval catalogue pull ./voice-release --project acme/support-bot --task voice-line --label production

opseval task list --project acme/support-bot            # kind, modality, profile, item count
opseval task update voice-line --project acme/support-bot --default-suite smoke --region eu
opseval task archive voice-line --project acme/support-bot
```

| Rule | Why |
|---|---|
| A task that does not exist is an error naming the project and the slug | A typo in `--task` must not start an empty catalogue |
| Two tasks may each have a scenario with the same id | Each task has its own items and version history |
| A label belongs to one task: `production` is that task's production catalogue | A voice task's release never moves a chat task's |
| A push links the folder's profile as the task's profile | The task knows its endpoint |
| An archived task takes no push and no label move, but can still be listed and pulled | Archiving hides a task; it never deletes anything |
| `db init --task` on an existing task with a different kind or modality is refused | Change it with `opseval task update`, never silently |

When you upgrade from a database made before tasks existed, `opseval db upgrade` gives each
project a `default` task with kind `chat` and modality `text`, and moves its catalogue into it.
If the project has exactly one profile, the upgrade links it. Change the kind or modality
afterwards with `opseval task update default …`.

## Runs in the database

With `--project`, a run is written to the database and its files (the result, each attempt's
record, the report, the JUnit XML and any recorded audio) to the blob store, instead of a run
folder. Without `--project` nothing changes: run folders under `OPSEVAL_REPORTS_DIR`.

```bash
opseval run ./release/suites/smoke.yaml --profile ./release/profile.yaml --target wss://… \
  --project acme/support-bot --task default          # exit codes as always

opseval serve --project acme/support-bot             # the console reads (and starts) runs there
opseval prune --project acme/support-bot --yes       # old runs, old audio, orphaned blobs

# Move existing run folders in (any layout Opseval has written). Run it again safely.
opseval runs import reports/* --project acme/support-bot --task default
```

The rows (runs, attempts, scores, verdicts, per-turn metrics, usage events) are for queries; what
a reader sees is the JSON the run wrote, read back from the blob store. Two rules are in the
schema itself: a score is empty exactly when its status is `infra_error` (a harness problem is
never stored as a 0), and a metric with no value must say why.

### The blob store

| Setting | Default | |
|---|---|---|
| `OPSEVAL_BLOB_STORE` | `file` | `file` (a folder) or `s3` (AWS S3, MinIO, R2 or any S3-compatible store) |
| `OPSEVAL_BLOB_DIR` | `blobs` | the folder, for `file` |
| `OPSEVAL_S3_ENDPOINT`, `OPSEVAL_S3_BUCKET`, `OPSEVAL_S3_REGION`, `OPSEVAL_S3_PREFIX` | | where, for `s3` (path-style addressing). The endpoint's host must be in `OPSEVAL_ALLOWED_HOSTS`, and anything but loopback must use https |
| `OPSEVAL_S3_ACCESS_KEY_ID`, `OPSEVAL_S3_SECRET_ACCESS_KEY` | | secrets; requests are signed (SigV4) by Opseval itself, no AWS SDK |
| `OPSEVAL_S3_SERVER_SIDE_ENCRYPTION` | `true` | asks the store to encrypt every object at rest |
| `OPSEVAL_BLOB_PENDING_GRACE_HOURS` | `24` | how long a blob may stay *pending* before `prune` treats it as an orphan |

Back up both the database and the blob store: each holds half of a run.

| Rule | Why |
|---|---|
| A blob is recorded *pending* before it is uploaded, and *committed* with the row that refers to it | A crash leaves a pending row that `prune` cleans up, never a row pointing at nothing |
| `prune` deletes an orphan's object first, then its row, and never lists the store to guess | A crash mid-cleanup is retried; an object Opseval has no row for is not Opseval's to delete |
| Audio is stored only encrypted (`OPSEVAL_AUDIO_ENCRYPTION_KEY`), the demo's voices too | A voice can be biometric data; plain audio is refused before the run starts |
| A blob store failure is a harness error (exit 3) | A run whose record could not be kept was not measured |
| `runs import` skips a run already imported unchanged and refuses a different run with the same id | Importing twice is safe; nothing is overwritten |
| `runs import` refuses a folder still running, unreadable, or with plain audio, whole | A run is never half-imported |
| A folder keeps its layout when imported | A run written before per-call usage still reads as one |

The Cost and Tokens dashboards work on runs in the database as on folders: they read through
the same store. Indexed analytics over the usage and metric rows come with ADR-0052's
`PgAnalytics`.

