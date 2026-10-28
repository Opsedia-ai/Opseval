# ADR-0053: Voice sessions and records

- **Status:** Proposed
- **Date:** 2026-10-28
- **Deciders:** Opseval maintainers (owner request 2026-10-28)

## Context

On 2026-10-28 the owner made voice evaluation the top priority. This reverses the PRD, which
listed voice as a v1 non-goal (ATTENTION D13 tracks updating the planning documents). The order
is MENA first, then the US and Europe, then Chinese. Every channel an agent can speak over is in
scope: an HTTP endpoint that takes and returns an audio file, a WebSocket audio stream, a phone
call (SIP, Twilio), a WebRTC room, and any other speaking agent.

Nothing in Opseval handles audio today:

- `transport.base.Session` exchanges decoded JSON frames (`send(frame)`, `receive(timeout)`).
- `models.record.FrameRecord` is text-only: `payload`, `text`, `latency_ms`, `phase`.
- `storage.LAYOUT` is `opseval-runs/2`, and a run folder holds JSON only.

Voice adds new ways for the harness to fail: a speech provider is down, a transcript is empty, a
dialect is recognised badly. Each of them could be scored as the agent failing. It also adds data
that the law treats as biometric or personal. This ADR fixes the session contract, the record and
the storage. Speech providers are ADR-0054, the full-duplex driver is ADR-0055, phone calls are
ADR-0056, WebRTC is ADR-0057, and languages are ADR-0059.

## Decision

### `AudioSession`, the contract every voice transport implements

`transport/audio.py` adds `AudioSession` beside `Session`:

- `start()` opens the call or stream; `close()` ends it.
- `send_audio(chunk)` streams caller audio (PCM16 at the session's rate); `send_dtmf(digits)`.
- `events()` yields typed events: `AgentAudio(chunk)`, `AgentSpeechStart`, `AgentSpeechEnd`,
  `AgentTranscript(text, final)` when the agent exposes its own ASR, `Dtmf`, `Transfer(to)`,
  `Hangup(by)` and `Mark(name)`.
- `transport_events()` as on `Session` (drops, resumes, token refreshes).
- **One monotonic clock per attempt.** Every event carries milliseconds since the attempt
  started, from `time.monotonic_ns()`. Wall-clock `at` is kept for display only. Latency and
  overlap are never computed from wall-clock time.

### The record

- `FrameRecord.audio`: blob ref (a path relative to the run folder), `start_ms`, `end_ms`,
  `sha256`, codec and rate.
- `FrameRecord.transcript`: provider, model, `confidence`, word timings, and whose ASR it is
  (`opseval` or `agent`). `text` stays the field scorers read.
- `RunRecord.voice`: the attempt's timeline (speech starts and ends per speaker, barge-ins,
  DTMF, transfer, hang-up, marks).
- `storage.LAYOUT` becomes `opseval-runs/3`; layouts 1 and 2 are still read. Audio goes under
  `audio/` in the run folder, one file per speaker per turn.

### `VoiceBridge(Session)`: the existing driver, unchanged

`VoiceBridge` wraps an `AudioSession` and presents it as a `Session`, so the driver, scorers and
judges run on voice with no change:

- **Out:** the simulated customer's text goes to TTS (ADR-0054), and the audio is streamed.
- **In:** agent audio is buffered until the end of its turn, which is the agent's own
  `AgentSpeechEnd` or a mark when the protocol has one, else energy-based detection with
  `audio.end_of_turn_silence_ms`. The audio then goes to STT, and the turn is returned as a
  `message` frame whose `text` is the transcript.
- **Harness time is not the agent's time** (ADR-0042). The turn clock starts when the last
  caller sample was sent, not when TTS was called. STT runs after `AgentSpeechEnd`, under its own
  `OPSEVAL_STT_TIMEOUT_SECONDS`. An overrun is an `SttError`, never a `TransportTimeout`.
- PCM16, μ-law and A-law conversion and resampling are written in-house. `audioop` was removed
  in Python 3.13 and the core must run on 3.11 to 3.13.

The bridge is turn-based, so it cannot measure barge-in or talk-over. That is ADR-0055's job.

### The one rule, for voice

| What happened | Result | Why |
|---|---|---|
| No agent speech within the turn timeout | `TransportTimeout` | The agent did not answer. Its fault. |
| The agent speaks past `audio.max_turn_seconds` | `TransportTimeout` | The turn never ended. Its fault. |
| TTS or STT unreachable, 5xx, or over its own timeout | `TtsError` / `SttError` (`HarnessError`) | Opseval could not speak or listen. Not judged. |
| Empty transcript for agent audio that has energy | `infra_error` `ASR_NO_TRANSCRIPT` on every text-reading scorer | The agent said something we could not hear. Not measured, and named. |
| Transcript confidence under `OPSEVAL_STT_MIN_CONFIDENCE` | judge scores `degraded`, named in every output (ADR-0017) | The evidence is weak; the score is not hidden. |
| Agent audio cannot be decoded | synthetic `error` frame; bytes kept under `audio/` | The agent sent broken audio. Its fault, with proof. |
| Agent audio with no energy (silence) | no speech; the turn times out | Silence is not an empty transcript. |

`protocol.replies_read` counts a turn with audio as read, so an untranscribed turn is reported
once, as `ASR_NO_TRANSCRIPT`, and not again as an unread reply. Deterministic checks that need no
text (tools, flow, terminal state) still score that attempt.

### Privacy and residency

Agent and caller audio is personal data, and a voice can be biometric data, under GDPR, the Saudi
PDPL, the UAE PDPL and China's PIPL.

- **Per-speaker channels.** Caller and agent audio are separate files, so either can be deleted
  or withheld alone.
- **Encrypted at rest** with `OPSEVAL_ENCRYPTION_KEY` (a `SecretStr` in `SECRET_FIELDS`). Without
  a key, a run that records audio is refused at launch (exit 3), unless it keeps hashes only
  (`OPSEVAL_RECORD_AUDIO=hashes`). The demo's synthetic voices are exempt.
- **Separate retention.** `OPSEVAL_AUDIO_RETENTION_DAYS` (default 30) prunes `audio/` before the
  record (`retention.py`, ADR-0037). A pruned blob reads as "audio pruned", never as missing
  evidence; text scorers still rescore from the transcript.
- **Region.** A task's region (ADR-0049) picks its blob store (ADR-0051) and the speech
  providers it may use (ADR-0054). A provider outside the region's list is refused at launch.
- **Credentials are redacted in transcripts.** A transcript becomes `FrameRecord.text`, so
  `redaction.redact_text` (ADR-0033) runs on it before the record is written, and on word
  timings too. Audio itself is not redacted; that is stated in the record.

## Consequences

- Every text scorer and judge works on voice from the first voice PR.
- Verification: `opseval demo --voice` runs a speaking demo agent over `audio-ws` and `audio-http`
  and exits 0; `--bug silent` exits 1; `--stt-down` exits 3. CI uses loopback fake TTS and STT
  servers; real providers run nightly. Fault-injection tests cover every row of the table.
- A voice extra (`opseval[voice]`) holds the encryption library, which must pass the `make audit`
  licence allowlist. The core install gains no dependency.
- A layout bump means a new reader for layout 3, with the old ones kept.

## Not decided here

- The default energy threshold and end-of-turn silence per language. They are calibrated per
  language pack (ADR-0059).
- Consent records per recording (`audio_assets.consent_basis`, ADR-0051).
