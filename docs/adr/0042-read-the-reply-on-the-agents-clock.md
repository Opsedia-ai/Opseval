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

## Decision

### `protocol.replies_read`, always on, on the harness axis

A new deterministic check fails when either holds, counting the conversation only (handshakes
aside, and probe answers, which `protocol.probes_passed` grades on their own):

- the agent sent frames and the profile read nothing from any of them: none had the text field,
  and none was classified as a checkpoint, result, error or notification;
- frames were classified as messages and not one of them had the text field: the kind is mapped,
  the text is not.

What counts is the text field being **present**, whatever its value:

- **`""` and `null` count as read.** The path matched and the agent said nothing, which is the
  agent's failure. Treating it as unread would hide a real agent failure as "not judged", the
  other half of the core rule. No built-in check yet fails an agent that says nothing: that is a
  coverage gap, tracked in `docs/ATTENTION.md` until the next change closes it.
- **Partly unread is still a pass.** An agent can legitimately send frames a profile does not
  map. The counts are in `details` (`received`, `unread`).
- **Nothing received at all is not this check's business.** That is the agent not answering,
  which is already `TIMEOUT`.
- The failure says what the replies looked like (`shape`): their keys, empty objects, JSON that
  is not an object, or not JSON at all.

The engine adds it to every attempt (`scoring.ALWAYS_ON`), whatever the scenario lists, and always
with its own spec: a scenario that lists it with `threshold: 0` cannot switch it off. A check
whose job is to catch a wrong configuration cannot rely on the author remembering it, or let the
author weaken it. It is deliberately **not** in `effective_specs`, which stays what the scenario
asked for: preflight and the runner use that to decide which dependencies a scenario needs, and a
check that measures nothing about the agent must not change whether a judge outage skips a
judge-only scenario. Its id lives in the models (`models.results.UNREAD_REPLIES`), so the scorer,
the engine and the verdict rules share one spelling.

### A failed harness-axis score invalidates the attempt, unless the agent failed anyway

`attempt_verdict` invalidated an attempt only for `judge.persona_consistency`, by id. It now
invalidates on any failed **harness-axis** score, which is what the axis has always meant
("grades the harness; never counts against the agent"). The persona's wording is unchanged; any
other harness check names its own reason. This also covers scorer plugins that declare the
harness axis.

One exception, because unread replies leave only the *frame* checks blind: when the unread
replies are the only harness failure, an agent failure proven some other way still stands, and
the attempt is **failed**:

- a check that failed on the agent's own traces (`details.source == "trace"`), such as a forbidden
  tool that ran;
- any failed check when the attempt ended `TIMEOUT`: the agent did not answer in time, whatever
  the mapping.

### Every attempt unread is exit 3

A run where every attempt was lost by the transport **or** invalidated because nothing was read
exits 3 (harness or configuration error), not 2. A profile that cannot read the agent is a
configuration problem as certainly as an unreachable agent is. A persona breaking character on
every attempt still exits 2: that can happen by chance, and it is not a configuration error.

### The turn is timed on the agent's clock

- `Session.send(frame, *, timeout=None)`: the driver passes the scenario's turn timeout (or a
  probe's own). Transports where sending waits for the answer use it; websocket ignores it,
  because `receive` bounds its wait. `None` keeps the transport default.
- httpx's `timeout` bounds each phase (connect, write, every read), not the request, so a body
  that trickles one byte at a time could hold a turn open indefinitely. http-json and openai-chat
  now bound the whole turn with `asyncio.timeout`, as sse already did, with connecting held to a
  share of the turn (`transport.base.turn_timeout`) so a refused connection stays a harness error.
- The python transport bounds an async agent the same way, and only converts its *own* deadline
  into a timeout: a `TimeoutError` the agent raises itself is not the turn running out. A
  synchronous agent cannot be interrupted, but an answer that arrives after the limit is late.
- **Where a frame is stamped depends on the transport** (`Session.replies_in_send`). Where
  `send()` waits for the answer (http-json, openai-chat, sse, python), the frame and the latency
  clock are stamped **before** `send()`: a tool the agent ran after receiving an approval is now
  after the approval. A streaming `send()` (websocket) may first refresh a token or reconnect,
  back off and resend, so there the frame reached the agent when `send()` returned; stamping it
  earlier would place a tool the agent ran in that gap *after* an approval it had not yet seen.
- A frame whose answer timed out, or whose wait the attempt deadline cut short, is still
  recorded, and so is what it was: the customer's turn is counted and a checkpoint decision keeps
  its `InterruptRecord`, so a tool the agent ran on that decision is checked against it.
- On a request/response transport a probe's answer is awaited inside `send`, so a timeout there
  is now a failed, unanswered probe (ADR-0025's rule), not the end of the attempt.

Verified against the case that found it: the booking agent that approved, booked, then took four
seconds to answer now passes, where it was failed with exit 1. Its `book_table` ran within 2 s of
the approval being sent, so that check is honestly *not measured* (`CLOCK_SKEW_AMBIGUOUS`); a
smaller `traces.clock_skew_ms` measures it when both clocks are trusted.

### Not changed: the skew window between two trace spans

`tools.ordering` applies the trace skew window to two spans of the agent's trace, so a lookup
followed quickly by an action is *not measured*. Ordering two spans exactly would be right when
they share a clock, but one trace can hold spans from several services and hosts, and dropping the
window there could fail a correct agent. That needs its own setting (a span-to-span tolerance the
profile declares) and is left for a later change.

## Consequences

- Every score list gains `protocol.replies_read`. `opseval rescore` adds it to old records (it is
  deterministic, so the fresh score is kept) and lists it as *added*; a scorer the new run no
  longer has is listed as *removed*.
- Old records from http-json, openai-chat, sse or python runs keep the timestamps they were
  written with: the clock fix applies to new runs only. Re-scoring an old traced run can still
  misplace a tool call; re-run it instead.
- `Session.send` has a new keyword argument and sessions a `replies_in_send` flag. There is no
  transport plugin group yet, so nothing outside the core implements them.
- Still open: an agent-facing check that the agent answered every turn with text (in
  `docs/ATTENTION.md`), profile-aware defaults (a chatbot's `scorers: default` still includes
  `flow.phase_order`), mapping replies whose text sits at a nested path, and the span-to-span
  tolerance above.
