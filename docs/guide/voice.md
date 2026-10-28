# Voice agents (preview)

Opseval can test an agent that speaks. The simulated customer's words are turned into speech
(TTS), streamed to the agent over its own protocol, and the agent's spoken answer is turned back
into text (STT). The driver, the scorers and the judges then work as they do for text
([ADR-0053](../adr/0053-voice-sessions-and-records.md)).

!!! note "Preview"
    This is the turn-based bridge. Barge-in and talk-over (ADR-0055), phone calls (ADR-0056),
    WebRTC (ADR-0057) and encrypted audio at rest are not built yet.

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
export OPSEVAL_RECORD_AUDIO=hashes          # see "Audio and privacy" below

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

A voice can be biometric data, so recorded audio must be encrypted at rest. Encryption arrives
in the next voice step. Until then, a voice run is refused (exit 3) unless it keeps hashes only:

- `OPSEVAL_RECORD_AUDIO=hashes` keeps no audio at all: each turn's SHA-256, length, codec, rate
  and duration.
- `OPSEVAL_RECORD_AUDIO=files` (the default) writes one WAV per speaker per turn under the run
  folder's `audio/`. Only the demo's synthetic voices may do so without encryption.
- Setting `OPSEVAL_AUDIO_ENCRYPTION_KEY` is refused for now, since this version cannot encrypt.

Credentials are redacted from transcripts as from any text. The audio itself is not redacted.
