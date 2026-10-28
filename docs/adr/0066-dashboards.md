# ADR-0066: Dashboards, usage events and model prices

- **Status:** Proposed
- **Date:** 2026-10-28
- **Deciders:** Opseval maintainers (owner request 2026-10-28)

## Context

The owner asked for Langfuse-style dashboards: cost, tokens, latency and quality over time,
filtered by the organisation → project → task hierarchy
([ADR-0049](0049-tasks-in-a-project.md)).

What exists today:

- `llm.UsageMeter` keeps one `ModelUsage` per `(role, model)`: calls, `input_tokens`,
  `output_tokens`, `rate_limited_calls` and `cost_usd`. It reads `prompt_tokens` and
  `completion_tokens` from the provider's `usage` object.
- A missing count is added as 0 (`int(usage.get("prompt_tokens", 0) or 0)`), so "the provider
  did not say" and "zero tokens" look the same.
- Prices come from `llm.PRICES`, a dict hard-coded in `llm.py` (empty today). An unknown model's
  `cost_usd` is `None`, never 0, which is right.
- `SuiteResult.usage` (`models/results.py`) stores the snapshot per run. The console's Usage
  page lists it per run through `views.usage` (`RunUsage`: simulator tokens, judge tokens,
  cost).
- Missing: reasoning and cached tokens, any per-call or per-attempt detail, a price table that
  can change without a release, prices that stay fixed for old runs, time series and filters.

## Decision

### One usage event per call

Every LLM, TTS or STT call writes one `usage_events` row (server mode) or one entry in the
attempt's record (files mode):

- attempt, run, task, project;
- role: `simulator`, `judge`, `attacker`, `generator`, `tts` or `stt`;
- provider and model, as sent;
- `input_tokens`, `output_tokens`, `reasoning_tokens`, `cached_input_tokens`,
  `audio_in_seconds`, `audio_out_seconds`;
- latency, and whether the call was rate-limited or retried;
- `cost` and `cost_source`: `provider` (the provider reported it), `price_table`, or
  `unknown`.

Counts come from the provider's own fields (`completion_tokens_details.reasoning_tokens`,
`prompt_tokens_details.cached_tokens`). A count the provider does not report is NULL, never 0.
`ModelUsage` gains the same fields as nullable, and its totals stay NULL when any call's count
is unknown, with the number of calls that reported it beside the total.

In files mode the events live inside the record, so `rescore` and old runs keep working. The
record change bumps `storage.LAYOUT`; older layouts are still read and show the new fields as
not measured.

### Model prices, versioned

A `models` price table, kept the way Langfuse keeps it:

- a match pattern on the model name, a unit (tokens, characters or seconds), and prices for
  input, output, reasoning, cached input and audio;
- `effective_from` on every row. A cost is computed with the row in force when the call was
  made, so an old run keeps its price;
- built-in defaults ship with Opseval and replace `llm.PRICES`. A project can override them in
  the console. An edit adds a row; it never rewrites one;
- cost is recomputed from the counts. A model with no price shows "no price", never $0. A
  provider-reported cost wins and is labelled as such.

If traces carry the agent's own token usage, it is shown as a separate **agent cost** series.
It is never added to the harness's own evaluation cost.

### Built-in dashboards

| Dashboard | Shows |
|---|---|
| Cost | evaluation spend over time by role and model; cost per run, per scenario and per passed test; budget against actual per task |
| Tokens | input, output, reasoning and cached tokens over time, stacked and per model; tokens per attempt; reasoning share |
| Latency | agent turn latency p50/p95/p99; LLM-call latency per role; time to first audio for voice |
| Quality | pass rate over time, gate-state history, flaky scenarios, scores per check, the scenario × run verdict matrix, judge agreement from `calibrate` |
| Voice | barge-in yield, talk-over, dead air, ASR confidence, the agent's WER, by language and dialect |
| Usage | runs, attempts and audio minutes per project and task |

All are filtered by organisation, project, task, suite, label, model, language or dialect, and
date range.

- **The browser computes nothing** ([ADR-0029](0029-console-api.md)). Every number comes from
  an `AnalyticsStore` endpoint ([ADR-0052](0052-analytics-store.md)); the console draws.
- **A null is a gap,** never a 0 on the axis. A pass rate excludes not-judged results and says
  how many it counted, so a harness outage never shows as an agent's bad week, and a week of
  agent failures is never hidden as "no data".

### Phasing

- **D1, with [ADR-0051](0051-runs-and-blobs-in-the-database.md):** usage events; reasoning
  and cached tokens in `UsageMeter` and `ModelUsage` (the layout bump); the Cost and Tokens
  dashboards over `FileAnalytics`, so they work in local mode first.
- **D2, with ADR-0052:** the `models` price table and its editor; Latency, Quality and Voice
  on `PgAnalytics`.
- **D3:** custom dashboards (pick widgets, save them per project) on the same endpoints.
  ClickHouse serves these queries when it is turned on.

## Consequences

- Golden tests on fixture runs require equal totals from files and from Postgres.
- A missing reasoning count stays NULL through the meter, the record, the API and the chart.
- A test changes a price and checks that earlier runs keep their cost.
- Vitest checks that no chart draws a NULL as 0.
- Fixing the `or 0` in `UsageMeter` changes what old totals mean; old records keep their
  totals, labelled as written before per-call usage.

## Not decided here

- Budgets that stop a run when exceeded (run control, [ADR-0035](0035-run-control.md), may
  gain one).
- Exporting dashboards or sharing them outside the console.
- Currencies other than USD.
