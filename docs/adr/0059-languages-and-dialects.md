# ADR-0059: Languages and dialects

- **Status:** Proposed
- **Date:** 2026-10-28
- **Deciders:** Opseval maintainers (owner request 2026-10-28)
- **Implementation:** Step 1 implemented (2026-10-28): *Unknown is a load error* and *BCP-47
  codes, old names kept* (`opseval.langpacks`). The new packs, `voice_map`, `stt_language`,
  `script`, `output.language_script` v2 and the judge's dialect label are not built yet. Two
  details this ADR left open: the existing Gulf pack covers Saudi Arabia and the UAE, so it is
  tagged `ar-x-gulf` (private use, as `ar-x-codeswitch`) and `ar-SA`, `ar-AE`, `ar-KW`, `ar-QA`,
  `ar-BH`, `ar-OM` are declared fallbacks *to the Gulf pack* (the nearer pack, not the base
  language) until each has its own; and the dialects that silently became MSA before (Levantine,
  Maghrebi, `ar-LB`, `ar-MA`, …) and regional English (`en-US`, `en-GB`, …) are declared
  fallbacks, so they still load, now with a warning. Only an LLM persona uses a pack, so only an
  LLM persona is refused: scripted turns are sent as written and load with a warning when their
  language has no pack. Judges grade against the language resolved once
  (`langpacks.effective_language`): for an LLM persona the pack the simulator was told to write
  (a fallback included, so `ar` + `levantine` is graded as MSA rather than blaming the agent or
  invalidating the attempt for the harness's own stand-in), for scripted turns the declared
  language. Every spelling of one pack gives the same judge label and rubric digest; the
  canonical spellings give exactly the old ones. `judge.language_consistency` and
  `judge.persona_consistency` went to version 3. A regional pack (`fr-CA`) is reachable without
  a base pack. *(Step 1's "not built yet" list is superseded by the MENA note below.)*
- **MENA step implemented (2026-10-30).** All new packs are *preview* (`stable=False`) until the
  promotion process in `docs/guide/languages.md` (a native-speaker review, then `opseval
  calibrate` and the STT canary) is done; nothing flips `stable` automatically. New packs:
  `ar-SA` (`ksa`), `ar-AE` (`uae`), `ar-KW` (`kuwaiti`), `ar-x-levant` (`levantine`, `shami`;
  stands in for `ar-SY`, `ar-PS`), `ar-LB`, `ar-JO`, `ar-IQ`, `ar-MA` (`moroccan`, `darija`),
  `ar-DZ`, `ar-TN` and `ar-x-codeswitch` (Gulf Arabic with English in Latin letters).
  Granularity: one pack per Gulf country the roadmap names, Qatar, Bahrain and Oman kept as
  declared fallbacks of `ar-x-gulf`; Lebanese and Jordanian packs plus a general Levantine one;
  one pack per Maghreb country (`maghrebi`, `libyan`, `sudanese`, `yemeni` stay MSA fallbacks).
  Every pack writes Arabic script; an Arabizi variant is not built. The aliases `saudi` and
  `emirati` still name the Gulf pack (nothing that resolved exactly moved);
  `LanguagePack.narrower` lets `advice` name `ar-SA` and `ar-AE` beside them. What moved are
  the declared fallbacks, which warned "no pack yet": `ar-SA`, `ar-AE`, `ar-KW`, `kuwaiti` (were
  Gulf) and the Levantine, Iraqi and Maghrebi names (were MSA) now resolve to their own packs;
  `rescore` re-grades such runs because the rubric changed. A pack carries `name`
  (human-readable), `scripts` and, for code-switching, `mixes` (the judges are told the mix as
  for `voice.code_switching`, which still wins when set). Judges are told
  `Arabic (Saudi), ar-SA` and the tag joins the rubric; the four older packs keep the old label
  byte for byte (`langpacks.LEGACY_LABELS`), so the judges' versions are unchanged and
  `test_prompt_hashes` holds. `output.language_script` is version 2: letters counted by Unicode
  name over ten scripts (`arabic`, `latin`, `han`, `kana`, `hangul`, `cyrillic`, `devanagari`,
  `hebrew`, `greek`, `thai`), in the script when the expected scripts together outnumber any
  other; a `script` left out comes from the pack (no pack and no `script` is `BAD_PARAMS`, not
  measured); `arabic` and `latin` work as before. Kana is its own script for now (give `han` or
  `kana`); the Japanese pack will decide. `voice_map` is **not** a pack field: Opseval has no
  vendor voice ids to ship (OpenAI-compatible voices are language-agnostic), so the voice comes
  from `OPSEVAL_TTS_VOICE_MAP`, now looked up by the pack's tag (`langpacks.voice_language`:
  `ar` + `ksa` is spoken as `ar-SA`), with a recommended mapping in the guide. `stt_language`
  is not built: STT is asked for the primary subtag, as before.
- **Review of the MENA step (2026-10-30).** A `language` + `dialect` pair that loaded before and
  now names two packs (`ar-SA` + `gulf`, `ar-LB` + `levantine`) keeps the pack it resolved to
  then, from frozen copies of the step-1 tables (`langpacks.legacy_lookup`), with a warning
  naming the packs it now also names; no pair that loaded is refused. Each attempt records the
  pack it resolved to (`RunRecord.persona["pack"]`, no layout change), and judges grade against
  it (`langpacks.graded_language`); a record without it is graded with the step-1 resolution,
  so `rescore` keeps its rubric (this replaces "`rescore` re-grades such runs" above).
  `LanguagePack.scripts` is required; a code-switching pack declares `primary_script` and
  `primary_share` (`ar-x-codeswitch`: half Arabic), and a pack with a private-use tag may
  declare `provider_language`: providers are given BCP-47 only (`ar-x-*` becomes `ar`), the
  voice map is still keyed by the pack tag, and preflight speaks the language attempts speak.
  `output.language_script` takes `script` (one, or `japanese` = `han` + `kana`) or `scripts`
  (a list) and `min_primary_share`; with neither, the scripts come from the pack, a declared
  fallback or the base pack (with a warning), and with none of those the scenario does not
  load. A code-switching pack and a different `voice.code_switching` do not load either. One
  Unicode script table (`langpacks.SCRIPT_PREFIXES`) serves the scorer and the voice bridge.
- **US/EU and Chinese steps implemented (2026-10-31).** Roadmap steps 2 and 3, all *preview*.
  New packs: `en-US` (`american`), `en-GB` (`british`, `uk`), `fr-FR`, `de-DE`, `es-ES`,
  `it-IT`, `zh-CN` (`mandarin`; Mandarin in Simplified characters) and `zh-HK` (`cantonese`;
  Cantonese in Traditional characters). `en` stays the stable base pack, unchanged (prompt,
  label, digest); `en-AU`, `en-CA`, `en-IE`, `en-NZ` stay its declared fallbacks. What moved
  are the fallbacks `en-US`, `en-GB`, `american`, `british`, `uk` (they warned "no pack yet"):
  each now names its own pack; a recorded attempt keeps the pack it ran with
  (`RunRecord.persona["pack"]`, else the step-1 tables), and `en-GB` + `american` (two English
  fallbacks then, two packs now) keeps `en` with a warning. The roadmap names `fr`, `de`, `es`
  and `it`: each bare code is its country pack (`LanguagePack.default_for_code`, one per code),
  so `language: fr` is `French (France), fr-FR`; another region (`fr-CA`, `es-MX`, `es-419`,
  `de-AT`) has no pack and is refused, never written as the European variety. No `pt-*` pack.
  `zh` alone names **no** pack (Mandarin or Cantonese must be said), and neither Chinese pack
  declares a fallback: `zh-TW`, `zh-MO`, `zh-SG`, `zh-Hant`, `zh-Hans` are refused, and a
  Cantonese customer is never simulated or graded as Mandarin. The script-explicit
  tags `zh-Hans-CN` and `zh-Hant-HK` name the same packs (aliases); `cmn` and `yue` are not
  language codes Opseval maps. **The written form.** Simplified and Traditional characters are
  both Unicode Han, so `output.language_script` checks `han` only; no character-variant table
  ships (none Opseval could verify). A pack's `written_form` is told to the language and
  persona judges on its own trusted line (`Expected written form: …`) and joins the rubric:
  `zh-CN` is Simplified, and Traditional or written Cantonese does not match; `zh-HK` is
  Traditional, with written Cantonese and Standard Written Chinese (書面語) **both acceptable**,
  as this ADR asks; the simulator writes colloquial written Cantonese. `en-US` and `en-GB` say
  that a US/UK spelling difference is not a change of language, so neither judge fails an
  agent, or invalidates an attempt, on spelling. Packs without a `written_form` (every older
  one) keep their prompts and digests, so the judges' versions are unchanged. `advice` warns
  when a Chinese scenario has `output.language_script` and no `judge.language_consistency`
  (`script_cannot_tell`): the written form is then not measured. Cantonese-only characters are
  named in the pack and the judge note, not used as a heuristic check. **The simulator** is
  told `Language: Chinese (Cantonese, Hong Kong) (zh-HK). <instruction>` for every pack with a
  human-readable name; the four packs in `LEGACY_LABELS` keep their prompt. **Typing noise** is
  per pack (`LanguagePack.keyboard`): `qwerty` (the default, byte-identical noise to before),
  `azerty` (`fr-FR`), `qwertz` (`de-DE`), and `ime` for Chinese: Han, kana and Hangul
  characters are never changed, because an IME's errors are whole wrong characters (homophones
  from the candidate list), which Opseval does not model; Latin words in the message still
  slip on QWERTY, and `advice` says so when `typing_noise` is set. **Voice.** No vendor voice
  ids ship (as for MENA): `OPSEVAL_TTS_VOICE_MAP` is looked up by the pack tag, then the primary
  language, then `default`, so a `zh-CN` entry never speaks for `zh-HK` (map both; a plain `zh`
  key would serve both). OpenAI-compatible voices are language-agnostic and are **unverified**
  for Cantonese (they may read Traditional characters in Mandarin). `stt_language` is still not
  built: the built-in STT is asked for `zh` for both packs, unverified for Cantonese, so
  `zh-HK` voice results wait on the STT check of the promotion steps. The STT canary has
  phrases for `fr`, `de`, `es`, `it`; Chinese keeps the English canary, because the WER is
  word-level and Chinese is written without spaces. The voice bridge's spoken checkpoint words
  (`SPOKEN_DECISIONS`) still cover `en` and `ar` only, so a French or Chinese voice caller
  approves in English: left for the telephony work, named in ATTENTION.

## Context

A persona names the customer's language and dialect (`persona.language`, default `en`, and
`persona.dialect`). The simulator turns that into writing instructions through
`simulator.langpacks.resolve`. Today there are four packs: `en` (stable), `ar` (Modern Standard
Arabic), `ar`/`egyptian` (aliases `eg`, `egypt`, `masri`) and `ar`/`gulf` (aliases `khaleeji`,
`saudi`, `emirati`).

`resolve` falls back silently, twice:

- an unknown dialect becomes the base language: `ar`/`levantine` becomes MSA;
- an unknown language becomes English: `fr` or `de` becomes English.

This is ATTENTION I14 (High). The simulated customer writes English for a French scenario, and
`opseval validate` says nothing. Then `judge.persona_consistency`, on the harness axis, can
invalidate attempts until the scenario is inconclusive, so the harness blocks a release for its
own mistake. Or the persona judge misses it, and a French agent is graded on an English
conversation. Either way a harness problem looks like a result.

Voice (ADR-0053) makes this matter more. A dialect now also picks a TTS voice and an STT language,
and a wrong STT language turns a correct agent into an unreadable transcript. The owner's order is
MENA first, then the US and Europe, then Chinese.

## Decision

### Unknown is a load error, never a fallback

- `resolve` raises `UnknownLanguage`, a `ConfigError`, when no pack matches. The catalogue loader
  calls it for every persona, so `opseval validate` and the console editor report it, and a run
  never starts with it (exit 3).
- A dialect falls back to its base language **only when the base pack declares it**
  (`LanguagePack.fallback_for`, a list of dialect codes). The fallback adds a warning through
  `advice.scenario_warnings`, shown by `validate` and the editor. A warning never changes an exit
  code. A dialect no pack declares is `UnknownLanguage`.

### BCP-47 codes, old names kept

- Packs are named with BCP-47 tags: `ar-SA`, `ar-AE`, `ar-EG`, `en-US`, `zh-HK`. A persona may
  give the tag in `language` alone, or as `language` plus `dialect`, as today.
- Every existing alias still resolves to the pack it resolves to now (`saudi` to the Gulf pack,
  `masri` to Egyptian), so no saved scenario changes meaning and `rescore` digests stay stable.
  When a more specific pack exists, `advice` names it.
- Matching ignores case and treats `_` as `-`.

### What a pack holds

- `instruction` and `examples`, as today.
- `voice_map`: per TTS provider (ADR-0054), the voices that speak this dialect, with gender and
  age where the provider says. A persona can pick one by name; the default is the first.
- `stt_language`: the code to send to STT, which is not always the same tag (Cantonese often
  needs `yue`).
- `script`: the Unicode script the dialect is normally written in, used by
  `output.language_script` below.
- `stable`. A pack is marked stable only after **both** a native-speaker review of its
  instructions and examples, **and** an STT accuracy check with `opseval calibrate` (ADR-0018)
  over labelled recordings in that dialect. Calibration reports the rate at which STT errors would
  have failed a correct agent, separately, because that is the direction the one rule forbids. An
  unstable pack runs, with a warning.

### The roadmap

Each step is an amendment to this ADR, adding packs:

1. **MENA:** Saudi (`ar-SA`), Emirati (`ar-AE`), Kuwaiti (`ar-KW`), Egyptian (`ar-EG`),
   Lebanese (`ar-LB`), Jordanian (`ar-JO`), Moroccan (`ar-MA`), Algerian (`ar-DZ`), Tunisian
   (`ar-TN`), MSA (`ar`), and Arabic–English code-switching (`ar-x-codeswitch`, private-use
   subtag), as spoken in the Gulf.
2. **US and Europe:** `en-US`, `en-GB`, `fr`, `de`, `es`, `it`.
3. **Chinese:** Mandarin `zh-CN` and Cantonese `zh-HK`. Spoken Cantonese is written in
   traditional characters with Cantonese-only words, and a chat customer in Hong Kong may write
   formal written Chinese instead. The `zh-HK` pack states which register the simulator writes,
   and the language judge is told both are acceptable written forms.

### `output.language_script` v2

- `script` gains `han`, `kana` and `hangul` beside `arabic` and `latin`, matched by Unicode script
  ranges, not by "Arabic versus Latin". The default comes from the persona's pack, so a Mandarin
  scenario does not need to say `han`.
- A message is "in the script" when that script has the most letters in it. Digits,
  punctuation and redaction markers are ignored, as markers are now (`redaction.strip_markers`).
- Kana counts with Han for Japanese only if the pack says so.
- The scorer's `version` goes from 1 to 2, so `rescore` reports the change.

### `judge.language_consistency` uses the dialect label

Today the judge is told `Expected customer language: ar (gulf)`. It will be told the pack's
label (`Arabic, Saudi (ar-SA)`), and the `dialect_register` criterion stops naming Arabic only:
"the same dialect, or the standard written form of the language". The rubric (ADR-0017) includes
the pack tag. The prompt changes, so the prompt hash moves on purpose, and the judge's version is
bumped.

## Consequences

- I14 is closed: a language Opseval cannot simulate stops the run before it starts, with the file
  and the persona named.
- Some scenarios that load today will fail to load. That is intended. The release note lists the
  error and the fix (add a pack, or use a declared fallback).
- Each new pack needs a native speaker and labelled recordings before it is stable. That is the
  slow part of the roadmap, and it is not skipped.
- `persona.language` stays a string; no scenario file needs to change unless it used a language
  that had no pack.

## Not decided here

- Community pack contributions as plugins (an `opseval.langpacks` group, ADR-0022's list).
- Right-to-left rendering fixes in the HTML report and console for mixed-direction text.
