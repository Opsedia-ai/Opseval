# ADR-0020: Preflight health checks

- **Status:** Proposed
- **Date:** 2026-10-02
- **Deciders:** Opseval maintainers

## Context

FR-CTL asks for preflight health checks — SUT reachable, token mintable, LLM keys valid, trace
source reachable — "that turn outages into `skipped_precondition` instead of failures".

The need is concrete. Point a suite at the wrong port and every scenario burns its repeats
producing transport errors. Forget `OPSEVAL_JUDGE_API_KEY` and every judge-only scenario reports
`JUDGE_UNAVAILABLE` and folds to inconclusive. Both are harness problems; both are knowable
before the first frame is sent; and both are far more useful said once, up front, than discovered
twenty scenarios later.

But the phrase "turn outages into `skipped_precondition`" cannot be applied uniformly, and
getting that wrong is worse than having no preflight at all. A skipped scenario is only a gate
**warning**: `release_gate` puts it in `warnings`, not `blocking`, so the gate status stays
`pass`. So "skip everything" on an unreachable agent would print **"Safe to release"** for a run
that never reached the agent at all. That is the precise inversion the one rule exists to prevent,
and it is exactly what a first implementation of this ADR did before the distinction below was
drawn.

## Decision

Preflight runs in `launch()` once the clients exist and before the scenario loop, concurrently,
and the result is recorded on the `SuiteResult` so it is auditable afterwards.

**The agent is not a dependency — it is the subject.** If the target is unreachable, `launch`
raises `ConfigError` → **exit 3** (harness/configuration error), names the target and the reason,
and runs nothing. Exit 3 is the documented code for "we could not do the job", the message points
at `--no-preflight` for anyone who wants to try anyway, and because the pytest plugin turns a
`ConfigError` into an error on every collected item, an unreachable agent can never read as a
pass or a skip.

**A dependency outage skips only the scenarios it actually silences.** A scenario whose scorers
*all* need the judge cannot produce a verdict without it, so it becomes `skipped_precondition`
with the reason. A scenario with both deterministic and judge scorers keeps running: skipping it
would throw away the checks that *can* run, which is its own kind of dishonesty. The same rule
applies to trace-backed scorers and to the LLM persona.

**Reachability is not validity, and the check says which it proved.** Any HTTP answer — 401, 404,
anything — proves the host is up, and a WebSocket server that refuses the upgrade with an HTTP
status is up too. That is all a probe can honestly claim, so a configured and reachable LLM
endpoint reports `unverified`, not `ok`, with the note that the key was not tested. `--verify-llm`
spends one token per role on a real completion and upgrades it to `ok`. The default is off because
a health check should not silently cost money, and `unverified` is deliberately **not** blocking:
an unproven key is a caveat, not grounds to refuse to run.

**A probe is skipped rather than guessed.** When a transport factory is injected (tests, the
`python` transport, an embedded harness) there is no host to reach, so the SUT check reports OK
with that reason. Probing a URL the run will never open would invent a failure it could never
have hit.

The five checks are `sut`, `token`, `simulator`, `judge`, `traces`, each reporting one of `ok`,
`unavailable`, `not_configured`, `not_needed` or `unverified`. `not_configured` is separated from
`unavailable` because the fix differs: one is a missing setting, the other a dead endpoint.

`opseval preflight <suite> --profile … --target …` runs the checks alone, for debugging a CI
setup without running a suite: exit 0 when everything needed is available, 2 when something is
missing, 3 for a configuration error. Note the asymmetry with `run`: standalone preflight
*reports* an unreachable agent (exit 2, nothing was measured), while `run` *refuses* (exit 3,
nothing was attempted).

## Consequences

- A misconfigured run now costs one round of checks instead of a full suite against a dead
  endpoint, and the failure names the thing that is wrong.
- `opseval run` against an unreachable agent changes from "every attempt is a transport failure,
  exit 3" to "nothing ran, exit 3" — the same exit code, reached sooner, with a better message.
  One pytest-plugin test's expected output changed accordingly; its contract (an error for every
  scenario, never a pass) is unchanged.
- Preflight adds a few seconds and, for HTTP targets, one unauthenticated GET to the agent and to
  each LLM base URL. Nothing is sent to the agent beyond a connection, and no conversation or
  credential is involved beyond the bearer the transport would use anyway. `--no-preflight` turns
  it off entirely.
- A dependency that is up but broken in a way a probe cannot see (an endpoint that answers but
  rejects every completion, a trace source with the wrong project) still surfaces during the run,
  as before. Preflight narrows the window; it does not close it.
- `--verify-llm` is the only check that proves credentials, and it is opt-in, so the common case
  reports `unverified`. A team that wants key validity gated in CI has to ask for it. That is the
  honest trade, but it does mean the default run does not fully satisfy FR-CTL's "LLM keys valid".
- The report is on `SuiteResult`, so the HTML report and the gate summary could surface it. They
  do not yet; today it is in `result.json` and the console log only.
