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

Every LLM, TTS, STT or telephony call writes one `usage_events` row (server mode) or one entry in
the attempt's record (files mode):

- attempt, run, task, project;
- role: `simulator`, `judge`, `attacker`, `generator`, `tts`, `stt` or `telephony` (the one list
  of roles, used by the meter, the price table and budgets);
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
record change bumps `storage.LAYOUT` again (the next number at the time, after ADR-0053's
`opseval-runs/3`); older layouts are still read and show the new fields as not measured.

### Budgets: one rule

Every money budget (a task's monthly budget, [ADR-0064](0064-continuous-evaluation.md); a run's
cost cap) acts the same way, from the usage events above:

- **Checked before each scenario and each attempt starts,** never during one.
- **When it is exhausted,** no new attempt starts. The remaining scenarios are
  `skipped_precondition` with `skip_kind: budget`, named in every output. Like the stop policies
  of [ADR-0035](0035-run-control.md), that is a gate warning, not a failure: nothing the agent did
  is judged by it. A suite that judged nothing still blocks the gate (ADR-0030).
- **An attempt in flight is never cut.** It finishes and is judged on what happened, so a run can
  overshoot by up to one attempt per lane, and the overshoot is shown.
- **A scenario left with some repeats run** is folded by `verdicts.fold_cut_short`, as a cancel
  is: it fails if it would fail even had every remaining attempt passed, and is otherwise skipped
  with `skip_kind: budget`.
- **A cost with no price** cannot be counted against a budget, and is never counted as $0.

Per-attempt caps, such as the attacker's turns and tokens
([ADR-0063](0063-adaptive-attacker.md)), are separate: they end one attempt, not the run.

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
- `TestVerdict.skip_kind` gains `budget`, and `verdicts.fold_cut_short` takes the skip kind it
  records (today it is always `cancelled`). Fault-injection tests prove a budget never cuts an
  attempt and never turns a certain failure into a skip.
- Vitest checks that no chart draws a NULL as 0.
- Fixing the `or 0` in `UsageMeter` changes what old totals mean; old records keep their
  totals, labelled as written before per-call usage.

## Not decided here

- Where a run's cost cap is declared (a suite `stop:` field next to ADR-0035's, or a `run` flag).
- Exporting dashboards or sharing them outside the console.
- Currencies other than USD.

## Implementation note: D1 (2026-10-28)

Built, in local (files) mode:

- **Usage events.** `models/usage.py` defines `UsageEvent` and the one list of roles
  (`UsageRole`). `llm.UsageMeter` keeps one event per call, priced when it is recorded; a
  call made inside `meter.attempt(scenario, index)` (a context variable, so concurrent lanes
  and judge ballots stay apart) belongs to that attempt. The runner saves the record before
  scoring with `usage_events: null`, and again after scoring with every call the attempt paid
  for, judges included, so a crash while judging leaves "not measured", not a short list. Calls
  outside any attempt go to `SuiteResult.usage_events`. `rescore` wraps each attempt it
  re-grades in `meter.attempt` too, and writes those calls to its result's
  `attempt_usage` (records are never rewritten).
  Layout `opseval-runs/6`; layouts 1–5 are read and are "written before per-call usage".
- **Counts.** `llm.token_counts` reads `prompt_tokens`, `completion_tokens`,
  `prompt_tokens_details.cached_tokens` and `completion_tokens_details.reasoning_tokens`; a
  missing or non-count field is `null`, 0 stays 0. A provider-reported `usage.cost` wins
  (`cost_source: provider`).
- **Failed calls.** A call that got no usable answer after its retries is an event with
  `answered: false`, no counts and no cost. It is counted in `ModelUsage.failed_calls` and
  left out of every total and every `of`, as speech already did (ADR-0054: a failed call
  produced nothing to count). Otherwise one transient outage would erase a run's token totals.
- **`ModelUsage`** gains `reasoning_tokens`, `cached_input_tokens`, `failed_calls` and
  `reported` (how many answered calls reported each count). Per this ADR a total is `null`
  unless every answered call reported it; `reported: null` marks an old row.
- **Prices.** `opseval/pricing/builtin.yaml` replaces `llm.PRICES`/`SPEECH_PRICES`: rows with a
  regex `match`, a unit (tokens per 1M, characters per 1M, seconds per minute) and
  `effective_from`. The newest row in force on the call's day prices it; an unreported cache
  or reasoning split is billed at the base rate (no discount was reported); a priced count
  not reported is `null`. It ships a few OpenAI rows (gpt-4o, gpt-4o-mini, whisper-1, tts-1,
  tts-1-hd) with their sources, for the maintainers to check and extend. **The cost stored
  with each call when it was made is authoritative**: nothing reprices it, so a row added
  later, even one backdated to before the run, never changes an old run, and the run's own
  usage rows, the Usage page and the dashboards read the same figures. A call stored with
  `cost_source: unknown` stays not measured.
- **`opseval.analytics`.** `AnalyticsStore` (protocol), `UsageFilter` and `FileAnalytics`,
  which answers `cost` and `tokens`: spend by role and by model per day, week or run; cost per
  run, per scenario, per attempt and per passed test; tokens stacked as input not cached,
  cached input, output not reasoning and reasoning; per model; tokens per attempt; reasoning
  share. Every value is `{value, measured, of, complete}`; `of: 0` is "no calls", `measured: 0`
  with `of > 0` is "not measured", `complete: false` with a value is a floor. Ratios (cost per
  attempt, per passed test, tokens per attempt) have the same shape: the denominator counts
  only measured units (`measured` of `of`: a passed test or attempt with no per-call usage
  never dilutes it), and `complete: false` says the numerator was a floor (drawn "≥"). A
  scenario's `passed_runs` counts only runs whose attempts of it were measured. Token
  aggregates count only roles with tokens (`models.usage.has_tokens`: TTS, STT and telephony
  calls are left out of every token total and its `of`, so a voice attempt is not partial).
  A run whose records cannot be read is left out **entirely** (no calls, passed tests or
  bucket) and named in `scope.unreadable_runs`. The latest re-score of a run is counted with
  that run, its calls grouped by role as `judge (rescore)` and counted in
  `scope.rescore_calls`; per-attempt and per-scenario figures stay the attempts as they ran.
  `FileAnalytics` reads the finished results once per request (options and answer share
  them) and caches each run by `result.json`'s and its latest re-score's mtime. Runs written
  before per-call usage contribute their per-model totals as written and are named in
  `scope.legacy_runs`. Filters: suite, role, model, since/until, bucket. Organisation,
  project, task and label need server mode and are **refused** (400
  `FILTER_NEEDS_SERVER_MODE`), never ignored.
- **API and console.** `GET /api/v1/analytics/cost` and `/analytics/tokens`; console pages
  `/cost` and `/tokens`, drawn as in-house SVG (no chart library): a `null` is a gap ("no
  data"), a partial value hatched and said with "≥" and its coverage, with a table view; a
  bucket where any series was not measured is a floor as a whole (every segment hatched,
  "partial: … not measured" in its tooltip and table row). The Usage page's simulator and
  judge token totals carry `*_tokens_complete` and are drawn "≥ n" when partial.
  The report's usage table shows reasoning and cached tokens and how many calls reported each.

Deferred, as the phasing says or because nothing to build it on exists yet:

- per-project price overrides and the price editor, Latency, Quality, Voice and Usage
  dashboards, `PgAnalytics` and its golden test against `FileAnalytics` (D2);
- budgets ("Budgets: one rule") and `skip_kind: budget`: not in D1's list, and where a run's
  cost cap is declared is not decided;
- the agent cost series from traces: no trace source carries the agent's token usage yet;
- the `attacker`, `generator` and `telephony` roles are in the list but nothing calls them yet;
- custom dashboards (D3).

## Implementation note: D2 (2026-10-30)

Built (the ADR stays Proposed):

- **The known-part cost (the D1 follow-up).** A usage row carries `cost_known_usd` and
  `unpriced_calls` beside `cost_usd`, which stays `null` when any answered call had no price.
  One function, `models.usage.cost_parts` (a `CostParts` of known USD, priced and unpriced
  calls, summed with `math.fsum` so a database and a folder add the same calls to the same
  figure), is the arithmetic of the usage rows (`llm.fold_usage`), the run detail
  (`RunDetail.cost`), the Usage page (`unpricedCalls`) and both analytics stores. Costs on the
  wire are rounded to 6 digits everywhere, so the run detail and the Cost dashboard show the
  same number. Layout `opseval-runs/9`; rows of layouts 1–8 read with the split they tell (a
  total means every call was priced; no total and no per-count tally means none was), else
  `unpriced_calls: null` ("not recorded"), and the run detail then splits the run from its
  records. A failed call is not an unpriced call.
- **Prices.** The built-in table stays in `pricing/builtin.yaml` (reviewed regexes); a project's
  own rows are in `model_prices` (migration `0008`). A project row names its models exactly or
  by a glob whose only wildcard is `*` (at most four, characters `[A-Za-z0-9._:/@+-]`), matched
  case-insensitively by a linear-time two-pointer matcher: a pattern typed in a browser is
  never a regular expression, so it cannot hang the server. A project row in force wins over a
  built-in one; within each, the newest in force wins. **Stored cost stays authoritative** (the
  D1 rule): a row is in force from an instant that may not be in the past (422
  `PRICE_BACKDATED`), so "the price in force when the call was made" and "the cost stored with
  the call" can never disagree, and adding a row never changes an earlier run. A server-mode
  run is priced with the project's table, read once when the run starts. An edit adds a row;
  nothing updates or deletes one. API: `GET /prices` (built-in and project rows, and
  `canEdit`/`editNote`, decided by the server), `POST /prices` (`manage`: admins and owners,
  never a key; audited as `price.create`; 409 `PRICES_READ_ONLY` without a database),
  `GET /prices/resolve?model=&at=` (the row that applies, or `null`: "no price", never $0).
- **Latency, Quality, Voice and Usage**, on `AnalyticsStore` (ADR-0052), `GET
  /api/v1/analytics/latency|quality|voice|usage`:
  - Latency: the agent's reply time per turn (the first frame back after each frame sent,
    `RunRecord.turn_latencies`; a reply the clocks could not time is unmeasured), voice turn
    latency and time to first audio (ADR-0055 metrics), and the harness's own LLM/TTS/STT call
    latency per role (answered calls, retries included; a re-score's calls are not the run).
  - Quality: pass rate per bucket over judged verdicts (`passed / (passed + failed)`; anything
    else is `not_judged`, never in the denominator, and a bucket with nothing judged is
    `null`), the gate state of every run, flaky scenarios (marked flaky, or judged verdicts
    that changed within a suite), every check's pass rate (`infra_error` not judged) and mean
    score, and the scenario × run matrix of the newest `limit` runs (the
    `views.scenario_trend` shape). Judge agreement is `null` with a note: calibrations are
    written beside a run folder and no store reads them yet.
  - Voice: `voice.barge_in_yield_ms`, `voice.talk_over_ratio`, `voice.dead_air_ms`,
    `voice.asr_confidence`, `voice.agent_asr_wer`, in total and by the persona's language and
    dialect, with unmeasured rows and degraded values counted apart.
  - Usage: runs, attempts, voice attempts and audio minutes (TTS audio out plus STT audio in,
    over answered speech calls; `null` with none) per project and task, and per bucket.
  - **Percentiles are nearest-rank** in both stores: the value at rank `ceil(p·n)` of the
    sorted values, which is Postgres `percentile_disc`. Every answer carries `n` (and
    `unmeasured`) beside the value; nothing measured is `null`, drawn as a gap.
  - Filters: suite, task, language, dialect, since/until and bucket where they apply. Each is
    applied or refused with a code, never ignored: `FILTER_NEEDS_SERVER_MODE` (task,
    organisation, project, label in local mode), `FILTER_NOT_APPLICABLE` (model or role on a
    dashboard that is not about calls; language on cost, tokens or usage),
    `FILTER_NOT_RECORDED` (label: a run does not record the catalogue label it ran), and
    `FILTER_OUTSIDE_SCOPE` (another project in server mode). A verdict's language is its
    scenario's first attempt's persona.
- **Console.** `/latency`, `/quality`, `/voice`, `/usage-totals` and `/prices`, drawn with the
  in-house SVG of D1 plus `GapLines` (a `null` ends the line: no point and no segment across
  it, the bucket says "no data", "not measured" or "nothing judged"). The add-a-price form is
  shown only when the server says `canEdit`. The run detail shows the cost split. Vitest
  proves a null is never drawn as 0 and that the pages say "not measured".

Deferred: the label filter (needs runs to record the label they ran), judge agreement (needs
calibrations stored through the run store), budgets and the agent cost series (as D1 said),
custom dashboards (D3).
