# ADR-0054: Speech providers

- **Status:** Proposed
- **Date:** 2026-10-28
- **Deciders:** Opseval maintainers (owner request 2026-10-28)

## Context

The voice bridge (ADR-0053) turns the simulated customer's text into speech (TTS) and the agent's
speech into text (STT). Both are network calls to a provider that Opseval does not control, and
both receive conversation content: the customer's script goes to TTS, the agent's voice goes to
STT. So the same rules apply as to the LLM roles:

- **No vendor SDKs** (ADR-0003). The core talks HTTP through httpx.
- **Every destination is allowlisted, with TLS off loopback** (`config.check_url_allowed`).
- **Secrets are `SecretStr`** and go through `config.Settings`; no other module reads the
  environment.
- **A provider outage is a harness error.** If a TTS outage looked like an agent that never
  answered, the one rule would be broken on the first bad day of a provider.

Providers differ by region and dialect. A Saudi bank may need a GCC-hosted STT; a Cantonese
agent needs a model that knows Cantonese. Teams must be able to bring their own provider without
forking Opseval.

## Decision

### Built-in: the OpenAI-compatible audio API

Two clients over httpx, in `opseval/speech/`:

- `POST {OPSEVAL_TTS_URL}/v1/audio/speech` (model, voice, input, response format).
- `POST {OPSEVAL_STT_URL}/v1/audio/transcriptions` (multipart, `response_format=verbose_json`
  for word timings and confidence where the server gives them).

That one shape covers OpenAI, Groq, speaches and a local Whisper server. A field the provider
does not return (confidence, word timings) is `None` on `FrameRecord.transcript`, never 0, so a
check that needs it reports `NOT_MEASURABLE`.

### Plugins: `opseval.tts`, `opseval.stt`, later `opseval.telephony`

Three new entry-point groups, loaded the way `plugins.load_credential_provider` loads a credential
provider (ADR-0046):

- **Named to be used.** `OPSEVAL_TTS_PROVIDER` and `OPSEVAL_STT_PROVIDER` name one entry point
  each (default `openai`, the built-in). Only the named one is imported.
- **Fail closed.** A name no installed plugin provides, an import error, a contract violation or a
  constructor that raises is a `ConfigError` (exit 3). Opseval never falls back to another
  provider: a run that quietly used a different voice or ASR would measure something else.
- **The contract** is a plain class: `synthesize(text, voice, language) -> Audio` and
  `transcribe(audio, language, hint) -> Transcript`, both async. `opseval.testing` gains a
  contract kit for them, as `ScorerContract` is for scorers.
- **Guarded.** As `GuardedTokens` wraps a credential provider, the runner wraps a speech plugin:
  anything it raises becomes `TtsError` or `SttError`, naming the provider and the exception type
  only. A plugin's `TimeoutError` must never reach the bridge as the agent's timeout.

### Settings

All in `config.Settings` with docstrings (they feed `docs/reference/settings.md`):

- `OPSEVAL_TTS_URL`, `OPSEVAL_TTS_KEY` (`SecretStr`, in `SECRET_FIELDS`), `OPSEVAL_TTS_MODEL`,
  `OPSEVAL_TTS_VOICE_MAP` (persona voice name to provider voice; packs supply defaults,
  ADR-0059).
- `OPSEVAL_STT_URL`, `OPSEVAL_STT_KEY` (`SecretStr`), `OPSEVAL_STT_MODEL`,
  `OPSEVAL_STT_TIMEOUT_SECONDS`, `OPSEVAL_STT_MIN_CONFIDENCE`.
- `OPSEVAL_TTS_TIMEOUT_SECONDS`. Each call has its own limit, like the simulator's.
- `OPSEVAL_STT_CANARY_MAX_WER`: the bound for the STT health canary of ADR-0053.
- `OPSEVAL_STT_SECOND_PROVIDER`, `OPSEVAL_STT_SECOND_URL`, `OPSEVAL_STT_SECOND_KEY` (`SecretStr`),
  `OPSEVAL_STT_SECOND_MODEL`: the optional second STT check of ADR-0053. It must differ from the
  first in provider or model, or it is refused at launch (exit 3): the same STT twice proves
  nothing. It is allowlisted, region-checked and preflighted like the first.

### Errors

`TtsError` and `SttError` subclass `HarnessError` in `errors.py`. A provider 429 is also
`RateLimited`, so the run's `stop_on_rate_limit` policy (ADR-0035) sees it as a typed event, not
as error text. A 4xx other than 429 is a configuration error from Opseval's side and is reported
as such.

### Preflight

`preflight.py` gains `check_tts` and `check_stt`. Each sends one short phrase (synthesise, then
transcribe it back) to the configured provider, only when the suite has a voice scenario and only
to an allowlisted host. An unreachable provider skips the voice scenarios it silences
(`skipped_precondition`), as a dependency outage does today (ADR-0020). A refused host is exit 3.

### Usage and cost

`llm.UsageMeter` gains `audio_in_seconds`, `audio_out_seconds` and characters synthesised per role
(`tts`, `stt`) and model, the field names of the usage events of [ADR-0066](0066-dashboards.md).
A price is per second or per character. An unknown price stays `None` ("no price"), never $0, as
it is for tokens today.

### Regions

A task's region (ADR-0049) carries a list of providers it may use, e.g. a GCC task only with
GCC-hosted endpoints. The runner checks the configured TTS and STT hosts against that list at
launch. A mismatch is a `ConfigError` (exit 3), never a warning, because the audio would already
have left the region by the time anyone read a warning.

## Consequences

- OpenAI, Groq, speaches and local Whisper work with settings alone. Others (a regional
  provider, a cloud speech service) are plugins, and the core carries no SDK for them.
- Two more destinations per run receive conversation content. Both are allowlisted and preflighted.
- The fault-injection suite gains a provider that refuses, stalls, returns 5xx, returns an empty
  transcript and returns garbage audio. Each must land on the row ADR-0053 gives it.
- `opseval.telephony` is reserved now, so ADR-0056 adds no new loading rule.

## Not decided here

- Which regional providers get first-party plugins. That follows the language-pack order
  (ADR-0059) and the design partners.
- Streaming STT (partial transcripts while the agent speaks). The full-duplex driver (ADR-0055)
  may need it for barge-in; the contract above takes whole turns.

## Implementation (V0b, 2026-10-28)

Built in `opseval/speech/`, with no voice bridge, transport or driver change yet (V0c):

- `speech/base.py`: the contracts. `TextToSpeech.synthesize(text, *, voice, language) -> Audio`
  and `SpeechToText.transcribe(audio, *, language, hint=None) -> Transcript`, both async.
  `Audio` is PCM16 mono plus its rate. `Transcript` carries text, provider, model, confidence
  (`None` when not given) and word timings (empty when not given), and turns into the record's
  `TranscriptInfo` with `info()`. A provider is built from a `SpeechConfig`: its role's URL
  (already allowlisted), model, key, timeout, voice map, and a `secret(name)` reader. A
  plugin's constructor takes that config.
- `speech/openai.py`: the built-in clients. Retries follow `llm.py`. 429 that outlasts its
  retries is `TtsRateLimited`/`SttRateLimited` (`RateLimited`). Another 4xx is
  `TtsRequestError`/`SttRequestError`, with no retry. Undecodable audio (a non-audio content
  type, an unreadable WAV, odd-length PCM, an empty body) is a `TtsError`. A non-JSON body or
  one with no `text` is an `SttError`. Word timings are kept all or nothing, because a partial
  list would read as an agent that said fewer words. Confidence is the mean of
  `exp(avg_logprob)` over the segments, and only when every segment has one. The language goes
  out as ISO-639-1 (`ar-EG` becomes `ar`).
- `speech/guard.py`: every provider, built-in or plugin, runs behind
  `GuardedTextToSpeech`/`GuardedSpeechToText`. These give each call its own limit
  (`asyncio.timeout`, retries included), check the answer, count usage and resolve the voice
  through `OPSEVAL_TTS_VOICE_MAP` (persona voice, then `ar-EG`, then `ar`, then `default`,
  then the persona voice as given). Anything a plugin raises becomes `TtsError`/`SttError`
  naming the provider and the exception type only. That includes a `TimeoutError`, a
  `TransportTimeout` and even its own `SttError`, whose message could carry a key. Only a
  built-in's own messages are kept.
- `speech/providers.py`: `text_to_speech`, `speech_to_text` and `second_speech_to_text` build
  the guarded providers from `Settings`. Each one allowlists its URL first, plugin or not.
  `check_second_stt` refuses a second STT that has the first's provider and model, is only
  partly configured, or has no first STT (`ConfigError`, exit 3).
- Settings: the names above, plus `OPSEVAL_TTS_PROVIDER`/`OPSEVAL_STT_PROVIDER` (default
  `openai`), `OPSEVAL_TTS_FORMAT` (`wav`|`pcm`) and `OPSEVAL_TTS_PCM_RATE`.
  `OPSEVAL_STT_CANARY_WAV` and `OPSEVAL_STT_CANARY_TEXT` are a canary the operator supplies
  when there is no TTS. Opseval bundles no recorded speech, because a recording's licence would
  need Gate 0 clearance. The three keys are in `SECRET_FIELDS`.
- Plugins: `plugins.load_speech_provider(group, name, config)` loads from `opseval.tts` and
  `opseval.stt`. `TELEPHONY_GROUP = "opseval.telephony"` is reserved. A plugin may not call
  itself `openai`.
- Preflight: `check_tts` speaks the canary phrase. `check_stt` transcribes it (from the TTS,
  else the operator's WAV) and compares the result with `speech.wer.word_error_rate`.
  Normalisation is NFKC, then casefold, then removal of combining marks and the tatweel, then
  punctuation of every script becomes a space. A WER over `OPSEVAL_STT_CANARY_MAX_WER` is
  `unavailable`. With no known speech, the provider still transcribes a second of silence and
  the check is `unverified`. `preflight(..., voice_scenarios=N)` runs these checks, plus the
  second STT's when it is configured. Every speech host is allowlisted before anything is
  probed. With `N = 0`, as for every suite until the bridge lands, nothing is built or sent.
- Usage: `ModelUsage` gains nullable `audio_in_seconds`, `audio_out_seconds` and `characters`.
  They are `None` on older results and on LLM roles. `UsageMeter.record_speech` counts roles
  `tts` and `stt` per model. A plugin's model is labelled `provider:model`, so two providers
  that serve the same model id are counted apart. `llm.SPEECH_PRICES` (per second, per
  character) is empty, so cost is `None`.
- An import-linter contract keeps `opseval.speech` away from transports, the driver, the
  simulator and the runner.

Not built yet: the region check (needs ADR-0049's tasks), the `opseval.testing` contract kit
for speech plugins, skipping voice scenarios on a speech outage (needs voice scenarios, V0c),
and the fault-injection rows that need the bridge.
