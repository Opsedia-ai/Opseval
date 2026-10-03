# ADR-0025: Protocol probes

- **Status:** Proposed
- **Date:** 2026-10-04
- **Deciders:** Opseval maintainers

## Context

FR-RUN-07 asks for protocol probes — raw frames with expected responses — and FR-RUN-04 already
names the state they belong to: `CONNECT → PROBES → OPENING → …`. The driver's docstring listed
every state except `PROBES`.

A conversation exercises the protocol incidentally. A probe exercises it on purpose: does the
agent answer a resume frame, report its version, reject a malformed request? Those are protocol
properties, and finding them out by inference from a failed conversation is slow and ambiguous.

## Decision

`profile.probes` declares a list of `{name, send, expect, timeout_seconds}`. The driver sends each
one after connecting and before the opening message, records what came back, and
`protocol.probes_passed` grades the results. Probes live on the **profile**, not the scenario,
because they describe the wire protocol rather than a customer's goal.

`expect` is a subset match — `kind` (through the profile's own `kinds` mapping), `fields` (exact
values) and `has_fields` (presence) — so an agent may send more than the probe asks about.

### Probes never abort the conversation

A failed probe is recorded and the attempt continues. A protocol fault and whatever it causes
downstream are both worth having in one record, and aborting would replace a conversation's worth
of evidence with a single terminal state.

### Who is at fault

| What happened | Result |
|---|---|
| Wrong or missing field in the reply | Failed probe → the agent's fault |
| No answer within the probe's timeout | Failed probe, `answered=False` → the agent did not answer, which counts against it |
| Transport error while probing | Not caught here; `run` turns it into `transport_failure` → the harness lost the session, attempt *not judged* |
| Probes declared but the attempt died before `PROBES` | `infra_error` (`NOT_MEASURABLE`) with the terminal state — never a pass |
| No probes declared | Vacuous pass, which ADR-0024 made visible in every output |

### An error reply fails, even an empty expectation

This is the one decision that came from running it rather than designing it. A probe with an empty
`expect` asserts "the agent answered". Pointed at the reference agent, a `ping` probe **passed** —
and the reply was `{"type": "error", "code": "unknown_frame"}`.

The agent had demonstrably not handled the frame. Passing there would make the loosest expectation
the most misleading one, which is the opposite of what a default should do. So an `error`-kind
reply now fails unless the probe declares `expect.kind: error`, and the failure says so:

> the agent answered with an error frame (unknown_frame); if that is the expected answer, declare
> `expect.kind: error`

Expecting an error on purpose is a real protocol test — "a malformed frame is rejected with a
code" — so it stays available, it just has to be asked for.

Checked while verifying this: the reference agent sends nothing unsolicited on connect, so a probe
reads its own reply rather than a greeting the agent volunteered.

## Consequences

- A protocol fault is now a named, scored finding instead of an inference from a strange
  conversation, and the probe's frames carry `sent_seq` / `received_seq` so the evidence is
  findable in the record.
- Each probe costs one round-trip per attempt, so three probes on `repeats: 3` is nine extra
  round-trips per scenario. Probes are profile-wide, so that cost applies to every scenario run
  against it. They are not in the default scorer pack.
- `protocol.probes_passed` uses the `output` axis. Protocol conformance is about what the agent
  produced, and adding an axis would have changed the gate's shape for one scorer.
- **A probe reads the next frame that arrives.** The reference agent volunteers nothing on
  connect, but an agent that sends an unsolicited greeting, or a late frame from a previous probe,
  would have it read as the answer. Correlating replies to requests needs a request id the
  protocol may not have, so this is a real limitation rather than something the harness can fix
  for every agent.
- Probes are sent as `FrameKind.UNKNOWN`, which is honest — they are raw frames, not canonical
  ones — but it does mean a probe frame appears as `unknown` in the transcript table.
