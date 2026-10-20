# ADR-0042: Read the agent's reply, on the agent's clock

- **Status:** Proposed
- **Date:** 2026-10-20
- **Deciders:** Opseval maintainers

## Context

Two independent reviews of adopting Opseval for agents other than the reference one (a code
audit, and four teams' agents driven end to end) found the same two breaches of the core rule.
Both come from things every new team does first: mapping a new wire format, and pointing Opseval
at an HTTP agent.

**A profile that reads nothing passes.** A chatbot answering `{"reply": "We open at 9."}`
against a profile that looks for `type` and `text` leaves every frame `unknown` with no text. The
transcript holds only the customer. `conversation.no_errors`, `output.terminal_state` and
`conversation.turn_budget` — the checks the `openai-chat` scaffold ships — all pass on nothing,
and the gate says *Safe to release*. The same happens to a `text/plain` agent, whose replies are
kept as `{"_raw": …}`. The mapping is the harness's configuration, so this is a harness problem
reported as a clean agent.

**A request/response turn was timed after the agent answered.** `Driver._send` stamped a sent
frame, and started the latency clock, after `session.send()` returned. On http-json, openai-chat,
sse and python, `send()` *is* the round trip. Three consequences:

- With traces, the approval was stamped after everything the agent did in reply. A booking agent
  that received the approval, ran `book_table`, then took four seconds to write its answer was
  failed with *book_table ran before confirm_booking was approved*: a critical failure, exit 1,
  for the harness's own clock. Under the 2 s skew window a shorter delay made the check
  ambiguous instead (not measured).
- Every reply's latency was about 0 ms.
- The scenario's `execution.turn_timeout_seconds` never reached these transports. They kept
  their constructor default (120 s), so a 2 s limit was not enforced and a 300 s one could never
  be reached: the agent was timed out at 120 s, and a timeout counts against it.

**Two spans of the agent's own trace were compared as if they came from two clocks.**
`tools.ordering` applied the trace skew window (default 2 s), which exists because Opseval's clock
and the agent's can disagree, to two tool spans from the same trace. A lookup followed quickly by
an action came back *not measured*, and an action run before its lookup inside the window was
hidden the same way: an agent failure reported as "could not measure".

## Decision

### `protocol.replies_read`, always on, on the harness axis

A new deterministic check fails when the agent sent at least one frame (handshakes aside) and
the profile read nothing from any of them. A frame is *read* when it had text at the mapped path,
or was classified as a checkpoint, result, error or notification.

- **An empty string counts as read.** `text: ""` means the path matched and the agent said
  nothing, which is the agent's failure. Treating it as unread would hide a real agent failure as
  "not judged", the other half of the core rule. (No built-in check yet fails an agent that says
  nothing; that is a coverage gap for the next change, not a harness question.)
- **Partly unread is still a pass.** An agent can legitimately send frames a profile does not
  map. The count is in `details` (`received`, `unread`); only "nothing at all" is decisive.
- **Nothing received at all is not this check's business.** That is the agent not answering,
  which is already `TIMEOUT`.

The engine adds it to every attempt (`scoring.ALWAYS_ON`), whatever the scenario lists, because a
check whose job is to catch a wrong configuration cannot rely on the author remembering it. It is
deliberately **not** in `effective_specs`, which stays what the scenario asked for: preflight and
the runner use that to decide which dependencies a scenario needs, and a check that measures
nothing about the agent must not change whether a judge outage skips a judge-only scenario.

### A failed harness-axis score invalidates the attempt

`attempt_verdict` invalidated an attempt only for `judge.persona_consistency`, by id. It now
invalidates on any failed **harness-axis** score, which is what the axis has always meant
("grades the harness; never counts against the agent"). The persona's wording is unchanged; any
other harness check names its own reason. This also covers scorer plugins that declare the
harness axis.

### Every attempt unread is exit 3

A run where every attempt was lost by the transport **or** read nothing exits 3 (harness or
configuration error), not 2. A profile that cannot read the agent is a configuration problem as
certainly as an unreachable agent is. A persona breaking character on every attempt still exits
2: that can happen by chance, and it is not a configuration error.

### The turn is timed from when sending began

- `Session.send(frame, *, timeout=None)`: the driver passes the scenario's turn timeout (or a
  probe's own). Transports where sending waits for the answer use it; websocket ignores it,
  because `receive` bounds its wait. `None` keeps the transport default.
- A sent frame's `at`, and the latency clock, are taken **before** `send()`. A tool the agent
  ran after receiving an approval is now after the approval, on any transport.
- A send that times out still records the frame, so the record shows what went unanswered.
- On a request/response transport a probe's answer is awaited inside `send`, so a timeout there
  is now a failed, unanswered probe (ADR-0025's rule), not the end of the attempt.
- The skew window applies only across clocks: `Evidence.compare(..., same_clock=True)` for two
  trace spans orders them exactly (equal instants stay ambiguous). `tools.ordering` is version 2.
  The tool-versus-decision comparisons in `tools.forbidden_not_called`, `tools.approval_gated`
  and `flow.checkpoint_gating` keep the window: one side of each is Opseval's clock.

Verified against the case that found it: the booking agent that approved, booked, then took four
seconds to answer now passes, where it was failed with exit 1. Its `book_table` ran within 2 s of
the approval being sent, so that check is honestly *not measured* (`CLOCK_SKEW_AMBIGUOUS`); a
smaller `traces.clock_skew_ms` measures it when both clocks are trusted.

## Consequences

- Every score list gains `protocol.replies_read`. `opseval rescore` adds it to old records (it is
  deterministic, so the fresh score is kept) and reports it as an added scorer.
- Old records from http-json, openai-chat, sse or python runs keep the timestamps they were
  written with: the clock fix applies to new runs only. Re-scoring an old traced run can still
  misplace a tool call; re-run it instead.
- `Session.send` has a new keyword argument. There is no transport plugin group yet, so nothing
  outside the core implements it.
- Still open, and named in `docs/STATUS.md`: an agent-facing check that the agent answered every
  turn with text, profile-aware defaults (a chatbot's `scorers: default` still includes
  `flow.phase_order`), and mapping replies whose text sits at a nested path.
