# ADR-0005: One storage contract for local and server modes

- **Status:** Proposed
- **Date:** 2026-09-22
- **Deciders:** Opseval maintainers

## Context

Opseval must run on a laptop or in CI with no database (local mode), and also as a shared, horizontally scalable service (server mode), without a rewrite.

## Decision

- A `Storage` protocol with two implementations: **FileStore** (`reports/` on disk) and **SqlStore** (PostgreSQL for metadata and results, plus S3-compatible object storage for immutable run records and reports).
- Both use the same object layout: `<prefix>/<project>/<run_id>/{manifest.json, records/<test>__attempt<N>.json, result.json, report.html}`.
- A queue on Redis/Valkey plus worker processes replaces any in-process run state in server mode.

## Consequences

- Offline `rescore` and migration between modes need only the layout contract.
- ClickHouse is not needed at evaluation data volumes. We will revisit if Cloud analytics require it.
- Run folders also hold `profile.json` and `scenarios/` (the run's snapshot) and `rescore-*/` folders; see ADR-0013.

## Amendment (2026-10-09)

- **The protocol exists:** `storage.RunStore` lists what the runner, `rescore`, `calibrate`,
  `prune` and the console use. `FileStore` is its one implementation, checked against it by a
  test and by mypy. `SqlStore` is still to come, with server mode.
- **The layout is versioned.** Every manifest records `"layout": "opseval-runs/1"`, and a
  folder without the field predates versioning and has layout 1.
- **Readers refuse a layout they do not know.** `load_result`, `load_records`, `load_record`
  and `load_snapshot` raise `UnknownLayout` (a `ConfigError`, exit 3) with "upgrade Opseval",
  rather than read a newer folder with rules that may not apply to it.
- **Such a run is shown, never dropped.** The console lists it as a harness-error row
  (`UNKNOWN_LAYOUT`), and its detail page is a harness error. Its attempts answer 409, compare
  treats it as unavailable, and `prune` never deletes it.
- **When to bump the layout:** on any change that an older reader of a new folder, or a newer
  reader of an old one, could misread. Keep reading the old layouts.
- *(2026-10-19)* **Any new record field bumps the layout**, even an optional one: an older reader
  rejects a field it does not know (`extra="forbid"`), and the layout bump lets it say "upgrade"
  rather than fail with a validation error. Layout 2 (`opseval-runs/2`) adds
  `RunRecord.created_artifacts`; this version reads layouts 1 and 2, and a folder without the
  field is still layout 1.
