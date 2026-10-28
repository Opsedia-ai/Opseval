# ADR-0052: The analytics store, and ClickHouse as an option

- **Status:** Proposed
- **Date:** 2026-10-28
- **Deciders:** Opseval maintainers (owner decision 2026-10-28: ClickHouse is optional, behind
  an interface)

## Context

The console's numbers come from `server/views.py`. Each one scans finished runs:
`views.load_results` globs every `result.json`, and `views.trend`, `views.scenario_trend`,
`views.usage` and `views.compare` fold them in Python. ADR-0029 requires that the browser
computes nothing, so every aggregate is a server function with tests. That works for one
catalogue folder and a few hundred runs.

What teams now ask for is more: pass rate by language, dialect, persona or model; flaky
scenarios across months; voice latency percentiles ([ADR-0053](0053-voice-sessions-and-records.md));
cost per task;
dashboards ([ADR-0066](0066-dashboards.md)). Scanning files cannot answer those
at server scale, and runs are moving into Postgres
([ADR-0051](0051-runs-and-blobs-in-the-database.md)).

[ADR-0047](0047-server-mode-on-postgres.md) did not use ClickHouse: evaluation volumes are
tens of runs a day, and one database keeps self-hosting simple. The owner asked to settle it,
and decided on 2026-10-28: ClickHouse is optional, behind an interface. Postgres stays the
source of truth.

## Decision

### One protocol for every aggregate

An `AnalyticsStore` protocol answers the questions the console and the API ask:

- pass rate by scenario, task, suite, language, dialect, persona, label or model, over time;
- the verdict matrix (scenario × run), as `views.scenario_trend` returns it today;
- flaky scenarios (verdict flips and `flaky` verdicts);
- metric percentiles (p50, p95, p99) from `attempt_metrics`, voice metrics included;
- cost and tokens by role, model or task ([ADR-0066](0066-dashboards.md));
- judge agreement from saved calibrations
  ([ADR-0018](0018-judge-calibration-against-human-labels.md)), with the false-pass rate kept
  separate;
- gate-state history;
- usage: runs, attempts and audio minutes per project and task.

Every answer is a typed model, filtered by organisation, project, task, suite, label, model,
language and date range.

**Every answer says what it counted.** Each value carries `measured` (how many results it
covers) beside the value. A value with nothing measured is `null`, never 0. A rate over
judged results excludes `infra_error` scores and not-judged verdicts from the denominator and
reports them separately. The one rule holds at the aggregate level: a harness outage must not
pull a pass rate down, and a week with no judged runs must not draw as a 0% week.

### Three implementations

- **`FileAnalytics`** wraps today's `views.py` scans. Local mode is unchanged: same folder,
  same numbers, no database.
- **`PgAnalytics`** runs indexed SQL on the ADR-0051 tables. It is the default in server mode.
- **`ClickHouseAnalytics`**, later, as an optional extra (`opseval[clickhouse]`).
  - Postgres stays the source of truth. ClickHouse is fed from Postgres through an outbox
    table written in the same transaction as the run, so a run is never in ClickHouse without
    being in Postgres.
  - If ClickHouse lags or is down, the answer says so (stale since, or unavailable). It never
    returns a partial answer as if complete, and it can fall back to `PgAnalytics`.
  - The client speaks ClickHouse's HTTP interface over httpx
    ([ADR-0003](0003-python-core-openai-compatible-http.md)); the host is allowlisted and
    TLS off loopback.

**The ClickHouse extra is Apache-2.0 core, not `ee/`.** Analytics over evaluation results is an
evaluation feature. GOVERNANCE.md pledges that every evaluation capability is Apache-2.0 with
no usage limits when self-hosted, so the faster store for those answers cannot sit behind a
licence key.

### Why ClickHouse is not required

- **One database to self-host.** A team that runs Opseval should not have to operate an OLAP
  cluster to see a pass rate.
- **Volumes.** Text evaluation writes a few thousand score rows a day for a busy team. Postgres
  with the right indexes answers those queries in milliseconds.

### When to turn it on

- The managed Cloud edition, where many organisations share one analytics backend.
- Large voice workloads, where per-turn and per-frame metrics (latency, talk-over, dead air,
  confidence) reach millions of `attempt_metrics` rows.
- Long histories queried by many dashboards at once.

## Consequences

- The console's routes call the protocol, not `views.py` scans directly. `views.py` keeps the
  pure mapping to wire models.
- Golden tests run the same fixture runs through `FileAnalytics` and `PgAnalytics` (and later
  `ClickHouseAnalytics`) and require identical answers, `null`s included.
- A new aggregate is added to the protocol and to every implementation at once, with the
  golden test. No implementation may answer a question the others cannot.
- The outbox adds one write per run in server mode, whether or not ClickHouse is used, so
  turning it on later needs no backfill design.

## Not decided here

- The ClickHouse table layout and retention.
- Materialised views or rollup tables in Postgres; added only when a measured query needs them.
- An analytics API for third parties beyond what the console uses.
