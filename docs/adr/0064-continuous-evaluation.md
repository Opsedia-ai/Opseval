# ADR-0064: Continuous evaluation (flexibility level L5), and the whole ladder

- **Status:** Proposed
- **Date:** 2026-10-28
- **Deciders:** Opseval maintainers (owner request 2026-10-28)

## Context

Today Opseval runs when someone runs it: a person at a terminal, a CI job, a click in the console.
An agent in production changes without a release: the model provider updates a model, a prompt is
edited, a tool's backend moves. Nobody runs the suite, so nobody notices.

The last level of the flexibility ladder makes evaluation continuous. Scheduled runs, regression
alerts and scenarios proposed from what went wrong. The risk is automation that decides on its own:

- a scheduler that quietly stops, so monitoring ends and nobody is told;
- a harness outage reported as an agent regression;
- a proposer that edits the release catalogue;
- a bill nobody approved;
- an alert wired straight to a rollback.

## Decision

### The ladder, opt-in per task

| Level | ADR | What a team configures per task | Guardrail |
|---|---|---|---|
| L0 | ADR-0048 | Scenarios in YAML or the guided form | the loader's checks; the commit guard |
| L1 | [0060](0060-datasets-and-matrices.md) | `dataset:`, `{{var}}`, `matrix:` | expanded at load with a cap; no template logic; a missing variable is a load error |
| L2 | [0061](0061-rubric-judges-and-content-checks.md) | `judge.rubric` criteria, `content.*` checks | criteria fenced and in the rubric digest; judge independence; `must` criteria cannot be averaged away |
| L3 | [0062](0062-generated-scenarios.md) | `opseval generate` from docs or transcripts | drafts only; every draft through the loader; provenance; a cost cap |
| L4 | [0063](0063-adaptive-attacker.md) | an `attacker:` with strategies and caps | graded by the security judges; attacker family differs from the judge's; stops at the commit gate |
| L5 | this ADR | a `schedule:`, regression rules, proposals from failures and traces | only a human admin or owner moves `production`; a monthly budget; alerts, never rollback |

- **Every level is opt-in per task.** A task that uses none of them behaves as today.
- **L0 to L2 are opt-in by writing them.** They are scenario files, versioned and reviewed like any
  other.
- **L3 to L5 are switched on per task by an admin** (roles of
  [ADR-0050](0050-accounts-and-access.md)). They spend money without a person at the keyboard, or
  read production data.

### `schedule:` on a task, run by workers

```yaml
schedule:
  - cron: "0 */6 * * *"     # five fields, UTC unless tz is given
    suite: suites/smoke.yaml
    label: production
    repeats: 1
```

- **Enqueueing.** Schedules are stored on the task ([ADR-0049](0049-tasks-in-a-project.md)). One
  worker at a time holds the scheduler, through a Postgres advisory lock, and enqueues runs on the
  queue of [ADR-0047](0047-server-mode-on-postgres.md) step 4. Any worker runs them, exactly as
  console-started runs.
- **What a scheduled run is.** A normal run of the catalogue under `label`, with the task's profile.
  It is recorded as `trigger: schedule` and shown as such everywhere.
- **Never the irreversible step.** A schedule cannot set `allow_commit`. Scheduled runs keep the
  commit guard of [ADR-0008](0008-committed-outcome-guard.md) on.
- **Missed and overlapping runs are said, not dropped.**
  - After a worker outage, a missed slot runs once, not once per slot missed.
  - A slot whose previous run is still going is recorded as `skipped: previous_running`.
  - Both show in the task's history.

### Regression detection

After each scheduled run, rules are evaluated on the analytics store
([ADR-0052](0052-analytics-store.md)):

- a scenario that passed in the baseline run and fails now, both judged;
- a pass rate down by more than a set amount over the last *N* runs;
- a metric past its threshold (turn latency p95, voice metrics, cost per run).

Each alert counts what it is based on ("3 of 40 scenarios, 2 runs"), as every analytics answer does,
and links the runs.

**Two kinds of alert, never mixed.** This is the one rule, applied to monitoring:

- **Agent regression:** judged verdicts got worse.
- **Monitoring gap:** a run exited 2 or 3, a scenario was not measured, a schedule was skipped or
  the budget ran out. It is raised when the gap lasts longer than the task's setting (default 24
  hours).

A harness outage therefore never reads as a regression. A harness that keeps failing never lets
monitoring stop in silence.

### Proposed scenarios, from failures and from production

- **From a failed scheduled attempt:** a draft regression scenario, with the customer's turns
  scripted from the record. It is a regression test for exactly what failed.
- **From production traces:** conversations read through the trace sources
  ([ADR-0011](0011-trace-backed-tool-scoring.md) Langfuse, [ADR-0028](0028-otlp-trace-source.md)
  OTLP), selected by a filter the admin sets (an error status, a low user score). They go through
  the generator of ADR-0062.
- **Everything is a draft,** with provenance, credential redaction and the personal-data
  acknowledgement, set once per task by an admin. Proposals spend from the task's budget.

### Only a human admin or owner moves `production`

- `production` is a protected label ([ADR-0050](0050-accounts-and-access.md)). Only a human
  admin or owner moves it: signed in, or with a key that an admin or owner granted
  `label:protected` for a release step they run themselves.
- **No key used by schedules or proposals holds `label:protected`,** nor does any key of the
  scheduler, the workers or the regression rules. Those keys are system keys, and ADR-0050
  refuses the grant to a system key.
- A proposed scenario reaches `production` only after a person accepts it and an admin labels the
  catalogue version that contains it.

### A monthly budget per task

- **What it covers:** every role the task's automation uses: `simulator`, `judge`, `attacker`,
  `generator`, `tts`, `stt` and `telephony`. Costs come from the usage events and price table of
  [ADR-0066](0066-dashboards.md).
- **At 80%,** an alert.
- **At 100%,** the budget rule of ADR-0066 applies to scheduled runs: no new attempt starts, the
  remaining scenarios are skipped with `skip_kind: budget`, and an attempt in flight is never cut.
  A scheduled run that could not start at all is recorded as `skipped: budget_exhausted`. Both are
  raised as a monitoring gap. Proposals stop. Runs a person starts are not bound by the task's
  automation budget; they get a warning instead.
- **A model with no price** cannot be budgeted. A schedule using one is refused when it is saved,
  unless the task sets a token cap in its place. An unknown cost is never counted as $0.

### Alerts go through sinks; nothing rolls back

- Alerts use the sinks of [ADR-0027](0027-sinks.md): the webhook, the GitHub and Jira issue sinks
  ([ADR-0040](0040-github-issues-sink.md), [ADR-0045](0045-jira-sink.md)) and plugin sinks. A sink
  failure is named on the run and in the console, never swallowed.
- **Opseval never rolls anything back,** and never calls a deployment system. An alert carries the
  gate state and the evidence. What to do about it is the team's decision, made in its own
  automation, if anywhere.

## Consequences

- An agent's quality is watched between releases, at a cost each task sets and can see.
- Server mode with workers is required (ADR-0047 step 4). Local mode has no scheduler, and cron plus
  `opseval run` remains the answer there.
- More alerts. The two alert kinds and their counts are what keep them readable.

## Not decided here

- Statistical tests for "a pass rate dropped" beyond a fixed threshold over *N* runs.
- Sampling production traffic continuously, rather than on a schedule.
