# ADR-0024: Tool argument schemas, and naming vacuous passes

- **Status:** Proposed
- **Date:** 2026-10-03
- **Deciders:** Opseval maintainers

## Context

FR-SCR-04 lists `tools.args_valid` **(JSON Schema)** among the trace-backed scorers, as a
must-have for v0.2, and requires every trace-backed scorer to return `infra_error` with reason
`TRACE_UNAVAILABLE` when there is no trace. It was the only one of that list still missing.

Checking *that* a tool ran says nothing about *how* it was called. An agent that calls
`create_refund` with the wrong order id, a negative amount, or an extra field it invented has done
something a passing `tools.expected_called` cannot see.

Building it surfaced a second, pre-existing problem, described below.

## Decision

### `tools.args_valid`

A tool declares `args_schema` in the SUT profile, and the scorer validates every observed call's
arguments against it with the `jsonschema` library. It scores the share of checked calls whose
arguments validated, and names the offending field and reason for each failure.

`requires = "trace"`, because arguments are only visible in traces — frames carry evidence that a
tool ran, never what it was given. Frames-only evidence is `TRACE_UNAVAILABLE`, as FR-SCR-04
specifies.

Three cases are deliberately *not* the agent's fault:

- **No tool declares an `args_schema`** → `infra_error` (`NOT_MEASURABLE`). A pass would claim
  the arguments were verified against a rule that does not exist.
- **A declared schema is not valid JSON Schema** → reported as *the schema* being broken, by
  catching `SchemaError` separately. Letting it reach the engine would produce an anonymous
  `SCORER_ERROR`, and the author's mistake would read as the agent misbehaving.
- **A called tool has no schema** → counted in `unchecked` and reported, never failed. The same
  for an undeclared trace call, which `details.undeclared_trace_calls` already surfaces.

`jsonschema` becomes a core runtime dependency rather than an optional extra. The scorer is M for
v0.2, and a must-have scorer that silently does nothing without an extra is not shipped. It and
its tree (`jsonschema-specifications`, `referencing`, `rpds-py`, `attrs`) are all MIT, which the
licence allowlist permits; `make audit` passes clean.

### Vacuous passes are named

Building the "no call to a schema'd tool" case meant deciding what a pass on no evidence should
look like — and that exposed something already in the codebase. Five scorers return
`result(1.0, vacuous=True)`: `flow.rejection_routing`, `tools.expected_called`,
`tools.forbidden_not_called`, `tools.ordering` and `tools.call_success`. **`vacuous` went into
`details` and was surfaced nowhere** — not the HTML report, not the gate summary, not JUnit, not
the console.

"No forbidden tool was called" is trivially true when nothing is forbidden. The pass is honest,
and it is evidence of nothing. Presented without a word, it is indistinguishable from a real pass,
so a suite whose rules do not actually apply reads exactly like a suite that verified them.

So `TestVerdict.vacuous_scorers` and `SuiteResult.vacuous_scorers` now feed all four outputs,
following the pattern ADR-0017 set for `degraded`. `tools.args_valid` then keeps the established
convention — a vacuous pass with a note — rather than inventing a stricter rule for itself, which
would have left the five existing scorers inconsistent with the newest one.

As with `degraded`, it changes **no** status, pass rate or exit code. A vacuous pass is a caveat
on a pass, not a verdict of its own, and a test asserts the exit code is unmoved.

## Consequences

- An agent can now be checked on *how* it calls a tool, not just whether it did. Teams that
  declare schemas get argument validation free on every traced run.
- Suites whose rules never fire will start saying so. That is the point, and some reports will
  grow an "ℹ️ Passed vacuously" block that was not there before — most often
  `tools.forbidden_not_called` on a scenario with nothing forbidden, which is a fair prompt to ask
  whether that scorer is earning its place in the pack.
- One new core dependency, five packages in total. The core was seven dependencies and is now
  eight.
- Argument validation needs **full** trace coverage, so a team without a trace source gets
  `TRACE_UNAVAILABLE` rather than a pass. That is honest but it does mean the scorer is useless to
  them, and adding it to a suite would turn every run inconclusive — so it is not in the default
  pack.
- Only the arguments are checked, not the *result* of the call. A tool called with perfect
  arguments that returns nonsense is `tools.call_success`'s business, and neither scorer reads a
  tool's output.
