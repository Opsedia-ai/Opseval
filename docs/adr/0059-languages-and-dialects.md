# ADR-0059: Languages and dialects

- **Status:** Proposed
- **Date:** 2026-10-28
- **Deciders:** Opseval maintainers (owner request 2026-10-28)

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
