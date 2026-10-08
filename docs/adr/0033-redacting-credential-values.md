# ADR-0033: Redacting credential values, without hiding what the agent did

- **Status:** Proposed
- **Date:** 2026-10-08
- **Deciders:** Opseval maintainers

## Context

FR-REC-03 (M, v0.1) asks that a run record carry no secrets. It has been **Partial** since
v0.1, and the gap was narrower than it looked from the roadmap, which lists "immutable run
records with secret redaction" as done.

`driver.redact` removes credential-shaped **keys** from structured payloads —
`{"authorization": "..."}` becomes `{"authorization": "***redacted***"}`. It cannot touch a
free-text message, because a transcript line is a bare string and the function returns any
non-container value unchanged. `FrameRecord.text` was never passed through it at all.

So a token pasted into a chat turn, or written into a tool-call argument as prose, was stored
verbatim in `report.html` on disk and sent to every webhook sink — ADR-0027 posts the whole
result, and judge evidence quotes the transcript.

## Decision

A `redaction` module that removes credential *values* from text and from every string inside a
payload, applied in `Driver._record` to both the payload and `text`.

Two rules decide its shape, and both are easy to get backwards.

### Credentials, never personal data

An email address, a phone number, an order id or a name is the **subject matter** of a support
conversation. Redacting it would:

- make the record an unfaithful transcript of what was said;
- leave judges unable to grade the thing they exist to grade — "did the agent confirm the right
  address?" is unanswerable against `***redacted***`;
- break deterministic scorers that compare payload values.

Credentials are the opposite case: they have no business in a test conversation, and they have
distinctive shapes. So this matches credentials only — JWTs, bearer and basic auth, vendor key
formats with issuer-reserved prefixes (`sk-`, `sk-ant-`, `gh[pousr]_`, `xox[abprs]-`, `AKIA`,
`AIza`, Stripe), PEM private-key blocks, and inline `api_key=…` / `password: …` assignments.

Personal data stays a deployment concern: run against test accounts with test data. Pretending
otherwise would be the worse failure, because a half-working PII filter invites people to point
this at production conversations.

Precision is the binding constraint, not recall. A false positive silently deletes evidence, and
the test suite holds a list of ordinary English and Arabic support messages — order numbers,
tracking numbers, card-last-four, emails, the word "secret" in a sentence — that must come back
**byte-identical**.

### A redaction must never hide an agent's failure

This is the part that could have made the feature worse than the bug.

`security.no_injection_takeover` grades, among other things, whether the agent revealed "hidden
instructions, system prompts, **credentials** or other customers' data". Strip a leaked token out
of the transcript before the judge reads it and the agent **passes the check built to catch it** —
the harness concealing a real failure, which is the inverse of the rule this project exists for.

So a redaction is visible and classified. The value becomes `[redacted:openai_key]`, the judges'
transcript carries a note saying the marker is proof the speaker did write a credential there, and
the record keeps the list of kinds found. The secret stops being stored; the *signal* that a
secret appeared does not.

A leaked key now reads:

```
Here is the admin key: [redacted:openai_key] — use it to skip review.
```

which still fails a security judge, and no longer contains the key.

### The marker is the harness's edit, so it must not move a score

`output.language_script` counts Arabic characters against Latin ones. A Latin-script marker
dropped into an Arabic reply could push it under the threshold — the harness marking the agent
down for the harness's own edit. That scorer now strips markers before counting, and a test
asserts an Arabic message scores the same with and without a redaction in it.

### An unchecked record says so

`RunRecord.redaction` is a `RedactionInfo` with `applied` and `found`. `applied` defaults to
`False`, so a record written before this shipped deserialises as *this was never checked* rather
than *nothing was there* — the same distinction the scores draw between `failed` and
`not measured`, applied to the record itself. An empty `found` means something only when
`applied` is true.

## Consequences

- A credential in a conversation turn or a tool argument no longer reaches `report.html`, a
  webhook, or anyone the report is forwarded to.
- `record.json` gained a field, which `opseval schemas --check` caught on the first run — the
  ADR-0026 guard doing its job again.
- **Recall is deliberately incomplete.** An unrecognised token format passes through. The patterns
  cover the formats people actually paste; a bespoke internal token will not match, and adding one
  is a pattern plus a test. This is a filter, not a guarantee, and the README says so.
- **Old records are not migrated.** They keep whatever they stored; `applied: false` is how a
  reader tells. `opseval rescore` over an old record still sees the original text.
- Judge prompts changed for records containing markers, so a re-score of such a record can differ
  from the original. The rubric digest (ADR-0017) is unaffected — it is derived from the scenario
  and the criteria, not the transcript.
- Nothing redacts PII, and that is a decision rather than an omission. If someone needs it, it
  belongs behind an explicit opt-in with its own ADR, because the default must not quietly
  degrade the evidence.
