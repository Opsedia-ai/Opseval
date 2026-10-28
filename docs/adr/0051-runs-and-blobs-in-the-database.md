# ADR-0051: Runs and blobs in the database

- **Status:** Proposed
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
- `attempt_metrics`: a long table `(attempt_id, name, value, unit, degraded)`. A metric that
  was not measured has a row with `value` NULL and a reason, or no row; it is never written as
  0. Voice latencies and similar numbers land here.
- `blobs`: `(id, kind, content_type, size, sha256, store, key, region, encryption,
  retain_until, legal_hold, deleted_at)`. A blob under legal hold is never pruned.
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
- Server-side encryption is requested and recorded on the `blobs` row. Audio may also be
  encrypted client-side with `OPSEVAL_ENCRYPTION_KEY` (ADR-0047 step 5).
- A blob store error is a `HarnessError`. A run whose record could not be stored ends as
  `harness_error` (exit 3), never as a pass or a fail.

### `DbRunStore`

`DbRunStore` implements `storage.RunStore`, method for method, on the tables and a
`BlobStore`. Nothing above the store changes: the runner, `rescore` and `calibrate` take a
store. Rules:

- `check_layout` still refuses a layout it does not know (`UnknownLayout`).
- `save_result` writes the run, verdict and score rows in one transaction, after the blobs.
  A crash between the two leaves blobs without rows, which retention cleans up; never rows
  that point at missing blobs.
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
- Golden tests run fixture runs through both stores and compare every `load_*` answer.
- The schema check constraint makes a stored `infra_error` with a score, or a failure without
  one, impossible, so a bug in a writer cannot quietly turn a harness error into a 0.

## Not decided here

- Partitioning or archiving old `scores` and `attempt_metrics` rows.
- A blob store other than files and S3 (Azure Blob, GCS) beyond their S3-compatible modes.
- Exporting a database run back to a folder (`runs export`); likely, but not needed yet.
