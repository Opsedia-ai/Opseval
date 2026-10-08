# ADR-0030: Closing four leaks in the one rule

- **Status:** Proposed
- **Date:** 2026-10-08
- **Deciders:** Opseval maintainers

## Context

The rule: *a harness problem must never look like an agent failure, and a real agent failure must
never be hidden as "not measured".* A review of the code on `main` found four places where it
leaked. None needed new machinery; each was a missing distinction.

1. **The gate said "Safe to release" about a suite that judged nothing.** `release_gate` treated
   every `skipped_precondition` and `blocked` scenario as a warning. A run where the judge was
   down and every scenario was judge-only produced `gate.status = "pass"` and a green "Safe to
   release" in the Markdown summary, next to exit code 2. ADR-0020 named this inversion for an
   unreachable agent and fixed it there, but the same path through dependency skips stayed open.
2. **An unmeasured scorer vanished from a passing attempt.** `attempt_verdict` leaves
   `infra_error` scores out of `measured`, which is right: they are not the agent's fault. But it
   meant an attempt could pass with its 3-vote security judge returning `JUDGE_DISAGREEMENT`
   ("never a pass", ADR-0014), and no output said so. Degraded (ADR-0017) and vacuous (ADR-0024)
   scores were already named everywhere; a score that never happened was not.
3. **A connection that never opened was blamed on the agent.** The HTTP transports caught
   `httpx.TimeoutException` and raised `TransportTimeout`. `ConnectTimeout` and `PoolTimeout` are
   subclasses of it, so "the TCP connection never opened" and "Opseval's own pool was exhausted"
   became the agent's fault, while `ConnectError` on the very next line was a harness error, and
   preflight (ADR-0020) calls an unreachable agent a configuration error. The WebSocket transport
   already got this right.
4. **An unexpected crash exited 1.** Every command handled `ConfigError`, but anything else that
   escaped (a full disk under `FileStore`, a template bug, a bug in Opseval) reached the
   interpreter, which exits 1: "the agent failed".

## Decision

1. **The gate blocks when nothing was judged**, with the reason `no scenario was judged`, even if
   every scenario was quarantined: a suite that judged nothing has proven nothing. Individual
   skips stay warnings. A first draft also blocked on skipped *critical* scenarios, but
   `skipped_precondition` covers both a dependency outage and the operator's own choice (the
   commit guard of ADR-0008), and the gate cannot tell them apart: blocking would have pushed
   teams toward `--allow-commit`. Gating on skips by reason waits for the gate policy.
   **"Safe to release" is said only when everything passed, was measured and loaded**
   (`SuiteResult.safe_to_release`: gate passed, exit 0, no unmeasured scorer). Otherwise a
   passing gate says "No blocking issues, but not everything passed" and names each gap (failed or
   skipped scenarios, unmeasured checks, files that did not load); the Markdown summary shows it in
   yellow and the HTML badge reads "PASS WITH GAPS". A block caused by undeclared criticality says
   so. Exit codes do not change. Which outcomes block will become a per-suite policy (a follow-up
   ADR); what each outcome *is* stays fixed in code.
2. **Unmeasured scorers are named wherever the verdict is**, as degraded and vacuous ones are.
   `TestVerdict.unmeasured_scorers` lists agent-facing scorers that returned `infra_error` in an
   attempt that was still judged. The harness axis is left out (it grades the simulator), and so
   are invalidated attempts (they already say why). It appears in the console summary, the
   Markdown gate summary, the JUnit XML (suite and case properties) and a banner in the HTML
   report. The verdict itself does not change.
3. **A timeout before the agent had the request is a harness error.** `transport.base` exports
   `NOT_SENT_TIMEOUTS = (httpx.ConnectTimeout, httpx.PoolTimeout)`, and every HTTP transport catches
   it before `httpx.TimeoutException`, mapping it exactly as it maps a refused connection
   (`TransportError`, or `TransportClosed` for SSE). Read and write timeouts still belong to the
   agent. SSE also bounds the whole turn with `asyncio.timeout`, which would fire before an
   equally long connect limit on a real hang; its connect and pool limits are therefore held to
   `min(10s, turn / 2)`, while the agent keeps the full turn to answer.
4. **The console script has a harness-error boundary.** `opseval` now points at
   `opseval.cli:entrypoint`, which runs the Typer app and turns any `Exception` that escapes into
   exit 3, printing the traceback and saying it is a harness problem. `SystemExit` passes through
   unchanged.

Each decision has tests that fail without it, and both halves of the rule are pinned where it
applies (a `ReadTimeout` is still the agent's fault on every HTTP transport).

## Consequences

- A suite with a judge outage no longer reads "Safe to release": it blocks if nothing was
  judged, and otherwise reads "No blocking issues, but not everything passed" with the skipped
  scenarios named. Exit codes are unchanged, so CI behaves the same.
- pytest-opseval still reports a skipped scenario as a pytest skip, consistent with skips being
  warnings here; it will need to follow the gate policy when that lands.
- A scorer that could not run is visible but still does not fail the attempt. Some scenarios need
  a check to be *present* for a pass to mean anything (a security judge on an injection scenario).
  A follow-up decision should let a scenario mark a scorer `required: true`, so that an unmeasured
  required scorer invalidates the attempt (`inconclusive`). That changes the verdict contract of
  ADR-0006, so it is left to its own ADR.
- Not fixed here, and still open: the attempt deadline (`asyncio.timeout` around the whole
  attempt) includes time spent waiting on the simulator, so a slow or rate-limited simulator LLM
  can still end an attempt as the agent's `TIMEOUT`. A profile with `committed_outcome` but no
  `commit_gate` is not refused at load time. Click turns Ctrl-C (and the SIGINT a cancelled CI
  job sends) into "Aborted!" with exit 1, the code for "the agent failed"; the job shows as
  cancelled, so the impact is small, but it is the same leak.
- The console server (ADR-0029) launches runs in the background and must follow the same rule as
  `entrypoint`: an exception there is a harness error with a visible status, never a silent
  absence. It does not yet; that is part of hardening the server.
