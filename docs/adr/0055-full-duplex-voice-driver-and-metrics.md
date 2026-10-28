# ADR-0055: Full-duplex voice driver and voice metrics

- **Status:** Proposed
- **Date:** 2026-10-28
- **Deciders:** Opseval maintainers (owner request 2026-10-28)

## Context

The voice bridge (ADR-0053) makes a call look like a chat: one side speaks, then the other. That
reuses the whole text pipeline, but it cannot test what makes a voice agent good or bad:

- Does the agent stop talking when the caller interrupts (barge-in)?
- Does it talk over the caller, or leave long dead air?
- How fast does it start speaking, and how long are its turns?
- Does it still understand the caller over background noise, or across Arabic and English in one
  sentence?
- Does it navigate a DTMF menu, or hand the call to a human when it should?

These need a driver that runs on a timeline, with both parties able to make sound at once. The
risk is that two drivers drift apart and one of them breaks the one rule. So the new driver must
pass the same fault-injection tests as the bridge.

## Decision

### A timeline driver beside the text driver

`opseval/voice/driver.py` drives an `AudioSession` directly. It does not replace `driver.py`: a
scenario runs on the bridge unless it declares `voice.duplex: true` or uses a duplex-only action.

- It plays the caller's turns as audio on the attempt's monotonic clock (ADR-0053), while it
  keeps reading agent events.
- It produces the same `RunRecord`, with `RunRecord.voice` filled, so scorers, judges, verdicts
  and reports need no second path.
- Checkpoints and the commit guard (ADR-0008) work as on the text driver. The default `stop_at`
  stays `commit_gate`.

### What the simulated caller can do

Declared in the scenario's `voice:` block, so it is part of the scenario's digest:

- **Barge-in:** interrupt the agent `after_ms` into its turn, with a given phrase.
- **Background noise** at a set SNR in dB, mixed in-house from bundled CC0 clips (street, café,
  car, call centre). The clip and its digest go into the record.
- **Silence injection:** the caller pauses mid-sentence or says nothing for `ms`.
- **Accent and voice per persona,** from the language pack's voice map (ADR-0059).
- **Arabic–English code-switching** within a sentence, written by the simulator and voiced with a
  voice that speaks both.

Noise and accent are things the harness does to the caller. If they make the agent's speech
unintelligible to Opseval's own STT, that is the harness's problem (ADR-0053's table), not the
agent's.

### Metrics

Each goes into `attempt_metrics` (ADR-0051) as one row per attempt. In files mode they live in the
record. A metric that could not be measured is NULL, and is shown as "not measured", never 0.

| Metric | Measured as |
|---|---|
| `voice.time_to_first_audio_ms` | call start to the agent's first `AgentSpeechStart` |
| `voice.turn_latency_ms` p50, p95 | caller's speech end to agent's speech start, per turn |
| `voice.barge_in_yield_ms` | caller's interruption start to agent's speech end; NULL if no barge-in was planted |
| `voice.talk_over_ratio` | share of agent speech time that overlaps caller speech |
| `voice.dead_air_ms` | longest gap with neither party speaking, outside planted silences |
| `voice.agent_asr_wer` | WER of the agent's own transcript against the caller's known text |
| `voice.asr_confidence` | mean and minimum of Opseval's STT confidence on agent turns |
| `voice.dtmf_navigation` | the expected digits reached the expected menu state |
| `voice.transfer` | a transfer happened, when, and to which target |

**Agent WER needs the agent's transcript.** The reference is free: Opseval knows exactly what the
caller said. If the agent exposes no `AgentTranscript`, the metric is `NOT_MEASURABLE`, never a
perfect or a zero score. The reference is the text before noise was added, so a high WER under
planted noise is the agent's result at that SNR, stated with the SNR.

### Scorers and the gate

- Deterministic scorers `voice.*` read the metrics and compare them with scenario thresholds,
  e.g. `voice.turn_latency_ms: {p95_max: 1500}`. A threshold on a NULL metric is `infra_error`
  `NOT_MEASURABLE`, named as not measured (ADR-0030).
- `judge.pronunciation_dialect_fit`: a quote-backed LLM judge (ADR-0036) over the transcript and
  the per-word confidence. Did the agent speak in a register and dialect that fits the caller's?
  Quotes must match transcript words. Low-confidence words are named as such in the harness
  facts, so the judge does not grade STT noise as the agent's pronunciation. The score is
  `degraded` when confidence is under `OPSEVAL_STT_MIN_CONFIDENCE`.
- Gate policies (ADR-0032) can block on these scorers like any other.

### Same rule, same tests

The duplex driver uses the same `AudioSession` and providers, so the ADR-0053 table applies
unchanged. The fault-injection suite runs every row against both drivers, plus three duplex rows:

- An agent that never yields to a barge-in fails `voice.barge_in_yield_ms` (its fault).
- An STT outage during a barge-in turn is `SttError` (not judged), not a failed yield.
- A planted silence is never counted as dead air.

## Consequences

- Voice behaviour (interruptions, latency, noise) is testable and can block a release.
- Two drivers to maintain. They share the session, the providers, the record and the tests; only
  the scheduling differs.
- Barge-in may need streaming STT (ADR-0054, not decided there). The first version detects the
  agent's speech end by energy and its own events, which needs no transcript.
- Bundled noise clips add a few MB to the package; they must be CC0 and listed with their source.

## Not decided here

- Default thresholds per metric. They come from design partners' agents, not from guesses.
- Multi-party calls (conference, warm transfer with a third voice).
