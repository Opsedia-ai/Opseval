# ADR-0037: Retention: `opseval prune`

- **Status:** Proposed
- **Date:** 2026-10-09
- **Deciders:** Opseval maintainers

## Context

FR-SAF-06 (a must-have) asks for pruning runs older than N days, 90 by default. A run folder
keeps whole conversations, with credential values redacted (ADR-0033) but personal data
deliberately kept, so keeping them for ever is a liability, not a feature. Per-project policies
and legal hold are an Enterprise item (EE-06). The core needs the plain, safe version.

Pruning is the only Opseval feature that deletes data, and the reports folder is shared with
runs that may be in flight, possibly started by another process or another host.

## Decision

`opseval prune [--older-than DAYS] [--reports-dir DIR] [--yes]`, with `OPSEVAL_RETENTION_DAYS`
(default 90) as the default age.

- **A dry run unless `--yes`.** Without it, the command prints the plan (delete or keep, with
  the reason for each run) and deletes nothing.
- **Age is the run's start**, from its manifest. A run whose start cannot be read is kept.
- **Only runs that have ended are deleted:** a `result.json`, an `error.json`, or a process on
  this host that is gone (the same rule the console uses for "interrupted",
  `storage.process_gone`). A run from another host, or one still running, is kept. A run is
  never guessed dead.
- **Only run folders are touched.** That means directories directly inside the reports folder,
  with a run-id name and a `manifest.json`, and never a symbolic link. Anything else is not
  listed and not touched.
- **Each folder is checked again just before it is deleted**, so a plan cannot delete
  something that changed under it.
- **A folder is deleted in an order that is safe to stop at.** Its contents go first, then
  `manifest.json`, then the result or error file. If deletion stops part-way (a file owned by
  another user, for example), the folder still reads as a run that ended, or no longer as a run
  at all; never as one still going. Prune carries on with the other runs, lists what it could
  not delete and why, and exits 3.

## Consequences

- Teams can schedule `opseval prune --yes` (cron, a CI job) to keep 90 days of runs.
- A run's re-scores (`rescore-*` folders) live inside it and go with it.
- Compare and Trend lose what is pruned. A baseline older than the window must be kept
  outside the reports folder.
- Not built: pruning from the console, keep-the-last-N rules, per-suite windows. Those are
  policy choices that belong with the server mode or EE-06.
