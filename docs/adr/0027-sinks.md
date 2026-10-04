# ADR-0027: Sinks — publishing a finished run

- **Status:** Proposed
- **Date:** 2026-10-05
- **Deciders:** Opseval maintainers

## Context

Three v0.5 integration requirements are the same shape:

- **FR-INT-04** (S): score push to Langfuse
- **FR-INT-07** (S): webhooks on run completion
- **FR-INT-05** (C): issue publisher for failed critical scenarios

The architecture already unifies them: a `Sink` plugin over `opseval.sinks`, contract
`async publish(run_result)`, listed as "Langfuse scores, webhooks, issue trackers". Building them
one at a time would have produced three ad-hoc publish paths.

## Decision

`opseval.sinks` with `Sink.publish(result) -> str`, two built-ins, and the same entry-point
discovery the scorer plugins use (ADR-0022).

### A sink cannot change a verdict, and must not fail silently

These pull in opposite directions and both matter.

Publishing happens after the result exists, so a sink has nothing to say about the agent.
`publish_all` catches every exception, and a test asserts the `SuiteResult` is byte-identical and
the exit code unmoved after a sink raises.

But a failure is still **reported** — in `result.sinks`, the console, and the gate summary as
"⚠️ Not published". The contrast with telemetry (ADR-0021) is deliberate: telemetry may fail
silently because nobody asked for it, whereas a sink *was* asked for, and an operator believing a
notification went out when it did not is the failure this feature would otherwise introduce. One
sink failing does not stop the others.

Sink outcomes are recorded on the result, so a failed publish is visible after the fact rather
than only in whatever console scrollback someone kept.

### Destinations are checked before the run

`build_sinks` runs in `launch()` *before* the first frame. A destination that is not in
`OPSEVAL_ALLOWED_HOSTS`, or plain HTTP off loopback, is a `ConfigError` — exit 3 — because
discovering a bad destination after a suite has something to publish is too late, and a webhook
URL receives the whole run.

### The webhook sends the whole result

Not a summary. This is the operator's own endpoint and they already have this data; trimming it
would only make the hook less useful. The redaction that matters still holds, because
`config_digest` redacts secrets before they reach a `SuiteResult` at all — verified end to end
against a live local server, asserting every key- and token-shaped field in the delivered payload
is `None` or `***redacted***`. That is a property of the result, not trust in the destination.

### An unmeasured verdict is not pushed as a number

`judge.passed → 1.0` and `failed → 0.0`, but `inconclusive`, `skipped_precondition` and `blocked`
are pushed as a **categorical** score carrying the state, with no numeric value. A 0 on a Langfuse
dashboard is indistinguishable from "the agent failed", and the whole point of the three-state
contract is that those are different things. The returned detail says how many were measured and
how many were not.

An attempt with no session id is skipped with a reason rather than invented — there is nothing to
attach a score to.

### `AttemptVerdict` gained `session_id`

The score push needs to know which agent session a verdict judged. The session id lived only on
the `RunRecord`, which would have meant widening the sink contract to `publish(result, records)`
and contradicting the architecture. Carrying it on the verdict is the smaller change and makes a
verdict self-sufficient for correlating back to a trace.

This changed `result.json`, and `opseval schemas --check` (ADR-0026) caught the drift on the next
run — the guard doing its job a day after it was built.

## Consequences

- Langfuse users see Opseval's verdict next to the conversation it judged, and anyone with an
  HTTP endpoint gets the full run on completion. FR-INT-05's issue publisher is now a sink someone
  can write without touching the runner.
- Third parties can ship sinks, so `opseval.sinks` joins `opseval.scorers` as a public,
  stability-promised surface. Discovery fails closed for the same reason: an operator who
  installed a publisher and got silence would reasonably assume it had published.
- **A failed publish does not fail the run.** That is deliberate — the verdict is about the agent
  — but it does mean a team relying on a webhook for notification needs to watch for the warning,
  because CI will still be green. A `--fail-on-sink-error` option is the obvious next step and is
  not built.
- The score push makes one request per attempt with a session id. On a large suite with high
  `repeats` that is a lot of small requests, with no batching.
- Nothing retries. A sink that fails is reported and that is all, because a retry loop after the
  result is written would delay the exit code for a side effect.
