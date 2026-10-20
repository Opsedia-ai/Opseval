# SUT profile guide

A **SUT profile** (system under test) tells Opseval how your agent talks: how to read the frames
it sends, how to write the customer's messages and checkpoint answers, and what its conversation
looks like. Opseval is black-box: it knows nothing of your agent beyond this file.

Below is the profile of the reference refund agent, which speaks JSON over WebSocket.

## Reading frames

```yaml
kind_field: type              # which field says what a frame is
kinds:                        # your values -> Opseval's frame kinds
  session_created: handshake
  message: message
  needs_human: needs_human    # the agent asks for an approval (a checkpoint)
  result: result              # an outcome, possibly the irreversible one
  error: error
  notification: notification
text_field: text
phase_field: phase
checkpoint_field: checkpoint
payload_field: payload
outcome_field: outcome
turn_end_field: expects_reply # a message with this truthy ends the agent's turn...
turn_quiet_ms: 400            # ...otherwise this much silence does
```

A frame Opseval cannot classify is recorded as `unknown`, verbatim. An `error` frame counts
against the agent.

If the mapping is wrong, Opseval says so instead of passing. When the agent sends frames and the
profile reads nothing from any of them (no text at `text_field`, and no kind it maps), the
always-on check `protocol.replies_read` marks the attempt **not judged** and names the keys the
replies did have; a run where that happens on every attempt exits 3. An agent that answers with an
empty text is different: the field was read, the agent said nothing, and that counts against it.

## Writing frames

```yaml
user_message_frame: {type: user_message, text: "{text}", timezone: "{timezone}"}
resume_frame: {type: resume, checkpoint: "{checkpoint}", action: "{action}", feedback: "{feedback}"}
```

`{name}` placeholders are filled in for each message: `text` and `timezone` for a customer
turn; `checkpoint`, `action` (`approve` or `reject`) and `feedback` for a checkpoint answer.

## The conversation

```yaml
phases: [intake, lookup, proposal, confirmation]
checkpoints:
  proposal_approval: {phase: proposal}
  supervisor_review: {phase: confirmation}
commit_gate: supervisor_review      # runs stop here unless commit is allowed twice
committed_outcome: refund_issued    # needs a commit_gate, or the profile does not load
tools:
  lookup_order: {evidence_key: order_summary}
  create_refund: {evidence_key: refund_status, commits: true}
```

`phases` and `checkpoints` let the flow scorers check the order things happened in. `tools`
declares what the agent can call. Tool checks prefer a trace source (Langfuse or OTLP files) and
fall back to the evidence keys in frames, marked *degraded*.

## What the agent creates

```yaml
artifacts:
  - {kind: refund, path: data.refund_status.refund_id}             # in result frames (the default)
  - {kind: ticket, path: payload.ticket.id, frame_kinds: [needs_human]}
```

Each attempt's record lists what the agent created (`created_artifacts`: kind, id and the frame
that reported it), so you can find what a run left in a test tenant. The report and the console
show it, and say "nothing" when artifacts were looked for and none were found. With no
`artifacts:` declared, nothing is looked for, and nothing is claimed. A path can index a list
(`data.refunds.0.id`). An id that looks like a credential is redacted like any other value; the
artifact is still listed, with its id marked as redacted.

## Credentials and connections

- A static bearer token comes from `OPSEVAL_STATIC_TOKEN`. A `token:` block mints short-lived
  HS256 tokens per attempt instead, signed with `OPSEVAL_TOKEN_SIGNING_KEY`.
  `OPSEVAL_CREDENTIALS` names a credential provider plugin instead, for tokens from single
  sign-on, OAuth or a vault. It cannot be combined with a `token:` block.
- `reconnect:` resumes a dropped WebSocket session.
- `probes:` sends raw protocol frames once per attempt, before the conversation, and checks the
  answers.
- `sut_model:` names the model your agent runs on, so a judge from the same family is refused.

Validate a profile and the scenarios that use it with `opseval validate`. Editors get completion
from the published JSON Schemas (`schemas/v1/`).
