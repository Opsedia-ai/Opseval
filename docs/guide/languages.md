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
| `en` | — | — | stable | `en-AU`, `en-CA`, `en-IE`, `en-NZ`, `australian`, `canadian`, `irish` |
| `en-US` | `american` | — | preview | — |
| `en-GB` | `british` | `uk` | preview | — |
| `fr-FR` | — | `fr` alone | preview | — |
| `de-DE` | — | `de` alone | preview | — |
| `es-ES` | — | `es` alone | preview | — |
| `it-IT` | — | `it` alone | preview | — |
| `zh-CN` | `mandarin` | `zh-Hans-CN` | preview | — |
| `zh-HK` | `cantonese` | `zh-Hant-HK` | preview | — |
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
scores it produces are as trustworthy as the pack's wording. Every pack but `en` is a preview
today.

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
  `output.language_script` accepts Arabic and Latin, as long as at least half of each reply's
  letters are Arabic (an all-English reply is not code-switching). For a *spoken* caller,
  `voice.code_switching` ([ADR-0055](../adr/0055-full-duplex-voice-driver-and-metrics.md))
  may be set too, but only to the same languages (`[ar, en]`, in any order): a different list
  is a load error, because the simulator and the judges would be told different mixes.

- **English.** `en` is the stable base pack, plain everyday English, unchanged. `en-US` and
  `en-GB` add US or UK spelling, words, date order and tone. Other regions (`en-AU`, `en-CA`,
  `en-IE`, `en-NZ`) are written as plain English, with a warning.
- **Western Europe.** `fr-FR` (French, polite *vous*), `de-DE` (German, polite *Sie*, umlauts
  and ß), `es-ES` (Spanish as spoken in Spain, *vosotros*, *vale*) and `it-IT` (Italian,
  polite *Lei*). The bare codes `fr`, `de`, `es` and `it` are these packs. Another region
  (`fr-CA`, `fr-BE`, `de-AT`, `de-CH`, `es-MX`, `es-419`) has no pack and is **refused**, so a
  Mexican customer is never written as a Spaniard without anyone saying so; use the bare code
  if European wording is acceptable, knowingly. There is no Portuguese pack.
- **Chinese.** `zh-CN` is Mandarin in Simplified characters, as typed in mainland China;
  `zh-HK` is Cantonese in Traditional characters, as typed in Hong Kong: colloquial written
  Cantonese (嘅, 咗, 喺, 唔, 係), with an English word here and there. `zh` alone is **refused**:
  say which. Neither stands in for the other, and `zh-TW`, `zh-MO`, `zh-SG`, `zh-Hant` and
  `zh-Hans` have no pack (refused). `zh-Hans-CN` and `zh-Hant-HK` are other spellings of the
  two packs.

### Moved by the US/EU step

`en-US`, `en-GB`, `american`, `british` and `uk` were declared fallbacks of `en` (they loaded
with a "no pack yet" warning); they now name their own packs. A saved run keeps the pack it was
graded against (below), and the pair `language: en-GB` + `dialect: american`, which loaded as
English before, still loads as English, with a warning naming both packs. `fr`, `de`, `es`,
`it` and `zh-*` had no pack, so an LLM persona in them was refused; scripted turns in them,
which loaded with a warning, now load without one, and their typing noise follows the pack's
keyboard.

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

The same holds for a **pair** of fields that loaded before: `language: ar-SA` with
`dialect: gulf` (or `saudi`), `ar-KW` + `gulf`, `ar-LB` + `levantine`, `ar-MA` + `maghrebi`, and
so on. The two fields now name different packs, but the pair keeps the pack it resolved to then
(the Gulf pack, or MSA) and loads with a warning naming the packs it now also names. Give only
the tag to use the new pack. A pair that never loaded (`ar-SA` + `egyptian`) is still refused.

What did move are **declared fallbacks**, which loaded with a "no pack for this dialect yet"
warning: `ar-SA`, `ar-AE`, `ar-KW` and `kuwaiti` were written as Gulf Arabic, and `levantine`,
`lebanese`, `jordanian`, `iraqi`, `moroccan`, `algerian`, `tunisian` (and their tags) as MSA.
They now resolve to their own packs, as the warning promised; `syrian` and `palestinian` moved
from MSA to the Levantine pack and still warn.

A **saved run** keeps the language it was graded against. Each attempt records the pack it
resolved to (`pack` in the record's persona), and judges grade a re-scored attempt against that
pack. A run recorded before this was written is graded with the resolution of its time (so
`ar` + `lebanese` stays MSA), and `opseval rescore` keeps its rubric digest and its grades. A
new run of the same scenario uses the new pack.

## What the judges are told

`judge.language_consistency` and `judge.persona_consistency` are told the pack's name and tag:
`Expected customer language: Arabic (Saudi), ar-SA`. The four packs that existed before
(`en`, `ar`, `ar-EG`, `ar-x-gulf`) keep the old label (`ar (egyptian)`) byte for byte, so their
saved grades and digests stay valid. The rubric of a new pack includes its tag.

Some packs also tell the judges their **written form**, on a line of its own, and it joins the
rubric:

| Pack | `Expected written form:` says |
|---|---|
| `zh-CN` | Simplified characters; Traditional characters or written Cantonese do not match |
| `zh-HK` | Traditional characters; written Cantonese **and** Standard Written Chinese (書面語) are both acceptable, mixed too; Simplified characters do not match |
| `en-US`, `en-GB` | a US/UK spelling or vocabulary difference is not a change of language |

So an agent that answers a Hong Kong customer in Simplified characters fails
`judge.language_consistency`, and one that answers in formal written Chinese does not. A
customer simulated in the wrong written form fails `judge.persona_consistency`, which is on the
harness axis: the attempt is invalidated, never blamed on the agent.

The simulator is told the pack's name too: `Language: Chinese (Cantonese, Hong Kong) (zh-HK).`
before its instruction (the four older packs keep their prompt).

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
  - {id: output.language_script, params: {script: japanese}}       # han and kana together
  - {id: output.language_script, params: {scripts: [cyrillic, latin]}}
```

`script` is one script, or `japanese` (kanji and kana, written together in one sentence);
`scripts` is a list counted together. Give one of them, not both.

Left out, the scripts come from the persona's pack (`arabic` for the Arabic packs, `arabic` and
`latin` for `ar-x-codeswitch`, `latin` for English). For scripted turns in a dialect or region
with no pack of its own, they come from the pack that stands in for it, else from the
language's base pack (`ar-OM` and `ar` + `hassaniya` take `arabic`), and `validate` warns that
the default is a stand-in's. When nothing gives one (`sw`, with no Swahili pack), leaving
`script` out is a **load error**: the check could never be measured. `script: arabic` and
`script: latin` work as before.

**Chinese: Han only.** `zh-CN` and `zh-HK` both expect `han`. Simplified and Traditional
characters are the same Unicode script, so `output.language_script` cannot tell them apart and
does not try (no character-variant table ships, because none could be verified). The written
form is graded by `judge.language_consistency`, told the pack's form (above). A Chinese
scenario with `output.language_script` and no `judge.language_consistency` loads with a warning
that the written form is not measured.

`min_primary_share` is the share of a reply's letters the first expected script must have.
Left out, it is the pack's own (`ar-x-codeswitch`: 0.5, so a reply must be at least half
Arabic), and none when `script` or `scripts` is given. `0` turns it off.

## Typing noise

`persona.typing_noise` mistypes words with seeded, reproducible slips, never touching numbers,
URLs, e-mail addresses, currency codes, quoted text or planted injections. A slip that hits a
neighbouring key uses the pack's keyboard:

| Keyboard | Packs |
|---|---|
| QWERTY | `en`, `en-US`, `en-GB`, `es-ES`, `it-IT`, the Arabic packs (Arabic letters use the Arabic PC layout) |
| AZERTY | `fr-FR` |
| QWERTZ | `de-DE` |
| input method (`ime`) | `zh-CN`, `zh-HK` |

Chinese is typed through an input method, whose real mistakes are whole wrong characters (a
homophone picked from the candidate list). Opseval does not model that, so on `zh-CN` and
`zh-HK` typing noise **never changes a Chinese character**; only Latin words in a message slip,
on QWERTY. `validate` says so when `typing_noise` is set on a Chinese persona. QWERTY gives
exactly the noise every pack had before keyboards existed.

## Voices for a voice run

A voice run speaks and hears the pack's tag: a persona written `ar` + `ksa` is spoken as `ar-SA`.
Opseval does not ship vendor voice ids. The voice comes from `OPSEVAL_TTS_VOICE_MAP`
([ADR-0054](../adr/0054-speech-providers.md)), looked up by the persona's `voice.caller_voice`,
then the pack tag, then the primary language (`ar`), then `default`. A speech provider (a
plugin included) is only ever given a BCP-47 tag: a private-use tag (`ar-x-gulf`,
`ar-x-codeswitch`) becomes its base language (`ar`) unless the pack declares a
`provider_language`. The built-in STT asks for the primary language (`ar`), which is what
OpenAI-compatible transcription takes. The preflight canary is spoken in the same language an
attempt speaks (the profile's `audio.language`, else the first scenario's pack tag).

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

For the US/EU and Chinese packs:

| Key in `OPSEVAL_TTS_VOICE_MAP` | Map it to |
|---|---|
| `en-US`, `en-GB`, `fr-FR`, `de-DE`, `es-ES`, `it-IT` | a voice your provider lists for exactly that locale; OpenAI-compatible voices read these languages, with an accent that is **unverified** |
| `zh-CN` | a Mandarin voice (`zh-CN` locale) |
| `zh-HK` | a **Cantonese** voice (`zh-HK` or `yue` locale). OpenAI-compatible voices are **unverified** for Cantonese and may read Traditional characters in Mandarin: listen first |
| `en` | an English voice, for `en` and the English regions without a pack |

Map `zh-CN` and `zh-HK` separately and do not map a plain `zh`: the lookup goes from the pack
tag to the primary language, so a `zh` entry would speak for both, while a `zh-CN` entry never
speaks for `zh-HK`. The built-in STT is asked for `zh` for both Chinese packs (OpenAI-compatible
transcription takes the primary language); whether it transcribes Cantonese well is unverified,
which is why `zh-HK` needs the STT check below before it is stable. The STT canary is spoken in
French, German, Spanish or Italian for those packs; Chinese keeps the English canary, because
its word error rate counts space-separated words and Chinese has no spaces. The voice bridge's
spoken checkpoint answers ("Yes, please go ahead") exist in English and Arabic only, so a voice
caller in another language approves in English for now.

```bash
export OPSEVAL_TTS_VOICE_MAP='{"ar-SA": "<your ar-SA voice>", "ar-EG": "<your ar-EG voice>",
  "ar": "<your MSA voice>", "default": "alloy"}'
```

Check each mapping by ear before trusting a dialect score, and run the STT check below.

## Adding a pack

A pack is a `LanguagePack` passed to `opseval.langpacks.register` (community packs need the
native-speaker review below before they are stable). `scripts` is **required**: `register`
refuses a pack without it, because a guessed default (Latin) would fail every reply of a correct
agent that writes another script. A code-switching pack also sets `primary_script` and
`primary_share`, and `mixes`. A pack with a private-use tag may set `provider_language` (a
plain BCP-47 tag) for speech providers.

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

### The US/EU and Chinese packs

The same two steps, with these specifics:

- **One reviewer per pack, from that country.** A reviewer from Quebec does not review `fr-FR`,
  a Taiwanese reviewer does not review `zh-HK`, and an American does not review `en-GB`: the
  point is to catch words and forms that belong to the neighbour. `en-US` and `en-GB` review
  spelling, words and date order; `fr-FR`, `de-DE` and `it-IT` the polite address the pack
  chose (*vous*, *Sie*, *Lei*); `es-ES` the Spain-only forms (*vosotros*, *vale*).
- **Chinese reviewers check the characters.** For `zh-CN`, that every example and simulated
  message is Simplified; for `zh-HK`, that every one is Traditional, that the written Cantonese
  reads as a Hong Kong customer types, and that the judge note's two acceptable written forms
  are right. Label `judge.language_consistency` too, with at least one agent reply in each
  written form (Cantonese, Standard Written Chinese, Simplified), because only the judge grades
  the form: its false passes on Simplified replies for `zh-HK` must be within the `calibrate`
  limit.
- **The STT check for `zh-HK` decides the STT provider.** Run it with Cantonese recordings.
  The canary's word error rate does not fit Chinese (no spaces), so judge the transcripts
  through `opseval calibrate` on `voice.intelligible` and `judge.pronunciation_dialect_fit`
  over a voice run of a correct agent, with zero false fails, and record the provider, model
  and the language code it was given (`zh` today).
