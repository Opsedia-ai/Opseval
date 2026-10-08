# ADR-0035: Run control: stop policies and cancellation

- **Status:** Proposed
- **Date:** 2026-10-09
- **Deciders:** Opseval maintainers

## Context

FR-CTL (a v0.1 must-have) asks for five stop policies (`fail_fast`, `max_consecutive_failures`,
`stop_on_rate_limit`, `max_wall_clock_seconds` and `stop_on_critical_failure`) and for
cancelling a run between tests. Only `--fail-fast` existed (ADR-0032). A run could not be
stopped except with Ctrl-C, which leaves no result at all, and the console had no way to stop a
run it had started.

Stopping early is a core-rule hazard in both directions:

- if what was not run disappears, a stopped run can look like a smaller run that passed;
- if a scenario's verdict is folded from the attempts that happened to finish, it claims a pass
  rate nobody measured.

## Decision

**Where a policy comes from.** A suite declares `stop:` in YAML, next to `gate:`. It is a
`StopPolicy` model with `extra="forbid"`, so a misspelt key is a load error. Each `opseval run`
flag replaces only its own field for one run. The boolean flags have `--no-…` forms, so a flag
can also switch a suite's policy off. `--fail-fast` keeps working as before. The result records
the policy in force (`SuiteResult.stop`).

**What each policy does.** A policy only decides whether the next scenario starts. It never
changes a verdict.

- `fail_fast`: stop after the first failure the gate blocks on (unchanged from ADR-0032).
- `max_consecutive_failures: N`: stop after N failed scenarios in a row. Only a pass resets the
  count. A skip or an inconclusive scenario says nothing about whether the agent recovered.
- `stop_on_critical_failure`: stop after a `critical` or `blocker` scenario fails.
- `stop_on_rate_limit`: stop when a rate limit outlasted its retries and ended an attempt,
  either at the agent's endpoint or at an LLM role.
- `max_wall_clock_seconds`: start no scenario once the run has taken this long.

Rate limits are detected by type, not by reading error text. `RateLimited` is a marker mixed
into `TransportRateLimited` (a `TransportError`) and `LLMRateLimited` (an `LLMInfraError`), so
each keeps its place in the core rule: a harness failure, not judged. The driver notes
`rate_limited` on the record, and the runner reads that note. A judge rate-limited while
scoring is not covered: it is already an `infra_error` named "not measured", and it happens
after the attempt.

**Cancellation.** `cancel.json` in the run folder is a cancel request. A file works across
processes, so a run the CLI started can be cancelled from the console and the other way round.
`opseval cancel <run_dir>` writes it, and refuses (exit 3) when the run has already finished.
The runner checks for it before every scenario and before every attempt, so a cancel takes
effect within one attempt.

**What a stop leaves behind.**

- Each scenario not run gets a `skipped_precondition` verdict with `skip_kind` `fail_fast`,
  `stopped` (every other policy) or `cancelled`, and a reason naming the policy.
- A scenario a cancel interrupts is **not folded** into a pass from its finished attempts.
  - It is skipped as `cancelled after k of n attempts`, and its records stay on disk.
  - **Unless it has already failed for certain:** if it would fail even had every attempt it did
    not run passed, it is `failed`, folded from the attempts that ran. A cancel must not hide a
    real agent failure as "not run". With the default threshold of 1.0, one failed attempt is
    enough.
  - The verdict keeps those attempts and `cut_short_of` (the planned repeats). `opseval rescore`
    then applies the same rule rather than re-folding over the attempts that ran, which could
    pass.
- `SuiteResult.stopped` (`by`, `after`, `detail`) says why the run ended early. The console
  summary, the Markdown gate summary, JUnit (`stopped_early`) and the HTML report all name it.
- The result is written as usual, so a stopped run still has a gate, a report and sinks. A
  re-score keeps `stop` and `stopped`.
- A policy that fires on the last scenario, or a cancel after it, left nothing out. Such a run
  did not stop early, and `stopped` stays unset.

**Checked between scenarios, not inside one.** A policy firing mid-scenario would force the
same choice as a cancel: throw away attempts, or fold a partial verdict. Only an explicit cancel
justifies that cost.

## Consequences

- A stopped or cancelled run is never "safe to release": it has skips. If nothing was judged,
  the gate blocks and the exit code is 2. A failure that happened before the stop still exits 1.
- Without a cancel or a policy, Ctrl-C (exit 130, ADR-0030) is still the only way to stop a
  run, and it leaves no result. A cancel request is the clean way.
- The console builds on the same pieces, with no runner behaviour of its own:
  - `POST /api/v1/runs/{id}/cancel` writes `cancel.json` (`by: console`). It answers 409
    `RUN_NOT_RUNNING` for a finished or interrupted run, 409 `RUN_FINISHING` once every attempt
    has run and the result is being written (as `opseval cancel` exits 3 then), and
    `accepted: false` when a cancel was already requested. A cancel that races the run's last
    moment can leave a `cancel.json` in a finished folder; nothing reads it after the run ends.
  - A running run's progress carries `cancelRequested`.
  - `LaunchIn.stop` overrides fields for one run. A null field keeps the suite's value.
  - `SuiteIn.stop` edits the suite's `stop:` block (omitted: kept; null: removed), and
    `SuiteOut.stop` shows it.
  - Run detail carries `stopPolicy`, `stopped`, and each test's `skipKind` and `cutShortOf`.
  - Every dropdown names what it falls back to ("stop (suite)").
- Partner suites with many repeats may want a stop *inside* a long scenario (for example, a
  wall-clock limit per scenario). That is a separate decision, because of the partial-verdict
  problem above.
