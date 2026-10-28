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
- `capabilities` (a class-level `frozenset`) names what a transport can do beyond speaking and
  listening; today only `dtmf`. A transport without it that is asked to press keys raises
  `UnsupportedCapability`, a `ConfigError` (exit 3): never dropped, never the agent's fault.
  Because the set is on the class, the catalogue loader can later refuse, at load time, a
  scenario that presses keys on a profile whose transport lacks `dtmf`.
- `end_of_caller_turn()` says the caller has finished speaking. A request/response transport
  sends the turn there, and the agent's turn clock starts there (ADR-0042).
- `next_event(timeout)` returns the next typed event, raising `TransportTimeout` or
  `TransportClosed` as `Session.receive` does: `AgentAudio(chunk)`, `AgentSpeechStart`, `AgentSpeechEnd`,
  `AgentTranscript(text, final)` when the agent exposes its own ASR, `Dtmf`, `Transfer(to)`,
  `Hangup(by)` and `Mark(name)`.
- `transport_events()` as on `Session` (drops, resumes, token refreshes).
- **One monotonic clock per attempt.** Every event carries milliseconds since the attempt
  started, from `time.monotonic_ns()`. Wall-clock `at` is kept for display only. Latency and
  overlap are never computed from wall-clock time.

### The record

- `FrameRecord.audio` (`AudioRef`): blob ref (a path relative to the run folder, under
  `audio/`), `start_ms`, `end_ms`, `sha256`, codec, rate and length. It is checked when loaded:
  `0 <= start_ms <= end_ms`; the rate is one of `audio.RATES` (repeated as
  `models.record.AUDIO_RATES`, since models import nothing else, and a test keeps them equal).
  Three states: a file (`path` set), hashes only (no path, not `pruned`) and pruned (no path,
  `pruned`); a pruned ref with a path is refused.
- `AudioRef.encryption` (`none` or `aes-256-gcm`) and `key_id` (a fingerprint of
  `OPSEVAL_AUDIO_ENCRYPTION_KEY`, never the key) are in the record from V0a, so encrypted files
  (V0d) need no further layout bump. `key_id` is set exactly when `encryption` is not `none`,
  and hashes-only audio cannot claim encryption (there is no file).
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
  in Python 3.13 and the core must run on 3.11 to 3.13. G.711 is checked bit for bit against
  `audioop`'s output. Resampling streams through `audio.Resampler` (`feed`/`flush`), which keeps
  the exact fractional position and the last sample across chunks, so chunked output is the same
  bytes as one-shot output: no drift, no seam at a chunk boundary. The codec lookup tables are
  built by `audio.warm(codec)`, which `AudioSession.__init__` calls, so building them is never on
  the agent's clock. WAV input at a rate outside `audio.RATES`, or with data that is not a whole
  number of frames, is refused (`AudioDecodeError`), not resampled: the transport decides the
  rate.

The bridge is turn-based, so it cannot measure barge-in or talk-over. That is ADR-0055's job.

### The one rule, for voice

| What happened | Result | Why |
|---|---|---|
| No agent speech within the turn timeout | `TransportTimeout` | The agent did not answer. Its fault. |
| The agent speaks past `audio.max_turn_seconds` | `TransportTimeout` | The turn never ended. Its fault. |
| TTS or STT unreachable, 5xx, or over its own timeout | `TtsError` / `SttError` (`HarnessError`) | Opseval could not speak or listen. Not judged. |
| Agent audio with energy, empty or garbage transcript, STT canary healthy | `voice.intelligible` fails (output axis) | STT is proven to work; the agent's audio is what failed. Its fault, with proof. |
| The same, and a second STT provider transcribes it | the second transcript is used, scores `degraded` | The agent was heard; the first STT missed it. Named. |
| The same, and the STT canary fails | `infra_error` `ASR_NO_TRANSCRIPT` on every text-reading scorer | Opseval's own listening is broken. Not measured, and named. |
| Transcript confidence under `OPSEVAL_STT_MIN_CONFIDENCE` | judge scores `degraded`, named in every output (ADR-0017) | The evidence is weak; the score is not hidden. |
| Agent audio cannot be decoded | synthetic `error` frame; bytes kept, encrypted, under `audio/` (hashes only: hash, length, codec, duration) | The agent sent broken audio. Its fault, with proof. |
| Agent audio with no energy (silence) | no speech; the turn times out | Silence is not an empty transcript. |

`protocol.replies_read` counts a turn with audio as read, so an untranscribed turn is reported
once (as a failed `voice.intelligible` or as `ASR_NO_TRANSCRIPT`), and not again as an unread
reply. Deterministic checks that need no text (tools, flow, terminal state) still score that
attempt.

### Empty transcripts: decided by evidence, not by default

An empty transcript for agent audio with energy could be the agent's fault (garbled, clipped or
wrong-codec speech) or Opseval's (a broken STT). Defaulting to either side breaks the one rule, so
Opseval measures which it is:

- **An STT health canary.** Opseval knows exactly what the caller said, so its own caller audio is
  a known-good clip. Preflight transcribes one phrase (ADR-0054). Within an attempt, whenever an
  agent turn comes back empty or garbage, the caller turn just before it is transcribed too, on
  the same provider and model and through the same codec path (μ-law 8 kHz on a phone line). The
  canary is healthy when its WER is at most `OPSEVAL_STT_CANARY_MAX_WER`.
- **A second STT check** (optional, recommended): a different provider or model
  (`OPSEVAL_STT_SECOND_*`, ADR-0054), under the same allowlist and region rules. It transcribes
  the same agent audio.
- **The decision**, in order:
  1. the second STT returns a usable transcript: it is used, and the attempt's text scores are
     `degraded`, naming both providers;
  2. otherwise, the canary is healthy: the agent's audio is the problem, and `voice.intelligible`
     fails, with the energy, duration and every transcript as evidence;
  3. otherwise (the canary's WER is over the bound, or the canary call itself failed): every
     text-reading scorer is `infra_error` `ASR_NO_TRANSCRIPT`, and the canary result is named.
- "Garbage" means no letter in any script and no digit (amended in review, see "Review of
  #84" below). A low-confidence transcript with words is the confidence row of the table, not
  this one.
- `voice.intelligible` is always on for voice attempts and sits on the `output` axis. Every
  canary call and its WER go into the record, so `rescore` sees the same evidence.
- A canary that is unhealthy while transcripts are not empty marks the attempt's judge scores
  `degraded`, named in every output (ADR-0017).

### Privacy and residency

Agent and caller audio is personal data, and a voice can be biometric data, under GDPR, the Saudi
PDPL, the UAE PDPL and China's PIPL.

- **Per-speaker channels.** Caller and agent audio are separate files, so either can be deleted
  or withheld alone.
- **Always encrypted at rest when stored,** agent audio without exception, with
  `OPSEVAL_AUDIO_ENCRYPTION_KEY` (a `SecretStr` in `SECRET_FIELDS`). It is a separate key from
  `OPSEVAL_ENCRYPTION_KEY`, which encrypts agent credentials (ADR-0047 step 5). The same rule
  holds for the blob store ([ADR-0051](0051-runs-and-blobs-in-the-database.md)). Without the key,
  a run that records audio is refused at launch (exit 3), unless it keeps hashes only
  (`OPSEVAL_RECORD_AUDIO=hashes`). The demo's synthetic voices are exempt.
- **Hashes only means no bytes.** In that mode each turn keeps its SHA-256, length, codec, rate
  and duration. Audio that could not be decoded keeps the same: a hash and the metadata, never
  the bytes.
- **Separate retention.** `OPSEVAL_AUDIO_RETENTION_DAYS` (default 30) prunes `audio/` before the
  record (`retention.py`, ADR-0037). A pruned blob reads as "audio pruned", never as missing
  evidence; text scorers still rescore from the transcript.
- **Region.** A task's region (ADR-0049) picks its blob store (ADR-0051) and the speech
  providers it may use (ADR-0054). A provider outside the region's list is refused at launch.
- **Credentials are redacted in transcripts.** A transcript becomes `FrameRecord.text`, so
  `redaction.redact_text` (ADR-0033) runs on it before the record is written, and on word
  timings too. The driver passes every frame through `redaction.redact_frame` (payload strings,
  `text`, `transcript.words`), so a field added later has one place to be covered. A credential
  can be spoken as one word or split over several, so the detectors run on each word, on the
  words joined by spaces, on runs of words joined with nothing, and on the turn's text (words
  are matched to what was found there). A word inside a credential becomes
  `[redacted:<kind>]`, its timing kept; the kinds are counted in `RunRecord.redaction` like any
  other. It errs towards removing: a word run straight on to a credential can be taken as its
  last piece. Audio itself is not redacted; that is stated in the record.

## Consequences

- Every text scorer and judge works on voice from the first voice PR.
- Verification: `opseval demo --voice` runs a speaking demo agent over `audio-ws` and `audio-http`
  and exits 0; `--bug silent` and `--bug garbled` (speech no STT can read, canary healthy) exit 1;
  `--stt-down` and `--stt-deaf` (STT answers, but empty for the canary too) exit 3. CI uses
  loopback fake TTS and STT servers; real providers run nightly. Fault-injection tests cover
  every row of the table.
- A voice extra (`opseval[voice]`) holds the encryption library, which must pass the `make audit`
  licence allowlist. The core install gains no dependency.
- A layout bump means a new reader for layout 3, with the old ones kept.

## Not decided here

- The default energy threshold, end-of-turn silence and `OPSEVAL_STT_CANARY_MAX_WER` per
  language. They are calibrated per language pack (ADR-0059).
- Consent records per recording (`audio_assets.consent_basis`, ADR-0051).

## Implementation (V0c part 1, 2026-10-28)

Built:

- **The bridge** (`opseval/voice/bridge.py`). `VoiceBridge(Session)` wraps an `AudioSession`;
  `VoiceTransport` opens and starts one per attempt. The driver gained four optional `Session`
  hooks and nothing else changed in it: `whole_turns` (a spoken turn is one frame, so an empty
  transcript still answers the turn instead of becoming a turn timeout; now `ends_turn`, see
  the review below), `use_harness_clock`
  (TTS and STT run with the attempt deadline paused, like the simulator), `frame_media` (the
  `AudioRef`, the `TranscriptInfo` and the latency on the attempt's monotonic clock, from the
  caller's last sample to the agent's first sample with energy, so STT time never inflates it)
  and `voice_timeline` (`RunRecord.voice`).
- **What is spoken.** The bridge finds the `"{text}"` value in the profile's
  `user_message_frame`. A checkpoint decision sent through `resume_frame` is spoken as its
  feedback, or as a plain yes or no in the scenario's language. A frame with nothing to say is a
  `ConfigError`. A voice profile cannot declare probes (they are JSON frames).
- **End of turn.** The agent's own `speech_end` event, a data message after its speech, or
  `audio.end_of_turn_silence_ms` of silence (silent chunks or no events) after a chunk with
  energy (`audio.silence_dbfs`). Silence alone never starts a turn, and is never sent to STT.
  Data messages (`AgentFrame`: a checkpoint, a result) and DTMF, transfer and hang-up go to the
  driver as frames, in order around the spoken turn, and to the timeline. A customer turn after
  the agent hung up is the agent's `TransportTimeout`.
- **The decision** follows the table above. Its evidence goes into the reply's payload under
  `_opseval_voice` (level, duration, every transcript tried, `heard: stt | second_stt |
  unintelligible | asr_failed`, the canary's provider, model, codec, rate, what was said and
  heard, WER and bound, or its error, and `low_confidence`), so `rescore` decides on exactly
  what the run saw. "Usable" means at least one letter in the language's script (Latin by
  default, Arabic for `ar`, CJK for `zh`/`ja`; superseded in review: any letter or digit). A second STT that fails is noted and the canary
  runs; a canary call that fails is unhealthy.
- **Scoring.** `voice.intelligible` (output axis) is added to the always-on checks for any
  record with a voice timeline. `Scorer.reads_text` (default `True`, so an unknown plugin errs
  towards "not measured"; `False` on every built-in that reads no words) decides which scores
  become `infra_error` `ASR_NO_TRANSCRIPT`, naming the canary, without being run. A turn heard
  only by the second STT marks every text-reading score degraded; a confidence under
  `OPSEVAL_STT_MIN_CONFIDENCE` marks every judge score degraded; both with
  `details.degraded_by`.
- **Transports.** `audio-ws` (`transport/audio_ws.py`): base64 JSON envelope or binary frames,
  event names from `audio.envelope`, an optional `start_frame`, `auth_frame` as for websocket,
  DTMF declared. `audio-http` (`transport/audio_http.py`): one `POST` per caller turn (WAV or
  raw, one session id per attempt in a header), sent at `end_of_caller_turn` and awaited in
  `next_event`; WAV, raw or JSON answers; a WAV at another rate is resampled; 5xx is the agent's
  error frame, 4xx and `NOT_SENT_TIMEOUTS` are transport errors. Both are allowlisted with TLS
  off loopback (`wss`, `https`).
- **Storage and privacy** (`voice/recording.py`). One PCM16 WAV per speaker per turn at
  `audio/<test>/attempt<N>/<n>-<speaker>.wav`, or the hash and metadata alone with
  `OPSEVAL_RECORD_AUDIO=hashes`. Undecodable agent audio is kept as it came (`.raw`), or as its
  hash. `check_audio_privacy` refuses at launch (exit 3) a run that would write files, except
  the demo's synthetic voices. `cryptography` is not in `uv.lock`, so encryption is not built:
  setting `OPSEVAL_AUDIO_ENCRYPTION_KEY` is refused too, rather than claiming an encryption
  that did not happen. `storage.LAYOUT` is `opseval-runs/5` (the profile snapshot may carry
  `audio:`, replies the bridge's evidence); 1–4 are still read.
- **Launch.** A voice transport needs the profile's `audio:` block and a configured TTS and
  STT (`runner.speech_kit`, exit 3 otherwise). Preflight runs the speech checks for every
  scenario. **Deviation from ADR-0054:** a blocking TTS or STT check refuses the run (exit 3)
  instead of skipping the voice scenarios, because every scenario of a voice run speaks, so the
  skip would leave nothing measured; per-scenario skips return when a suite can mix text and
  voice. `opseval preflight --transport audio-ws` runs the same checks.
- **The demo.** `opseval demo --voice` runs a speaking refund agent (English and Egyptian
  Arabic) over `audio-ws` (or `--voice-transport audio-http`) at μ-law 8 kHz, with fake
  OpenAI-compatible TTS and STT on loopback that encode text reversibly as tones
  (`demo/tones.py`: sync byte, length, UTF-8, checksum; 16 tones between 600 and 3300 Hz, 20 ms
  a nibble; noise and a bad checksum read as nothing). Exit codes, in CI's `check` and
  `container` jobs: `--voice` 0, `--bug silent` 1, `--bug garbled` 1, `--stt-down` 3,
  `--stt-deaf` 3 (refused at preflight: the canary is heard as nothing).

### Review of #84 (2026-10-28)

- **Usable is script-free.** A transcript is usable when it has a letter in any Unicode script
  (Latin, Arabic, CJK, kana, Hangul, Cyrillic, Hebrew, Devanagari, Thai, Greek, …) or a digit.
  A per-language script list decided the agent's fault from a gap in Opseval's own table: a
  Korean agent heard perfectly was "unintelligible". The expected script (`SCRIPTS`) is only an
  annotation: `off_script` in the evidence degrades the text scores, never fails the agent.
- **What ends the agent's turn.** `whole_turns` is replaced by `Session.ends_turn(payload)`:
  the bridge's spoken turn (even with an empty transcript) and a hang-up end the driver's wait;
  any other frame ends it only if `SutProfile.answers` says so (a checkpoint, a result, an
  error, words). A notification or data frame before speech (`response.created`) is recorded
  and the wait goes on, under the turn's own timeout: an agent that never speaks after it is
  the agent's `TIMEOUT`. After speech, only an answering frame ends the speech early.
- **A hang-up ends the call.** None of `TerminalState` names "the agent ended the call", and a
  new value would move every reader for one case, so it ends the way a text agent that stops
  answering does. After a hang-up the bridge never reads the line again: the connection
  closing is not a `TRANSPORT_FAILURE`. The driver's quiet window ends; if the customer
  considers the goal reached (or the script is done) the attempt is `GOAL_REACHED`, and if it
  still had something to say, the next turn is the agent's `TIMEOUT` ("the agent hung up").
  Either way it is judged, never "not measured".
- **`max_turn_seconds` by the audio's length too.** The bridge sums the agent audio of a turn
  at the session's rate; that or the wall clock, whichever trips first, raises
  `TurnNeverEnded` (a `TransportTimeout`: the agent's fault) before STT, so the STT never gets
  more than `max_turn_seconds` of audio (an `audio-http` answer of ten minutes arrives at once
  and never trips the clock). Silence before speech that long is dropped, not sent. The driver
  ends the attempt on it even in its quiet window, where an ordinary timeout means "the turn is
  over".
- **One unheard turn voids only itself.** `ASR_NO_TRANSCRIPT` is per turn. With some agent
  turns heard and some `asr_failed`, the text-reading scorers (`Scorer.reads_text`) grade a
  copy of the record whose unheard turns have no text (`scoring.voice.heard_only`), and their
  scores are degraded naming the unheard turns and the canary; a judge gets a harness fact
  saying the agent spoke turns Opseval could not hear, so the gap is not read as silence;
  `conversation.agent_replied` counts an unheard spoken turn as an answer. A failure in a heard
  turn (a credential leaked in turn 2 with turn 5 unheard) still fails. Only when no agent turn
  was heard are the text-reading scores `infra_error` `ASR_NO_TRANSCRIPT`.
  `voice.intelligible` (v2) grades the measured turns, degraded when some were not.
- **Latency on the arrival clock.** `audio-ws` reads the socket in a task of its own and stamps
  each message when it arrives; `audio-http` stamps the answer when the response came. The
  caller's end is taken when its last sample was sent, and cleared at the start of each caller
  turn. Agent speech that began before the caller finished is a barge-in: a `barge_in` timeline
  event and a latency of `None` (not measured), never `0.0`. A voice frame's latency is always
  the session's own, so the driver's clock (which would count STT) is never used for one.
- **Also:** `audio-http` keeps the rest of a JSON answer as a data frame after the speech (so
  a checkpoint or a committed outcome is seen and the commit guard holds); a checkpoint
  decision with feedback is spoken "Yes. <feedback>" / "No. <feedback>" and every spoken
  decision becomes the sent frame's `text` (`FrameMedia.text`); undecodable audio fails
  `voice.intelligible` with its reason and size; a voice profile without an exact `"{text}"` in
  `user_message_frame` or `"{action}"`/`"{feedback}"` in `resume_frame` fails to load; the
  runner closes the speech kit whatever happens; one driver helper pauses the attempt
  deadline for the simulator and for TTS/STT.

Left for the next steps: audio encryption at rest (the `opseval[voice]` extra), audio
retention (`OPSEVAL_AUDIO_RETENTION_DAYS`), the region check, tuning the second STT and the
per-language thresholds (ADR-0059), the full-duplex driver (barge-in), phone and WebRTC, and
showing audio and the voice timeline in the report and the console.

## Implementation (V0d, 2026-10-28): audio at rest and listening back

The owner settled ATTENTION D17 on 2026-10-28: `cryptography` (Apache-2.0 or BSD-3-Clause; with
`cffi`, MIT-0, and `pycparser`, BSD-3-Clause) in the optional `opseval[voice]` extra, which
`make audit` now exports with `server`. The core install gains nothing: the library is
imported only when a file is sealed or opened (a test checks `opseval.cli` and
`opseval.server` import without it), and a run that needs it without the extra is refused
(exit 3) naming `pip install 'opseval[voice]'`. The owner's requirement: a failed call can be
listened back to.

- **The file** (`voice/crypto.py`): `magic "OPSAUDIO" | version 1 | key id (8 bytes) | nonce
  (12) | AES-256-GCM ciphertext and tag`, one file per speaker per turn
  (`<turn>-<speaker>.wav.enc`, undecodable agent audio `.raw.enc`). A fresh random 96-bit nonce
  per file. The additional data is the header and the file's place as canonical JSON: run id,
  test id, attempt, the turn number in its name (the file is written before its frame's `seq`
  exists) and the speaker. A file moved to any other place, or changed by one byte, does not
  open (`AUDIO_TAMPERED`); and the record's SHA-256 of the plaintext is checked after
  decryption, which also covers the demo's plain files.
- **The key**: `OPSEVAL_AUDIO_ENCRYPTION_KEY`, 32 bytes as 64 hex or base64 (standard or
  URL-safe); anything else, or fewer than 12 distinct bytes (a placeholder), is a `ConfigError`.
  It is checked at launch even with `OPSEVAL_RECORD_AUDIO=hashes`. `key_id` = the first 16 hex
  of HMAC-SHA256(key, "opseval-audio-key-id"): a fingerprint that cannot be tested against a
  guessed key without the key. `opseval audio keygen` prints a new key.
- **Launch** (`check_audio_privacy` → `AudioPlan`): files with a key are sealed; files without
  a key are refused except for the demo's synthetic voices (plain WAV); hashes keep no file.
  The demo keeps a key set in the environment, so `opseval demo --voice` with a key writes
  sealed files and exercises the whole path. The V0c refusal of a set key is gone.
- **One reader** (`voice/playback.py`) for the CLI and the console. A turn is addressed by
  its frame's `seq`; its state is decided from the record and the run folder without the key:
  `available`, `pruned`, `hashes_only`, `undecodable`, `missing`, and the server adds
  `key_missing` / `key_mismatch` from the record's `key_id`. Decrypted audio stays in memory.
- **The whole call** is laid on the attempt's clock: each turn at `round(at_ms * rate / 1000)`,
  silence between, stereo (caller left, agent right) or mono (summed, clipped to 16 bits).
  `at_ms` is the recorded start, except that a turn the record shows starting before the
  previous turn ended, with no `barge_in` event for it, is moved to that end: audio sent faster
  than it plays (`audio-http`, the demo) would otherwise sound like a conversation that never
  happened. A recorded barge-in stays an overlap. Any turn missing for a reason other than
  undecodable agent audio refuses the whole call (fail closed); undecodable turns are silence,
  named. A call over 4 hours by the record's times, or over 256 MB, is refused rather than
  allocated (see the review below).
- **CLI** (`voice/cli.py`): `opseval audio list|export|keygen`. `export` writes a turn
  (`--turn SEQ`, `--raw` for undecodable bytes), the whole call (stereo; `--mono`;
  `--speaker caller|agent` keeps one side, timed as in the call), refuses to overwrite without
  `--force`, and exits 3 with the code and reason (`AUDIO_KEY_MISMATCH` names both key ids,
  "audio pruned on <date>", "only hashes were kept").
- **Console API** (`server/audio.py`, registered by `create_app`, behind the same host check
  and token): `GET …/attempts/{test}/{n}/audio` (the descriptor), `…/audio/call?mono=` and
  `…/audio/{seq}` (`audio/wav`, `Cache-Control: no-store`, `Content-Disposition: inline`).
  Codes: `AUDIO_NOT_FOUND`/`AUDIO_HASHES_ONLY`/`AUDIO_MISSING` 404, `AUDIO_PRUNED` 410,
  `AUDIO_KEY_MISSING`/`AUDIO_KEY_MISMATCH`/`AUDIO_UNDECODABLE`/`AUDIO_KEY_INVALID`/
  `AUDIO_EXTRA_MISSING` 409, `AUDIO_TOO_LARGE` 413, `AUDIO_TAMPERED` 422. A malformed server key does not stop `opseval serve`; it is said on
  every audio request. The attempt detail carries `audio` (turns, `callState`, `callNote`,
  `skipped`) and each spoken message entry its `audio` descriptor. A spoken turn with no
  transcript is now a message entry with `text: null` (in the report too: "spoken; nothing was
  heard in it"), because it is the turn someone needs to hear.
- **Console**: a ▶ per spoken turn and "Play whole call" / "Download WAV"
  (`components/AudioPlayer.tsx`). The bytes are fetched with the token (an `<audio src>`
  cannot send it) into a blob URL that is revoked when replaced or unmounted; the CSP gains
  exactly `media-src 'self' blob:`. Unplayable states are words, never a broken player.
- **Report**: no audio embedded; per voice attempt, "Audio: N turn(s), encrypted — listen in
  the console or `opseval audio export <run folder> --test … --attempt … --out ….wav`".
- **Retention**: `OPSEVAL_AUDIO_RETENTION_DAYS` (30) in `opseval prune` (ADR-0037 amendment):
  the files go, the record stays, and `audio/PRUNED.json` says when.

Review of #87 (2026-10-28):

- **Undecodable agent audio** is its own transcript entry (`undecodable_audio`, the agent's,
  a failure), never a message: only a `message` frame with audio and no text is "spoken;
  nothing was heard". The entry carries the turn's `audio` descriptor; its bytes, when kept,
  download as they came (`…/audio/{seq}?raw=true`, `application/octet-stream`, attachment;
  the report names `opseval audio export … --turn N --raw --out ….raw`).
- **Limits before decrypting**: a turn's file larger than the record says it was written
  (its WAV, plus header and tag when sealed) is `AUDIO_TAMPERED` without being read. A whole
  call is sized from the record alone (the turns' bytes, then the scheduled end × rate ×
  channels × 2) and refused over `MAX_CALL_BYTES` (256 MB) or 4 hours as `AUDIO_TOO_LARGE`
  (413; the descriptor's `callState: too_large`; its turns still play one by one), and checked
  again from the decoded audio before the one buffer is allocated. The WAV is built in place:
  header, then each turn written into the interleaved samples, each decoded turn let go once
  placed.
- **No key, three ways**: unset `key_missing` (`AUDIO_KEY_MISSING`), malformed `key_invalid`
  (`AUDIO_KEY_INVALID`), set without the voice extra `extra_missing` (`AUDIO_EXTRA_MISSING`),
  all 409, in the descriptor and on the play routes alike. Plain (demo) files play in all three.
- **Barge-in**: the bridge tags the `barge_in` event's `detail` with the file number of the
  turn it opened (`… (turn 3)`, written when the turn is kept; no new field, no layout bump),
  and playback keeps that turn's overlap. An untagged event (an older record, or a turn cut
  short) belongs to the last agent turn that started at or before it with no caller turn
  starting in between: containment in `[start, end]` missed barge-ins whose sparse (VAD-gated)
  chunks end before the speech-start stamp.
- **Export**: `--out` is created `0600` with `O_CREAT|O_EXCL|O_NOFOLLOW` (`--force`: `O_TRUNC`,
  still `O_NOFOLLOW`, mode reset to `0600`); a symbolic link at `--out`, dangling included, is
  refused, with or without `--force`.
- **Prune race and one marker parser**: a file gone between listing and reading re-checks the
  marker (`AUDIO_PRUNED`, 410). `retention.read_pruned_marker` is the one parser the player,
  the CLI and the report share; an unreadable marker reads as "audio pruned (marker
  unreadable)" in all of them.
- **Keys**: unpadded base64 (`secrets.token_urlsafe(32)`) is accepted; still exactly 32 bytes.
- The attempt route reads the record once and hands it to the audio descriptor, which lists
  the turns once (`server/lookup.py` holds the id check every route shares); the console's
  audio fetch shares `call()`'s token and error path (`callBlob`), the whole call already loaded
  is what "Download WAV" saves, and a download's blob URL is revoked after 60 s or on unmount.

Still open: the region check, the full-duplex driver (barge-in), phone and WebRTC, the voice
timeline in the report and console, key rotation tooling (re-sealing old runs under a new
key), and the blob store of server mode (ADR-0051), which must seal the same way.
