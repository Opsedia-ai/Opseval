# ADR-0036: The judge's transcript is fenced data

- **Status:** Proposed
- **Date:** 2026-10-09
- **Deciders:** Opseval maintainers

## Context

An LLM judge reads the conversation it grades, and part of that conversation is written by
the system under test. The agent's text is the one input in a judge's prompt that the graded
party controls. Until now the judge saw the conversation as plain `SPEAKER: text` lines inside
a fixed `<transcript>` tag, followed by lines the harness itself recorded (checkpoints, how the
conversation ended, the redaction note). Plain text cannot keep those apart: words in a turn can
look like a new line, another speaker or a harness fact, and a fixed tag is known in advance.

Judges already refuse to follow instructions in the transcript and must quote it, and the
security judges vote three times. But a judge that cannot tell where the agent's words end
grades on a premise the agent chose, which is the core rule failing in its most dangerous
direction: a real failure passed.

## Decision

The prompt separates what the harness knows from what it is grading:

- **Harness facts are trusted, and outside the fence**: checkpoints and the customer's
  decisions, the terminal state, and the redaction note.
- **The conversation is untrusted data, as JSON lines**: one object per turn, with
  `{"turn", "speaker", "text"}`. JSON escaping keeps a turn's text in its own string, so it
  cannot start a new line, change speaker or add a fact. Text is kept verbatim (`ensure_ascii`
  off, so Arabic stays readable and quotable).
- **The fence is named with a fresh random nonce on every ballot**
  (`<conversation-<16 hex>>`). Text inside it cannot close it, because nobody who wrote that
  text saw the name. Ballots of a multi-vote judge get different names.
- **Quotes are checked against the turns' own text and the harness facts**, never against the
  JSON encoding. A quote copied with JSON escapes (`\"`) is accepted in its unescaped form. A
  quote from nowhere is still `JUDGE_INVALID_QUOTE`, an `infra_error`.

Every judge moves to **version 2**, and `TRANSCRIPT_FORMAT` (`json-lines-nonce-1`) is part of
every judge's `prompt_hash`. So `opseval rescore` names the change and never reuses a grade
given to the old format without a judge (ADR-0013, ADR-0017).

## Consequences

- Judge scores before and after this change are not directly comparable. Re-scoring a saved
  run with a judge configured re-grades it; without one, judge scores are not reused and are
  reported as not measured.
- The nonce makes each ballot's prompt unique, so a provider-side prompt cache cannot serve
  one ballot's answer to another. That was already the intent for multi-vote judges.
- This does not make a judge immune to persuasion inside the fenced text. The defences stay
  layered: instructions to treat the conversation as data, mandatory verified quotes, scores
  computed by Opseval rather than the model, three votes where it matters, and calibration
  against human labels (ADR-0018) to measure what gets through.
