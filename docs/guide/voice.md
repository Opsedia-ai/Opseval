# Voice agents (preview)

Opseval can test an agent that speaks. The simulated customer's words are turned into speech
(TTS), streamed to the agent over its own protocol, and the agent's spoken answer is turned back
into text (STT). The driver, the scorers and the judges then work as they do for text
([ADR-0053](../adr/0053-voice-sessions-and-records.md)).

!!! note "Preview"
    This is the turn-based bridge. Barge-in and talk-over (ADR-0055), phone calls (ADR-0056),
    WebRTC (ADR-0057) are not built yet.

## Try it

```bash
opseval demo --voice                         # a speaking refund agent, healthy → exit 0
opseval demo --voice --bug silent            # the agent never speaks → exit 1
opseval demo --voice --bug garbled           # it speaks, but nobody could understand it → exit 1
opseval demo --voice --stt-down              # Opseval cannot listen → exit 3, nothing judged
opseval demo --voice --stt-deaf              # Opseval's STT hears nothing, not even itself → exit 3
opseval demo --voice --voice-transport audio-http
```

The demo needs no speech model and no key. Its "voices" are synthetic: a fake TTS writes text
as a sequence of tones and a fake STT reads them back, both served on `127.0.0.1` with the same
OpenAI-compatible API a real provider has. The calls go through μ-law at 8 kHz, as on a phone
line, so resampling and the codec are tested on every run. One scenario is in English and one in
Egyptian Arabic.

## Your own agent

Add an `audio:` block to the profile and pick a voice transport:

```yaml
name: my-voice-agent
audio:
  codec: mulaw              # pcm16 | mulaw | alaw
  rate: 8000                # 8000, 16000, 22050, 24000, 44100 or 48000
  end_of_turn_silence_ms: 800
  max_turn_seconds: 60
  envelope:                 # audio-ws only: how the stream is framed
    audio_event: audio      # {"type": "audio", "audio": "<base64>"}
    speech_end_event: speech_end
    start_frame: {type: start, codec: "{codec}", rate: "{rate}"}
user_message_frame: {type: user_message, text: "{text}"}
```

The bridge speaks the customer's words from the value that is exactly `"{text}"` in
`user_message_frame`, and a checkpoint decision from the `"{action}"` or `"{feedback}"` value in
`resume_frame` (the default has both). A voice profile without them fails to load, so
`opseval validate` catches it. A decision is said as a caller would say it, in the scenario's
language: "Yes, please go ahead.", "No.", "Yes. <feedback>" or "No. <feedback>", and those
words are what the record and the judges see.

```bash
export OPSEVAL_ALLOWED_HOSTS=agent.example.com,api.openai.com
export OPSEVAL_TTS_URL=https://api.openai.com OPSEVAL_TTS_MODEL=gpt-4o-mini-tts
export OPSEVAL_TTS_VOICE_MAP='{"default": "alloy"}'
export OPSEVAL_STT_URL=https://api.openai.com OPSEVAL_STT_MODEL=whisper-1
export OPSEVAL_TTS_KEY=… OPSEVAL_STT_KEY=…
export OPSEVAL_AUDIO_ENCRYPTION_KEY=…       # see "Audio and privacy" below (or RECORD_AUDIO=hashes)

opseval run suites/voice.yaml --profile profile.yaml --transport audio-ws --target wss://agent.example.com/voice
```

- `audio-ws` streams audio over a WebSocket, as base64 in JSON or as binary frames
  (`envelope.binary: true`). The agent's other JSON messages, such as a checkpoint or a result,
  are frames that the profile classifies as usual.
- `audio-http` sends each caller turn as one `POST` (a WAV file, or raw bytes with
  `http.request_format: raw`) and reads the answer as audio, or as JSON
  `{"audio": "<base64>", "transcript": "…"}`. Any other field of that JSON (a checkpoint, a
  result, an outcome, a phase) is a data frame after the speech, classified as usual, so
  checkpoints and the commit guard work as on a text agent.

The agent's turn ends with its speech (its own end-of-speech event, or
`end_of_turn_silence_ms` of silence), a checkpoint, a result, an error or a hang-up. A
notification such as `response.created` is recorded but does not end the wait: an agent that
sends one and never speaks has timed out.

## How a spoken turn is judged

| What happened | Result |
|---|---|
| The agent says nothing, or only silence, in time | the agent's timeout (exit 1) |
| It speaks past `audio.max_turn_seconds` (by the clock, or by the length of the audio it sent) | the agent's timeout |
| It hangs up while the customer still had something to say | the agent's timeout (a hang-up is never "not judged") |
| TTS or STT down, failing or over its own time limit | not judged (exit 3) |
| The agent's audio cannot be decoded | an error frame, and `voice.intelligible` fails: the agent's fault |
| It spoke, and the STT heard nothing usable | Opseval checks its own hearing first (below) |
| The transcript's confidence is under `OPSEVAL_STT_MIN_CONFIDENCE` | judge scores are marked degraded |

If the agent spoke but the transcript has no letter (in any script) and no digit, it could be
the agent's fault (garbled speech) or Opseval's (a broken STT). Opseval checks which. (Words in
another script than the scenario language's are heard; the text scores are only marked
degraded.)

1. A second STT (`OPSEVAL_STT_SECOND_*`), if configured, hears the same audio. If it can read
   it, its transcript is used and the text scores are marked degraded.
2. Otherwise the **canary** runs: the customer's own turn just before, which Opseval knows word
   for word, goes through the same STT and codec. If the STT gets it right (word error rate at
   most `OPSEVAL_STT_CANARY_MAX_WER`), Opseval can hear, so the agent's audio is the problem:
   `voice.intelligible` fails, with the level, length and every transcript as evidence.
3. If the canary fails too, Opseval's listening is broken for that turn. The checks that read
   the agent's words grade the turns that were heard and are marked degraded, naming the
   unheard ones; a judge is told which turns are missing. A failure in a heard turn (a leaked
   credential, say) still fails. Only when no agent turn was heard at all are those checks
   *not measured* (`ASR_NO_TRANSCRIPT`), with the reason naming the canary.

Latency is measured from the caller's last sample to the agent's first sample with energy,
when it arrived. Speech that began before the caller finished is a barge-in: that turn's
latency is not measured, never 0.

Preflight runs the same canary before the first call. A voice run whose TTS or STT is down, or
cannot hear the canary, is refused with exit 3: every scenario of a voice run speaks, so nothing
could be measured.

## Audio and privacy

A voice can be biometric data, so recorded audio is only ever stored encrypted at rest
(ADR-0053). A voice run against a real agent keeps its audio only with a key; without one it is
refused at launch (exit 3), unless it keeps hashes only:

- `OPSEVAL_RECORD_AUDIO=files` (the default) writes one file per speaker per turn under the run
  folder's `audio/` (`audio/<test>/attempt<N>/<turn>-<speaker>.wav.enc`), each sealed on its own
  with AES-256-GCM under `OPSEVAL_AUDIO_ENCRYPTION_KEY`. This needs the voice extra:
  `pip install 'opseval[voice]'` (it adds `cryptography`; without it the run is refused, naming
  the command). The key is 32 random bytes, as 64 hex characters or base64:
  `opseval audio keygen` makes one. A short key, or a placeholder such as a repeated character,
  is refused.
- Every file has a fresh random nonce, and its run, test, attempt, turn and speaker are bound
  into it: a file edited, or moved to another turn, attempt or run, does not open. The record
  holds each turn's SHA-256, which is checked again after decryption.
- The record keeps the key's fingerprint (`key_id`), never the key, so after a rotation you can
  tell which key opens which run. **Keep the key**: without it the audio cannot be played back,
  by design.
- `OPSEVAL_RECORD_AUDIO=hashes` keeps no audio at all: each turn's SHA-256, length, codec, rate
  and duration.
- The demo's synthetic voices are written as plain WAV, unless a key is set: then they are
  sealed like a real run's, which is a quick way to try the commands below.
- Audio the agent sent that could not be decoded is kept as it came, sealed the same way.

### Listening back to a call

When a voice scenario fails, listen to what the agent actually said.

**In the console** (`opseval serve` with the same `OPSEVAL_AUDIO_ENCRYPTION_KEY`): open the run,
then an attempt's conversation. Each spoken turn has a ▶ control, including a turn nothing was
heard in (often the one that failed), and the attempt has **Play whole call** and **Download
WAV**. The server decrypts in memory for each request, behind the console's token, and answers
`Cache-Control: no-store`; nothing decrypted is written to disk. A turn that cannot be played
says why instead: *audio pruned*, *only hashes kept*, *no key to decrypt* (the server has none),
*encrypted with another key*.

**From the command line:**

```bash
opseval audio list reports/<run id> --test vr-001             # every turn, its times and state
opseval audio export reports/<run id> --test vr-001 --attempt 0 --out call.wav       # whole call
opseval audio export reports/<run id> --test vr-001 --attempt 0 --mono --out call.wav
opseval audio export reports/<run id> --test vr-001 --attempt 0 --turn 3 --out turn3.wav
opseval audio export reports/<run id> --test vr-001 --attempt 0 --speaker agent --out agent.wav
```

- The whole call is **stereo** by default: the caller on the left, the agent on the right, so
  you can tell them apart even when they overlap. `--mono` mixes them (summed, clipped to 16
  bits where they overlap). `--speaker` keeps one side.
- Each turn plays at its time on the attempt's clock, with the silences between turns. A turn
  the record shows starting before the previous one ended is moved to that end, unless the
  bridge recorded a barge-in for it, which is kept as the overlap it was: audio sent faster than
  it plays (`audio-http`, or the demo) would otherwise sound like two people talking at once.
- `--turn N` takes the seq that `opseval audio list` and the console show. An undecodable agent
  turn is silent in the whole call (and named); `--turn N --raw` writes its bytes as they came.
- Errors exit 3 and say why: `AUDIO_KEY_MISMATCH` names both key ids, a pruned run says
  "audio pruned on <date>", a hashes-only run says "only hashes were kept".

The HTML report embeds no audio. For each voice attempt it says how many turns were recorded and
gives the exact `opseval audio export` command.

### Retention

Recorded audio has its own, shorter retention: `OPSEVAL_AUDIO_RETENTION_DAYS` (30 by default).
`opseval prune` removes a finished run's audio once the run is older than that, long before the
run itself goes (`OPSEVAL_RETENTION_DAYS`, 90). It is a dry run without `--yes`
(`--audio-older-than DAYS` overrides the setting). The record is never rewritten: it keeps every
turn's hash and transcript, so text scorers still rescore, and `audio/PRUNED.json` records when
the audio went, so it reads as "audio pruned on <date>", never as missing evidence.

Credentials are redacted from transcripts as from any text. The audio itself is not redacted.
