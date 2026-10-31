# Dashboards and prices

The console's dashboards show how your agent and the evaluation itself behave over time:
cost, tokens, latency, quality, voice and usage. Every number comes from the server; the
browser only draws it. Something that was not measured is shown as **not measured** (a gap in
a chart, never a 0), and a figure that covers only part of what it should is shown as a floor
(**≥**).

## Where the numbers come from

| Mode | Store | What it reads |
|---|---|---|
| Local (`opseval serve` on run folders) | `FileAnalytics` | each run's `result.json` and records |
| Server (`opseval serve --project`, a database) | `PgAnalytics` | the run rows in Postgres (indexed SQL) |

Both give the same answers to the same runs: a test writes the same runs to a folder and to
the database and compares every answer. In server mode the dashboards see the served project,
and only the served task when the server is in accounts mode.

## The dashboards

| Page | API | Shows |
|---|---|---|
| Cost | `/api/v1/analytics/cost` | evaluation spend by role and model, per run, scenario and passed test |
| Tokens | `/api/v1/analytics/tokens` | input, cached, output and reasoning tokens |
| Latency | `/api/v1/analytics/latency` | the agent's reply time, voice turn latency and time to first audio (p50, p95, p99); the harness's own call latency per role |
| Quality | `/api/v1/analytics/quality` | pass rate over time, the gate of every run, flaky scenarios, every check, the scenario × run matrix |
| Voice | `/api/v1/analytics/voice` | barge-in yield, talk-over, dead air, ASR confidence and the agent's WER, by language and dialect |
| Usage | `/api/v1/analytics/usage` | runs, attempts, voice attempts and audio minutes per project and task |

How to read them:

- **Percentiles are nearest-rank**: the p95 is the value at rank `ceil(0.95 × n)` of the sorted
  values, a time that was actually measured. Each one says how many values it covers (`n`) and
  how many were not measured (a reply during a barge-in, a voice metric the call could not
  measure).
- **The agent's reply time** is timed per conversation turn: probes, handshakes and
  notifications are not turns. A turn the agent never answered (a timeout) is shown as
  **unanswered** next to `n`, never as a time.
- **A pass rate counts judged verdicts only.** An outage, a skip or a check that could not run
  is counted apart (`not judged`), so a harness problem never shows as the agent's bad week,
  and a day with nothing judged is a gap, not 0%.
- **Judge agreement** says not measured for now: `opseval calibrate` writes its results beside
  a run folder, and no store reads them yet.

### Filters

Suite, task (server mode), language and dialect (the persona's, as the scenario declared
them), a date range and the bucket (day, week or run). A filter is applied or refused with a
reason, never silently ignored:

| Code | Meaning |
|---|---|
| `FILTER_NEEDS_SERVER_MODE` | task, project or organisation on run folders |
| `FILTER_NOT_APPLICABLE` | e.g. a model on the Quality dashboard, a language on Cost |
| `FILTER_NOT_RECORDED` | label: runs do not record the catalogue label they ran yet |
| `FILTER_OUTSIDE_SCOPE` | a project this server does not serve |

## What a run cost

When some calls have a price and others do not, a run shows **the cost of the priced calls
and how many calls had no price**, never one total that pretends to be complete. The run
detail, the Usage page and the Cost dashboard use the same arithmetic, so they agree.

## Prices

Opseval ships a built-in price table (`opseval/pricing/builtin.yaml`). Each call is priced when
it is made, with the row in force at that moment, and keeps that cost: a later price never
changes an earlier run. A model with no row has **no price**, never $0.

In server mode a project can add its own rows on the **Prices** page (admins and owners), or
through the API:

```bash
curl -X POST "$OPSEVAL/api/v1/prices" -H "Authorization: Bearer …" -H "Content-Type: application/json" \
  -d '{"pattern": "gpt-4o*", "matchKind": "glob", "unit": "tokens", "input": 2.5, "output": 10, "source": "our contract"}'
```

- `pattern` is a model name (`matchKind: exact`) or a glob whose only wildcard is `*`
  (`matchKind: glob`). Regular expressions are refused.
- Prices are USD per 1M tokens, per 1M characters, or per minute of audio (`unit`).
- A row is in force from now, or from a later `effectiveAt`; a date in the past is refused,
  because it would make old runs disagree with the cost they were stored with.
- An edit is a new row; nothing is rewritten or deleted. A project's row wins over a built-in
  one. Every row added is in the audit log (`price.create`).
- `GET /api/v1/prices/resolve?model=gpt-4o-mini&at=2026-10-01` says which row prices a model
  at a moment, or that it has no price.

On run folders the table is read-only: `POST /prices` answers 409 `PRICES_READ_ONLY`.
