# ADR-0051: Runs and blobs in the database

- **Status:** Accepted (implemented 2026-10-30, see Implementation)
- **Date:** 2026-10-28
- **Deciders:** Opseval maintainers (owner request 2026-10-28)

## Context

This is step 2 of [ADR-0047](0047-server-mode-on-postgres.md): runs in the database.

Today every run is a folder. `storage.RunStore` is the contract the runner, `rescore`,
`calibrate`, `prune` and the console use, and `storage.FileStore` is its only implementation.
It writes `manifest.json`, one record per attempt, `progress.json`, `result.json` (or
`error.json`), the HTML report and the JUnit XML. The console's `server/views.py` re-reads
every `result.json` on each request (`views.load_results`, `views.list_runs`, `views.trend`,
`views.usage`). That is fine for tens of runs; it is not fine for a shared server with months
of history, and it gives analytics ([ADR-0052](0052-analytics-store.md)) nothing to index.

Voice ([ADR-0053](0053-voice-sessions-and-records.md)) adds audio, which is large, personal
data and must be kept, encrypted and deleted on its own schedule. A record is not the right
place for it.

Two rules must survive the move:

- `ScoreResult` holds `score is None` exactly when the status is `infra_error`. The database
  must not let a harness problem be stored as a 0.
- An unmeasured value is "not measured", never 0.

## Decision

### Tables

All rows carry `project_id` and `task_id` ([ADR-0049](0049-tasks-in-a-project.md)).

- `runs`: the run id (the same id `FileStore` mints), suite, labels used, catalogue versions
  used, layout, status (`running | finished | harness_error | interrupted`), exit code, gate
  state, counts (null until finished), started/ended, the manifest as JSONB, and the
  record/report blob refs.
- `attempts`: one per `RunRecord` (`test_id`, `attempt_index`), terminal state, timings,
  trace coverage, redaction applied, the attempt verdict, and the record's blob ref. The full
  record stays in the blob store; the row holds what queries need.
- `scores`: one per `ScoreResult`: scorer id and version, axis, status, score, threshold,
  weight, degraded, vacuous, reason code. A check constraint repeats the model's rule:
  `(score IS NULL) = (status = 'infra_error')`.
- `verdicts`: one per `TestVerdict`: status, `skip_kind`, flaky, `cut_short_of`, criticality.
- `attempt_metrics`: a long table `(attempt_id, turn, name, value, unit, degraded, reason)`.
  - **Grain:** one row per turn for a per-turn metric (a turn latency), with `turn` set; one row
    per attempt for an attempt-level metric (time to first audio), with `turn` NULL.
  - **Raw values only.** Percentiles (p50, p95, p99) are computed by analytics
    ([ADR-0052](0052-analytics-store.md)) from the per-turn rows, never stored pre-folded.
  - **Not measured** is a row with `value` NULL and a `reason` (a check constraint:
    `value IS NOT NULL OR reason IS NOT NULL`), or no row. It is never written as 0.
  - Voice latencies and similar numbers land here.
- `blobs`: `(id, run_id, state, kind, content_type, size, sha256, store, key, region,
  encryption, retain_until, legal_hold, created_at, deleted_at)`. `state` is `pending` or
  `committed`. A blob under legal hold is never pruned.
- `audio_assets`: per stored audio file: blob, attempt, speaker channel (`customer` or
  `agent`), codec, sample rate, duration, redaction applied, consent basis.

The progress log and the cancel flag move to the `runs` row, so a worker
(ADR-0047 step 4) and the web process agree on them without shared files.

### The blob store

A `BlobStore` protocol: `put(kind, bytes, *, region, retain_until) -> ref`, `get(ref)`,
`delete(ref)`, `exists(ref)`.

- **`FileBlobStore`** is the default: a directory, content-addressed, written atomically.
- **`S3BlobStore`** speaks the S3 API to AWS, MinIO, R2 or any compatible store. Request
  signing (SigV4) is written in-house over httpx, as the LLM client is: no boto, no vendor SDK
  ([ADR-0003](0003-python-core-openai-compatible-http.md)). The endpoint is allowlisted
  (`check_url_allowed`) and must use TLS off loopback; the keys are `SecretStr`.
- Server-side encryption is requested and recorded on the `blobs` row.
- **Audio is always encrypted client-side when stored,** agent audio without exception, with
  `OPSEVAL_AUDIO_ENCRYPTION_KEY` ([ADR-0053](0053-voice-sessions-and-records.md)). That key is
  separate from `OPSEVAL_ENCRYPTION_KEY`, which encrypts agent credentials (ADR-0047 step 5), so
  either can be rotated or revoked alone. In hashes-only mode no audio bytes are stored at all.
- A blob store error is a `HarnessError`. A run whose record could not be stored ends as
  `harness_error` (exit 3), never as a pass or a fail.

### `DbRunStore`

`DbRunStore` implements `storage.RunStore` on the tables and a `BlobStore`. The contract is
not ready for a second store yet, and the `storage.RunStore` docstring overstates it:

- Its write methods return a `Path` (`write_manifest`, `save_record`, `save_result`,
  `save_report`, `save_junit`, `save_rescore` and others). A database store has no path.
- `server/views.py` is typed against `FileStore`, not `RunStore`, and reads files around the
  store: `store.root.glob("*/result.json")` in `load_results`, and
  `store.run_dir(run_id) / "result.json"` in `run_detail` and `compare`.

**The first PR of this ADR is that refactor, with no change in behaviour:** the write methods
return nothing (or a ref), the reads views need (list runs, is a run finished) become `RunStore`
methods, and views take a `RunStore`. Only after it does `DbRunStore` arrive, and from then on the
runner, `rescore`, `calibrate` and the console take a store without caring which. Rules:

- `check_layout` still refuses a layout it does not know (`UnknownLayout`).
- **A blob is written behind a pending row.** Before `put`, a `blobs` row with
  `state = 'pending'` and the run id is committed. The row that refers to the blob (an attempt,
  or the run in `save_result`) is written in the same transaction that marks the blob
  `committed`.
- `save_result` writes the run, verdict and score rows and commits the run's pending blobs in
  one transaction. A crash before it leaves pending rows, which retention finds; never rows that
  point at missing blobs.
- `load_result` rebuilds the `SuiteResult` from the stored `result.json` blob, not from rows,
  so a reader sees exactly what the run wrote. The rows exist for queries.

### Moving runs in

`opseval runs import FOLDER... --project org/p [--task t]` reads run folders through
`FileStore`, so it validates them as every reader does, and writes them through
`DbRunStore`. It is idempotent on the run id. A folder with an unknown layout or a broken
result is reported and skipped, never half-imported. Runs still in progress are refused.

## Consequences

- The console and analytics read indexed rows instead of every `result.json`.
- Two kinds of state to back up, as ADR-0047 said: the database and the blob store.
- Retention ([ADR-0037](0037-retention.md)) gains a database path: delete blobs whose
  `retain_until` has passed and that have no legal hold, then mark the rows. Audio can
  expire earlier than its record.
- Retention also deletes **orphans**: a `pending` blob older than a grace period
  (`OPSEVAL_BLOB_PENDING_GRACE_HOURS`, default 24) whose run is no longer running. It deletes
  the object first, then the row, so a crash mid-cleanup is retried, never lost track of. It
  never lists the store to guess: an object with no row is not Opseval's to delete.
- Golden tests run fixture runs through both stores and compare every `load_*` answer.
- The schema check constraint makes a stored `infra_error` with a score, or a failure without
  one, impossible, so a bug in a writer cannot quietly turn a harness error into a 0.

## Implementation (2026-10-30)

Built in two steps, as decided: first the refactor, then the database store.

**1. The run store is path-free** (`refactor(storage)`, no change in behaviour). Every write on
`storage.RunStore` returns nothing, except `save_record` and `save_rescore`, which return an
opaque ref (a record's ref goes into `AttemptVerdict.record_path`; `FileStore` returns the file
path as before). Every read anything above the store needs is a method: `run_ids`, `exists`,
`has_result`, `has_error`, `deletable`/`delete_run` (retention), `changed_at` (the analytics
cache), `rescores`/`load_rescore`, `load_report`/`load_junit`, `where` (the health route), and
`audio(run_id)`, a `RunAudio` (write, size, read, the prune marker, count, prune) that the
recorder, the player (`voice.playback` takes a `RunAudio` or a run folder), the report and audio
retention share. `server/views.py`, `server/audio.py`, `analytics/files.py`, `retention.py`,
`rescore.py` (`rescore_stored(store, run_id)`; `rescore_run(folder)` wraps it) and the runner
take a `RunStore`. `tests/test_store_contract.py` fails on any `store.root` or `.run_dir(` outside
`storage.py`. The commands that take a run *folder* on the command line (`cancel`, `summary`,
`calibrate`, `audio export`) still build paths from the folder they were given: that is local
mode by definition.

**2. Runs and blobs in the database** (migration `0005`, `db/run_store.py`, `blobs.py`).

- **Tables** as decided, with these choices. `runs.id` is the id `FileStore` mints (unique
  across projects, so an import never renames). `runs (task_id, project_id)` is a composite key
  to `tasks (id, project_id)` (`NO ACTION`: a task with runs cannot be deleted, as ADR-0049
  said). `attempts`, `verdicts`, `usage_events` and `blobs` carry `project_id` and `task_id`
  through one composite key `(run_id, project_id, task_id)` to `runs`, so no row can name another
  project's run; `scores` and `attempt_metrics` hang off an attempt and carry `project_id` only
  (their task is the attempt's). `run_scenarios` holds the snapshot `rescore` reads, and
  `runs.profile_snapshot` the profile.
- **Checks:** `ck_scores_infra_error_score` (`(score IS NULL) = (status = 'infra_error')`),
  `ck_attempt_metrics_unmeasured_reason` (`value IS NOT NULL OR reason IS NOT NULL`),
  `ck_usage_events_cost_source` (`(cost_usd IS NULL) = (cost_source = 'unknown')`, ADR-0066),
  and the status, kind and state enumerations.
- **`blobs`** columns follow the review: `media_type`, `bytes`, `retention_until`, plus `name`
  (what the object is to its run, as a folder would name it: `result.json`,
  `records/a__attempt0.json`, `audio/a/attempt0/001-agent.wav.enc`) and kind `result` besides
  `record|report|junit|audio|transcript|other`. A row is deleted rather than marked
  (`deleted_at` is not built). `retention_until` is not set yet: runs and audio are pruned by
  the run's age, as folders are; `legal_hold` is honoured (a run with a held blob is not
  deleted).
- **The pending-row protocol.** `save_record` commits the record blob, the attempt row, the
  attempt's audio blobs, its `attempt_metrics` (from the record's voice metrics) and its usage
  events in one transaction. A record is saved twice per attempt (before scoring, then with its
  usage events); the first blob goes back to `pending` in that transaction and is deleted after
  it (left pending, for retention, if that fails). `save_report` and `save_junit` leave their
  blobs pending; `save_result` commits them with the result blob, the run row, the verdict and
  score rows and the run's own usage events. Audio whose attempt never got a record stays
  pending: an orphan. `DbRunStore.orphans()` lists pending blobs older than
  `OPSEVAL_BLOB_PENDING_GRACE_HOURS` (24) whose run has ended (or whose process on this host is
  gone); `delete_orphans` deletes the object, then the row. `opseval prune --project` does it.
- **Progress, cancel and error** are columns of the `runs` row (JSONB). `save_error` before the
  manifest still records the run (the console's pre-minted id).
- **Audio is always sealed.** `DbRunAudio.write` refuses bytes that are not an Opseval sealed
  file (exit 3 at launch when a voice run would record files without
  `OPSEVAL_AUDIO_ENCRYPTION_KEY` into a store that keeps only sealed audio: the runner asks the
  store's `RunAudio.sealed_only`), and records `aes-256-gcm` and the key id from the file's
  header on the blob row. The `audio_assets` table is not built: the per-turn metadata stays in
  the record's `AudioRef`, and the blob row carries the rest.
- **Sync over asyncio.** `RunStore` is synchronous (the runner and the console's threads call
  it from plain code) and the driver is asyncpg. `DbRunStore` runs its own event loop on a
  thread and waits for each call, so any thread may use it and connections stay on their loop.
- **Blob stores.** `FileBlobStore` (default, `OPSEVAL_BLOB_DIR`) keys objects
  `runs/<run>/<blob id>` (one object per blob row, never shared, so deleting one never breaks
  another). `S3BlobStore` signs with SigV4 written in `blobs.py`, checked against AWS's
  published test vectors (`tests/test_blobs.py`) and a fake S3 that verifies every signature;
  path-style addressing (AWS, MinIO, R2); `x-amz-server-side-encryption: AES256` by default,
  recorded as `sse:AES256`; errors name the host and HTTP status only. Settings:
  `OPSEVAL_BLOB_STORE`, `OPSEVAL_BLOB_DIR`, `OPSEVAL_S3_ENDPOINT|BUCKET|REGION|PREFIX`,
  `OPSEVAL_S3_ACCESS_KEY_ID`, `OPSEVAL_S3_SECRET_ACCESS_KEY` (both secrets),
  `OPSEVAL_S3_SERVER_SIDE_ENCRYPTION`, `OPSEVAL_BLOB_PENDING_GRACE_HOURS`.
- **Wiring.** `opseval run --project org/p [--task t]` writes to the database
  (`OPSEVAL_DATABASE_URL`, or `--database-url`) and the blob store; without `--project` nothing
  changes. `opseval serve --project` reads runs from the database and writes the runs the
  console starts there. `opseval prune --project` prunes runs, audio and orphans.
  `opseval runs import FOLDER... --project` imports run folders of any layout 1–8, keeping the
  folder's layout (a run written before per-call usage still reads as one); idempotent on the
  run id (unchanged: skipped; a different run under the same id: refused, exit 3); a folder
  still running, unreadable, or holding plain audio (the demo's synthetic voices) is refused
  whole, and a failure part-way removes what was written.
- **Analytics in database mode** use `FileAnalytics` over the database store: it reads through
  the `RunStore` contract only, so the Cost and Tokens dashboards answer the same numbers in
  both modes. It still reads each run's result and records (cached per run by `changed_at`); the
  indexed `PgAnalytics` over `usage_events` and `attempt_metrics` is ADR-0052/ADR-0066 D2.
- **Golden tests** (`tests/test_db_runs.py`) run the demo into a folder, import it, and compare
  every read the console, rescore and analytics make (result, records, snapshot, manifest,
  run detail, attempts, list, usage, trend, report, JUnit, progress) between the two stores.

Not built yet: the `audio_assets` table, `retention_until` per blob, `runs export`, run
partitioning, the console's own project and task scoping (ADR-0047 step 1: `serve --project`
serves one project), and the report's audio export command for a database run (it names a
folder; play such a run in the console).

## Not decided here

- Partitioning or archiving old `scores` and `attempt_metrics` rows.
- A blob store other than files and S3 (Azure Blob, GCS) beyond their S3-compatible modes.
- Exporting a database run back to a folder (`runs export`); likely, but not needed yet.
