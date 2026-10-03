# ADR-0023: Flow map

- **Status:** Proposed
- **Date:** 2026-10-03
- **Deciders:** Opseval maintainers

## Context

FR-RPT-05 asks for a flow map of expected versus observed phase transitions, and UJ-04 places it
in the investigate-a-failure path: verdict → heatmap → transcript, timeline, **flow map**, judge
ballot.

`flow.phase_order` already decides pass or fail on the phase sequence. What it cannot do is show
the *shape* of what happened — which phase the conversation jumped from, what it skipped, where it
came to rest. That is what someone opening a failed run actually wants, and a score cannot carry
it.

## Decision

`build_flow_map(record, profile)` reads the frozen record and classifies each observed transition
against `profile.phases`: `expected` (straight to the next declared phase), `skipped` (forward
over one or more, naming which), `backward` (to a phase already passed), or `unknown` (either side
is not a declared phase). It also reports the phases never reached, the undeclared phases the
agent used, and where the conversation came to rest.

The map is rendered per attempt in the HTML report, open by default when it is not clean.
`render_html` grows an optional `profile` parameter to supply the declared order; **without it the
map reports itself unmeasured rather than inventing an expected order.**

### It refuses to guess

A profile with no phases, or a conversation with no phase markers, produces `measured=False` with
the reason, and the report prints that reason. Rendering an empty diagram would read as "the agent
did nothing", and an absent measurement must never look like a zero. An undeclared phase is shown
as `unknown`, not silently dropped — the agent said something about its own state and the operator
should see it.

### Two corrections found by running it

**The opening transition is not a skip.** The first implementation classified "the first marker
seen is not the first declared phase" as `skipped`. Running it against the reference agent showed
why that is wrong: the demo's first marker is `lookup`, not `intake`, so *every healthy run*
reported an unexpected transition. There is no evidence about what preceded the first marker, and
an early phase may simply not be one the agent marks. A signal that cries wolf on the happy path
teaches people to ignore it, so the opening transition is `expected` and the leading phases appear
in `never_reached`, which is the accurate statement.

**There is no `repeated` step.** The first version had one, for a phase recurring with another in
between. It was unreachable: consecutive frames in a single phase are collapsed into one visit
before classification, so a transition's two sides are never the same phase, and revisiting a
phase is already `backward`. A test caught it, and the member was removed rather than shipped as
dead code.

### It changes nothing

The flow map has no verdict. It does not alter a status, a score, a pass rate or an exit code, and
a test asserts the result is byte-identical after rendering one. `flow.phase_order` remains the
thing that decides; the map only explains.

## Consequences

- Someone investigating a failure can see the path taken without reading the frame table, and the
  map opens itself when the path was not clean.
- The reference agent's healthy report now says "1 phase(s) never reached · Never reached:
  intake". That is true, and it is a fair question for the demo profile: either `intake` is a
  phase the agent should mark, or it should not be declared. The map surfacing it is the feature
  working, but it does mean the demo's own report carries a note it did not before.
- `render_html` has a new optional parameter. Both in-tree callers pass the profile; a third-party
  caller that does not keeps working, minus the flow map.
- Only *received* frames carry phases, so the map is the agent's own account of its progress. If
  an agent marks phases unreliably, the map inherits that, and `unknown` is as much as it can say.
- The map is computed at render time rather than stored on the `SuiteResult`. That keeps the
  result small and means `opseval rescore` regenerates it, but it also means the v0.5 console will
  need the records and the profile, not just the result.
