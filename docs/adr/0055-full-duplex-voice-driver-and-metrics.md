# ADR-0055: Full-duplex voice driver and voice metrics

- **Status:** Proposed (V1 built, see the implementation note)
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

Noise and accent are things the harness does to the caller. They never touch the agent's audio,
so they cannot make it unintelligible. Whether an empty transcript of the agent is the agent's
fault or Opseval's is decided as ADR-0053 decides it: by the STT health canary and the optional
second STT, never by default. The canary transcribes the caller's audio **before** noise is
mixed in, so planted noise cannot make a healthy STT look broken.

### Metrics

Each goes into `attempt_metrics` ([ADR-0051](0051-runs-and-blobs-in-the-database.md)): one row
per turn for a per-turn metric (`turn` set), one row per attempt for an attempt-level metric
(`turn` NULL). Rows hold raw values. Percentiles such as p50 and p95 are computed by analytics
([ADR-0052](0052-analytics-store.md)) and by the scorers from those rows, never stored folded. A
metric that could not be measured is a NULL value with a `reason`, shown as "not measured",
never 0.

In files mode the metrics live in the record. That record change bumps `storage.LAYOUT` again
(the next number at the time, after ADR-0053's `opseval-runs/3`); older layouts are still read
and show the metrics as not measured.

| Metric | Measured as |
|---|---|
| `voice.time_to_first_audio_ms` | call start to the agent's first `AgentSpeechStart` (per attempt) |
| `voice.turn_latency_ms` | caller's speech end to agent's speech start (per turn; p50, p95 computed) |
| `voice.barge_in_yield_ms` | caller's interruption start to agent's speech end; NULL if no barge-in was planted |
| `voice.talk_over_ratio` | share of agent speech time that overlaps caller speech |
| `voice.dead_air_ms` | silence while the agent is the party expected to speak (per turn; see below) |
| `voice.agent_asr_wer` | WER of the agent's own transcript against the caller's known text |
| `voice.asr_confidence` | mean and minimum of Opseval's STT confidence on agent turns |
| `voice.dtmf_navigation` | the expected digits reached the expected menu state |
| `voice.transfer` | a transfer happened, when, and to which target |

**Dead air is only the agent's silence.** Simulator time is not the agent's time
([ADR-0042](0042-read-the-reply-on-the-agents-clock.md)). A gap counts only when the agent is the
party expected to speak:

- after the caller finished a turn (its last sample sent) and before the agent's first audio;
- a pause inside an agent turn, between its `AgentSpeechStart` and `AgentSpeechEnd`.

A gap that ends in caller speech, a planted silence, and time the harness spends generating the
caller's next turn (simulator and TTS) never count. A gap the clocks cannot place is left out
and the metric is `degraded`, so a harness stall never reads as the agent's dead air.

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
- A planted silence, and the harness's own simulator or TTS time, are never counted as dead air.

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

## Implementation (V1, 2026-10-28)

Built:

- **The duplex driver** (`opseval/voice/duplex.py`). **Deviation:** not a `voice/driver.py`
  that drives the `AudioSession` itself, but `DuplexBridge`, a subclass of the voice bridge that
  only changes the scheduling. The text driver still runs the conversation, so checkpoints, the
  simulator and the commit guard (ADR-0008, default `stop_at: commit_gate`) are the same code on
  both, and the two cannot drift apart on anything but scheduling. A reader task pulls every
  agent event into an inbox for the whole call (stamped on arrival by the transport); the
  caller's audio is paced chunk by chunk on the attempt's clock and its turn ends when its last
  chunk has played (the attempt deadline is paused while the caller talks, as for the simulator:
  ADR-0042); an agent turn ends when its audio has played out (a chunk plays when it
  arrived or when the one before it ended, whichever is later) and, after its own
  `speech_end`, speech that follows is held for the next turn.
- **The scenario's `voice:` block** (`models/voice.py`, one additive `Scenario.voice` field):
  `duplex`, `barge_in` (`agent_turn`, `after_ms`, `say`; implies duplex), `silences`
  (`caller_turn`, `ms`, `at: before | mid`; a mid silence splits the turn at its middle word,
  one word puts it before), `noise` (`kind`, `snr_db`, `seed`), `caller_voice` (looked up in
  `OPSEVAL_TTS_VOICE_MAP`) and `code_switching` (an LLM caller is told to mix the languages in
  a sentence; it needs a `caller_voice` that speaks them all, so one voice says the whole
  sentence; per-segment voices are later). A silence beyond `max_turns`, a repeated barge-in
  turn or one-language code switching fail to load; a `voice:` block on a text transport, and
  duplex on a transport without the `duplex` capability (`audio-http`), are refused at launch
  (exit 3) rather than silently not played.
- **Barge-in.** Each planted phrase is spoken and mixed before the caller's first word, so no
  TTS runs while the agent is the one expected to speak. It is streamed over the agent
  `after_ms` into its speech; after it, the agent's `speech_end` is taken as its play-back
  stopping. The interruption becomes a customer turn through one new `Session` hook,
  `caller_interjections()`: the driver records it right after the agent frame it cut into and
  then waits for the agent's answer under the turn's own timeout. The cut turn is transcribed
  while the caller is still finishing the interruption. A fragment the caller cut off that no STT
  can read is `heard: cut_short`: left out of `voice.intelligible` (v3) and counted as a reply,
  never the agent's unintelligibility. A `barge_in` event (speaker `caller`) is tagged with the
  interruption's file number, and playback of a duplex call keeps the recorded times (the overlap
  is real).
- **Noise. Deviation:** generated in-house (`opseval/voice/noise.py`, generator
  `opseval-noise/1`: white, pink by Kellett's filter, and "babble", several band-limited noise
  voices switched at a syllable rate), not bundled CC0 clips: no per-clip licence, no megabytes
  in the wheel, no recorded speech. Mixed at `snr_db` against the caller's voiced frames, into
  what the agent hears only; the canary and every reference text keep the clean audio and words.
  `VoiceTimeline.noise` records generator, kind, SNR and seed.
- **The record** (layout `opseval-runs/7`; 1–6 still read, `PRE_USAGE_LAYOUTS` unchanged):
  `VoiceTimeline.mode` (`turns | duplex`), `spans` (caller turns, the agent's voiced stretches
  on the play-out clock, joined across pauses under 200 ms, and `harness` spans for every TTS,
  STT and canary call), `metrics` and `noise`; a new event kind `agent_heard` keeps the agent's
  own transcript of a caller turn (redacted). Both bridges fill them.
- **Metrics** (`opseval/voice/metrics.py`, computed from the spans when the attempt ends; the
  files-mode twin of ADR-0051's `attempt_metrics` rows: name, turn, value, unit, degraded,
  reason, with a NULL value always carrying its reason). The rules as built: a gap a harness span
  overlaps is left out and degraded (`UNPLACEABLE`); dead air is the answer gap plus pauses of at
  least 200 ms inside an agent turn, never a gap that ends in caller speech or a planted silence
  (it sits inside the caller's turn); when the agent was still talking as the caller finished,
  there is no latency and the dead air starts when it stopped; talk-over counts only agent speech
  that *began* while the caller was talking (overlap the caller started is the caller's, and how
  fast the agent stopped is `voice.barge_in_yield_ms`); time to first audio runs from the call's
  start for a greeting, else from the end of the caller's opening. On the turn-based bridge
  talk-over and yield are `NOT_MEASURABLE` with that reason.
- **Scorers** (`scoring/voice_metrics.py`, each with a `Params` model, `reads_text = False`):
  `voice.time_to_first_audio_ms` (`max_ms`), `voice.turn_latency_ms` (`p50_max_ms`,
  `p95_max_ms`, `max_ms`; nearest-rank percentiles from the per-turn rows),
  `voice.barge_in_yield_ms` (`max_ms`; a planted interruption that never happened degrades it),
  `voice.talk_over_ratio` (`max_ratio`), `voice.dead_air_ms` (`max_turn_ms`, `max_total_ms`),
  `voice.agent_asr_wer` (`max_mean`, `max_turn`; the noise is in its details),
  `voice.asr_confidence` (`min_mean`, `min_turn`) and `voice.transfer` (`expected`, `to`,
  `within_ms`, read from the timeline). **Deviation:** the ADR's example `p95_max` is
  `p95_max_ms`, so a unit is always in the name. A threshold over nothing measured is
  `infra_error` `NOT_MEASURABLE`; an older record is `NOT_MEASURABLE`.
- **`judge.pronunciation_dialect_fit`**: quoted and fenced like every judge (ADR-0036), rubric =
  the resolved language and dialect, the STT's per-turn confidence and every word under the bound
  given as harness facts; low confidence degrades it as every judge (ADR-0053); judge
  independence as usual. Not measurable on a text attempt or with nothing heard.
- **`audio-ws`**: `transcript_of_field` (default `of`) marks the agent's transcript of the caller.
- **Demo:** `opseval demo --voice --duplex` (suite `voice-duplex`: a mid-sentence pause, babble
  at 30 dB, one barge-in, every timing scorer gated) exits 0; `--bug talks-over` (the agent never
  stops for the caller) exits 1. The demo agent speaks in real time and sends its transcript of
  the caller; the demo's tone STT now reads a turn spoken in pieces (`tones.decode_all`).
- **Tests** (`tests/test_voice_duplex.py`): the three duplex rows of the fault table (an agent
  that never yields fails the yield; an STT outage in a barge-in turn is `SttError`, not judged;
  planted silences and slow TTS/STT are never latency or dead air), noise never in the canary or
  the agent's audio, the metric rules from spans, the scorers, the judge's facts, load and
  launch refusals, and the demo exit codes.

Not built yet:

- `voice.dtmf_navigation` (the caller pressing keys and the menu state reached: it needs a DTMF
  action in the scenario and a menu-state signal in the profile).
- Running every ADR-0053 fault row against both drivers; the duplex rows above are tested.
- Per-segment voices for code-switching; the `attempt_metrics` table itself (ADR-0051, server
  mode) and the voice dashboards (ADR-0066); the voice timeline in the report and console.
- Greetings: the caller's opening is spoken as soon as the call is open, as the text driver
  opens; an agent that greets may be talked over by it, which is never counted against the
  agent (the caller started that overlap).

## Review of #91 (2026-10-30)

The code review of V1 found ways a harness problem could still read as the agent's, and an
agent failure as "not measured". Fixed, each with a fault-injection test
(`tests/test_voice_duplex_review.py`):

- **Play-out never carries over.** The turn-based bridge places the agent's audio by arrival
  (it never plays it out: the caller answers once the turn is over), so a turn sent faster than
  real time no longer pushes the next turn's spans late; its own pauses are then not on the
  clock, which degrades that turn's dead air (`UNPLACEABLE`), never hides it. The duplex driver
  keeps the play-out clock and re-anchors it when the caller starts a turn.
- **A duplex turn ends when its audio has played out**, whatever ended it (the agent's
  `speech_end`, silence in its audio, an answering data message); speech read meanwhile is
  handed to the next turn in order. A planned barge-in fires `after_ms` into the speech as it
  plays, so an agent that sends three seconds of audio in 200 ms is still interrupted and its
  yield measured.
- **An interruption is answered only by what follows it.** It is released to the driver once
  everything the agent sent before it ended has been handed over, so the record shows the cut
  turn first; a data message that arrived while the caller was still interrupting is recorded
  but never the answer (new `Session.stale` hook). An agent silent after a barge-in is the
  agent's `TIMEOUT`.
- **Interruptions are not scripted turns.** Planted silences and the noise seed count the turns
  spoken through `send` only, and an interruption is recorded as a customer turn (counted in
  `user_turns`) but never against `max_turns`, which budgets the turns the scenario wrote.
- **Harness spans say whether they held the line** (`VoiceSpan.holds_line`). On the duplex
  driver the session stamps every agent event on arrival while Opseval works, so its STT, TTS
  and noise mixing run beside the agent's path and never make a gap unplaceable: eight seconds
  of silence after a barge-in are eight seconds of dead air, even while the cut fragment is
  still being transcribed. The turn-based bridge keeps the old rule (a harness span holds the
  line).
- **Noise is mixed in a worker thread**, as a `noise` harness span, so it never stalls the
  session's reader (agent audio arriving meanwhile keeps its arrival time). The generator is
  unchanged (`opseval-noise/1`), so a seed gives the same noise as before; it was not
  vectorised (the `array` module has no vector arithmetic and numpy is not a dependency).
- **Cut short only when the agent stopped**: within 1 s of the caller cutting in, after at most
  2 s of speech (`CUT_YIELD_MS`, `CUT_FRAGMENT_MS`). An agent that talked on gets the canary
  like any turn, so `voice.intelligible` can fail it. A cut-short fragment's unusable text is
  removed from what the judges read, and a trusted harness fact says "agent spoken turn N was
  cut short by the caller's interruption" for every turn the agent stopped for.
- **The interruption's failure never replaces the turn's outcome.** The turn's own result, or
  its own error (an `SttError` stays a harness error), is kept; the interruption's error is a
  timeline note. The agent hanging up during it is the agent ending the call (the next
  customer turn is unheard: `TIMEOUT`), as for any hang-up; a line that closed with no
  hang-up is the transport's failure (not judged), raised by the next read or send after the
  cut turn was recorded.
- **The agent's own transcript** (`voice.agent_asr_wer`): every final is kept with its arrival
  time and attributed to the last caller turn that had started when it arrived; the finals of
  one turn are joined, so a caller pausing mid-sentence is graded on the whole turn.
- **Code-switching reaches the judges.** With `voice.code_switching`, the language and persona
  judges (version 4) are told the listed languages and the rubric digest includes them, so
  `rescore` re-grades: switching between them is expected of the persona, and an agent
  mirroring the caller's switches within them is acceptable.
- **`judge.pronunciation_dialect_fit`** (version 2): the words the STT was unsure of are the
  agent's, so they go inside the fence as each agent turn's `unsure_words`; the harness facts
  hold only numbers (per-turn confidence, how many words, the bound). A mean or lowest
  confidence under `OPSEVAL_STT_MIN_CONFIDENCE` (now recorded in the timeline) degrades it,
  with the reason.
- **No second reader.** `DuplexBridge` reads the session directly: a `duplex` session already
  stamps every event on arrival with its own reader (`audio-ws` does), which is now part of
  what the capability promises. `close()` cancels only the interruption it owns and lets an
  outside cancellation through.
- **Rescore recomputes the metrics.** The timeline keeps their inputs (`VoiceTimeline.inputs`:
  caller texts, redacted, and times; the agent's transcripts with arrival; agent turns; barge-in
  plans and outcomes; `METRICS_VERSION`). The metrics are computed from what the record keeps,
  and a `voice.*` metric scorer (version 2) recomputes them when the stored rules' version is
  older (`details.recomputed_from`). Layout `opseval-runs/8`; 1–7 still read (a layout-7 record
  keeps its stored rows), `PRE_USAGE_LAYOUTS` unchanged.
- The decision to generate noise rather than bundle CC0 clips is the maintainers' to take:
  `docs/ATTENTION.md` D18. This ADR stays *Proposed*.
