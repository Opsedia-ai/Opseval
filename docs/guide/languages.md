# Languages and dialects

The simulated customer writes in the language and dialect its persona names. A **language
pack** tells the LLM customer how a real customer types in that dialect, gives it a few example
phrases, and tells the judges and `output.language_script` what to expect
([ADR-0059](../adr/0059-languages-and-dialects.md)).

```yaml
persona:
  role: Customer in Riyadh whose order is late
  language: ar-SA          # a BCP-47 tag alone...
# or
  language: ar             # ...or a language plus a dialect name
  dialect: ksa
```

Matching ignores case and reads `_` as `-` (`AR_sa` is `ar-SA`). A language or dialect no pack
names is a **load error** for an LLM persona (exit 3), never a silent switch to English or to
another dialect. Scripted turns are sent as written, so they load with a warning instead.

## The packs

| Tag | Dialect name | Other names | Status | Stands in for (with a warning) |
|---|---|---|---|---|
| `en` | — | — | stable | `en-US`, `en-GB`, `en-AU`, `en-CA`, `en-IE`, `en-NZ`, `american`, `british`, … |
| `ar` | — | `msa` | preview | `maghrebi`, `libyan`, `sudanese`, `yemeni`, `ar-LY`, `ar-SD`, `ar-YE` |
| `ar-EG` | `egyptian` | `eg`, `egypt`, `masri` | preview | — |
| `ar-x-gulf` | `gulf` | `khaleeji`, `saudi`, `emirati` | preview | `ar-QA`, `ar-BH`, `ar-OM`, `qatari`, `bahraini`, `omani` |
| `ar-SA` | `ksa` | `saudi-arabia` | preview | — |
| `ar-AE` | `uae` | — | preview | — |
| `ar-KW` | `kuwaiti` | — | preview | — |
| `ar-x-levant` | `levantine` | `shami` | preview | `ar-SY`, `ar-PS`, `syrian`, `palestinian` |
| `ar-LB` | `lebanese` | — | preview | — |
| `ar-JO` | `jordanian` | — | preview | — |
| `ar-IQ` | `iraqi` | — | preview | — |
| `ar-MA` | `moroccan` | `darija` | preview | — |
| `ar-DZ` | `algerian` | — | preview | — |
| `ar-TN` | `tunisian` | — | preview | — |
| `ar-x-codeswitch` | `codeswitch` | `code-switching`, `arabic-english` | preview | — |

**Preview** means the pack runs but has not had the native-speaker review and the STT check
that make it stable (below). The guided scenario form marks the language "(preview)", and the
scores it produces are as trustworthy as the pack's wording. Every Arabic pack is a preview today.

### How the packs are grouped

- **Gulf.** Saudi Arabia, the UAE and Kuwait each have a pack. Qatar, Bahrain and Oman stay
  declared fallbacks of the Gulf pack (`ar-x-gulf`): their chat writing is close to it, and a
  pack of their own waits for a native reviewer.
- **Levant.** Lebanese and Jordanian have packs. A general Levantine pack (`ar-x-levant`, the
  common urban register of Damascus, Beirut and Amman) stands in for Syria and Palestine.
- **Maghreb.** Moroccan, Algerian and Tunisian each have a pack, because they differ a lot.
  `maghrebi` alone stays an MSA fallback: no one of them may stand in for the others.
- **Iraq** and **Egypt** have a pack each; **MSA** is `ar`.
- **Code-switching** (`ar-x-codeswitch`): everyday Gulf Arabic in Arabic script with English
  words and short phrases in Latin letters, as bilingual customers in the Gulf type ("أبي
  refund لو سمحت"). The judges are told the customer mixes `ar` and `en` on purpose, and
  `output.language_script` accepts Arabic and Latin. For a *spoken* caller who switches,
  `voice.code_switching` ([ADR-0055](../adr/0055-full-duplex-voice-driver-and-metrics.md))
  still applies and, when set, wins over the pack's list.

### Script: Arabic letters, not Arabizi

Many customers, in Lebanon and the Maghreb especially, type Arabic in Latin letters with digits
for some sounds (Arabizi: `3ayez`, `ba9`). Every Arabic pack writes **Arabic script**, and says
so to the simulator, because that is what most customers type in a support chat, what an
Arabic-speaking agent is built for, and what `output.language_script` and the judges can check.
An Arabizi variant is not built; a scenario that needs one can use scripted turns.

### Names kept from before

Every name that resolved to a pack before still resolves to the same pack, so no saved scenario
changes meaning and no rubric digest moves: `saudi` and `emirati` are still the **Gulf** pack.
`opseval validate` and the editor name the narrower pack (`ar-SA`, `ar-AE`) in a warning; use its
tag to switch.

What did move are **declared fallbacks**, which loaded with a "no pack for this dialect yet"
warning: `ar-SA`, `ar-AE`, `ar-KW` and `kuwaiti` were written as Gulf Arabic, and `levantine`,
`lebanese`, `jordanian`, `iraqi`, `moroccan`, `algerian`, `tunisian` (and their tags) as MSA.
They now resolve to their own packs, as the warning promised; `syrian` and `palestinian` moved
from MSA to the Levantine pack and still warn. A saved run of such a scenario re-scored with
`opseval rescore` is re-graded, because the judges' rubric now names the new pack.

## What the judges are told

`judge.language_consistency` and `judge.persona_consistency` are told the pack's name and tag:
`Expected customer language: Arabic (Saudi), ar-SA`. The four packs that existed before
(`en`, `ar`, `ar-EG`, `ar-x-gulf`) keep the old label (`ar (egyptian)`) byte for byte, so their
saved grades and digests stay valid. The rubric of a new pack includes its tag.

## `output.language_script`

Version 2 counts the letters of each message by Unicode script: `arabic`, `latin`, `han`,
`kana` (hiragana and katakana), `hangul`, `cyrillic`, `devanagari`, `hebrew`, `greek`, `thai`.
A message is in the script when the expected scripts together have more letters than any other
script. Digits, punctuation and redaction markers never count; a reply with no letters is not
an answer in the script.

```yaml
scorers:
  - {id: output.language_script}                      # the persona's pack decides
  - {id: output.language_script, params: {script: han, min_fraction: 0.9}}
```

Left out, `script` comes from the persona's pack (`arabic` for the Arabic packs, `arabic` and
`latin` for `ar-x-codeswitch`, `latin` for English). With no pack (scripted turns in a
language Opseval has no pack for) and no `script`, the check is *not measured*
(`BAD_PARAMS`), never a guess. `script: arabic` and `script: latin` work as before.

## Voices for a voice run

A voice run speaks and hears the pack's tag: a persona written `ar` + `ksa` is spoken as `ar-SA`.
Opseval does not ship vendor voice ids. The voice comes from `OPSEVAL_TTS_VOICE_MAP`
([ADR-0054](../adr/0054-speech-providers.md)), looked up by the persona's `voice.caller_voice`,
then the pack tag, then the primary language (`ar`), then `default`. STT is asked for the
primary language (`ar`), which is what OpenAI-compatible transcription takes.

OpenAI-compatible TTS voices (`alloy`, `nova`, …) are not tied to a language: they read Arabic
text with whatever accent the model has, usually not a regional one. For a dialect check, use a
provider that publishes voices per locale and map each pack to a voice of that locale:

| Key in `OPSEVAL_TTS_VOICE_MAP` | Map it to |
|---|---|
| `ar-SA`, `ar-AE`, `ar-KW`, `ar-EG`, `ar-LB`, `ar-JO`, `ar-IQ`, `ar-MA`, `ar-DZ`, `ar-TN` | a voice your provider lists for exactly that locale |
| `ar-x-gulf` | a Saudi or Emirati voice (`ar-SA` or `ar-AE` locale) |
| `ar-x-levant` | a Syrian, Lebanese or Jordanian voice (`ar-SY`, `ar-LB`, `ar-JO`) |
| `ar-x-codeswitch` | a Gulf voice that reads English words in Latin letters well; listen first |
| `ar` | a Modern Standard Arabic voice (many providers list it as `ar-SA` or `ar-XA`) |
| `default` | any voice, for the languages you did not map |

```bash
export OPSEVAL_TTS_VOICE_MAP='{"ar-SA": "<your ar-SA voice>", "ar-EG": "<your ar-EG voice>",
  "ar": "<your MSA voice>", "default": "alloy"}'
```

Check each mapping by ear before trusting a dialect score, and run the STT check below.

## Promoting a pack to stable

Nothing flips `stable` automatically. A pack becomes stable in a pull request that records both
of these, and the reviewer's name, in that pull request:

1. **Native-speaker review.** A native speaker of the dialect (not of a neighbouring one) reads
   the pack in `packages/opseval/src/opseval/langpacks.py`: the instruction, the example
   phrases, the dialect words it names, and the script rule. They check that a customer of that
   region would type this in a support chat, that no word belongs to another dialect, and that
   the examples are generic (written for Opseval, not copied from any product or company). Then
   they read 10 or more simulated conversations from a run with the pack and say, per attempt,
   whether the customer wrote the dialect. Those answers are labels:

    ```bash
    opseval run suites/dialect.yaml --profile profile.yaml      # LLM persona, the pack's tag
    opseval calibrate reports/<run> --template-for judge.persona_consistency > labels.yaml
    # the reviewer fills in passed/failed per attempt, then:
    opseval calibrate reports/<run> --labels labels.yaml --labelled-by "<reviewer>"
    ```

    The persona judge must agree with the reviewer, and its **false-pass rate** (it passed a
    customer the reviewer failed) must be within the limit `calibrate` enforces. Do the same for
    `judge.language_consistency` on the agent's replies if the dialect is new to the judge model.

2. **STT check.** Recordings of native speakers in the dialect (or TTS samples the reviewer
   has listened to and accepted), each with its exact words. For each sample:

    ```bash
    OPSEVAL_STT_CANARY_WAV=sample.wav OPSEVAL_STT_CANARY_TEXT="<its words>" \
    OPSEVAL_STT_CANARY_LANGUAGE=ar-SA opseval preflight suites/dialect.yaml --profile voice-profile.yaml
    ```

    The canary passes when the WER (with Arabic orthography folded) is at most
    `OPSEVAL_STT_CANARY_MAX_WER`. Then label a voice run of a **correct** agent in the dialect
    (`voice.intelligible`, `judge.pronunciation_dialect_fit`) and run `opseval calibrate`: its
    **false fails** are the attempts where STT errors would have failed a correct agent, the
    direction the core rule forbids. They must be zero for the pack to be stable.

Record the STT provider and model the check used: a pack is stable for the speech stack it was
checked with, and the pull request says so.
