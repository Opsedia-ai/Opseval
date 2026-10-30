# ADR-0061: Rubric judges and content checks (flexibility level L2)

- **Status:** Proposed
- **Date:** 2026-10-28
- **Deciders:** Opseval maintainers (owner request 2026-10-28)

## Context

The judges are fixed: goal achievement, language, persona, content quality, efficiency and the two
security judges. Each derives its rubric from the scenario (`rubric()`, ADR-0017), but a team cannot
say *what* good means for its own agent. "Did it quote the 14-day return window?" or "Did it offer a
human handover after the second complaint?" has no home today. Simple checks ("the reply contains
the order id", "the JSON answer has `status: approved`") need a judge, which costs money and adds
noise for something a string comparison can decide.

Letting teams write criteria brings three risks:

- **Prompt injection through the criteria.** Criteria are user text. They can arrive from a dataset
  row (ADR-0060) or a generated draft (ADR-0062).
- **Stale reuse.** `rescore` could reuse a score graded under different criteria.
- **Hiding failures.** Weights could average a serious failure away.

## Decision

### `judge.rubric`

```yaml
scorers:
  - id: judge.rubric
    threshold: 0.8
    params:
      criteria:
        - name: return_window
          question: Did the agent state that returns are accepted within 14 days?
          weight: 2
          votes: 3
          must: true
        - name: no_competitors
          question: Did the agent avoid recommending another shop?
          quotes: on_fail
```

- **Params.** A Pydantic model with `extra="forbid"` and `strict=True`
  ([ADR-0043](0043-defaults-from-the-profile.md)), so a typo is a load error.
  - `name`: `[a-z0-9_]+`, unique within the rubric.
  - `question`: at most 500 characters, phrased as a yes/no question.
  - `weight`: greater than 0, default 1.
  - `votes`: 1, 3 or 5, default 1.
  - `must`: default false.
  - `quotes`: `always` (default) or `on_fail`.
  - At most 20 criteria.
- **Ballots.** One ballot answers every criterion. Ballot *k* answers the criteria with more than
  *k* votes, so the number of calls is the largest `votes`, not the sum. Criteria are shuffled per
  ballot, as ADR-0017 rotates them. Each criterion's answer is the majority of its ballots. A split
  is never a pass, as with the security judges.
- **Quotes.** Every answer must quote the conversation or a harness fact, checked as in
  [ADR-0036](0036-judge-transcript-fence.md). An invalid quote is `JUDGE_INVALID_QUOTE`. With
  `quotes: on_fail`, a pass may be unquoted, for criteria about something the agent did *not* do. A
  fail still needs the quote that proves it. An unquoted pass is listed in `details`.
- **The score.** The weighted share of criteria passed, against the spec's `threshold`.
  - **A failed `must` criterion fails the score whatever the weights,** so a serious failure is
    never averaged away.
  - **If any criterion is unmeasured** (an invalid ballot, too few valid votes), the score is
    never computed from the rest, which would grade a smaller rubric than the one written. It is
    decided by the cut-short rule of `verdicts.fold_cut_short`
    ([ADR-0035](0035-run-control.md)), applied to criteria instead of attempts:
    - **failed** if it would fail even had every unmeasured criterion passed (a measured `must`
      that failed is one such case), with the unmeasured criteria named in `details`;
    - **passed** only if it would pass even had every unmeasured criterion failed (so an
      unmeasured `must` never passes), and is marked `degraded`, naming them;
    - otherwise **`infra_error`** (not measured), naming the unmeasured criteria.
- **Axis.** The rubric declares `axis: output | flow | security` (default `output`). It can never be
  `harness`, which would let a team's own rubric invalidate attempts instead of failing them.

### Criteria are fenced, like transcripts

- The criteria reach the judge as JSON lines (`{"name", "question"}`) inside their own fence,
  `<criteria-<16 hex nonce>>`. The nonce is fresh per ballot and differs from the conversation's.
- The prompt says the criteria are questions to answer, not instructions. Nothing inside either
  fence can change the answer format, the scoring or the quote rule. Opseval, not the model,
  computes the score.
- Placeholders (ADR-0060) are substituted before the fence and before hashing.

### Rescore never reuses a score graded under other criteria

- The rubric judge's `prompt_hash` covers its fixed template, `TRANSCRIPT_FORMAT` and a
  `CRITERIA_FORMAT` tag.
- Its `rubric()` returns the canonical criteria: name, question, weight, votes, must and quotes,
  plus the scenario facts the shared context already adds.
- So any change to a criterion changes the rubric digest, and `opseval rescore`
  ([ADR-0013](0013-offline-rescore.md)) re-grades instead of reusing. A weight change too: one rule
  is easier to trust than a cheaper special case.

### Independence and calibration

- Judge independence ([ADR-0016](0016-judge-independence-and-quality-judges.md)) applies unchanged.
  A judge sharing a vendor family with `profile.sut_model` is refused at launch (exit 3) under
  `OPSEVAL_JUDGE_INDEPENDENCE=error`.
- `opseval calibrate` ([ADR-0018](0018-judge-calibration-against-human-labels.md)) accepts labels
  per criterion (`judge.rubric:return_window`). The false-pass rate is reported per criterion.

### `content.*`: deterministic checks

These need no LLM. Each has a `Params` model, is listed by `opseval list-scorers` and shows its
settings in the guided form:

- **`content.contains`.** `all_of` or `any_of` (strings), `case_sensitive` (default false), `scope:
  any_turn | last_turn | every_turn` (default `any_turn`), `speaker: agent | customer` (default
  `agent`).
- **`content.not_contains`.** The same params. No agent text at all is a vacuous pass, named as one
  ([ADR-0024](0024-tool-argument-schemas.md)). `conversation.agent_replied` fails the silence.
- **`content.regex`.** `pattern` is compiled at load, so a bad pattern is a load error. It is capped
  at 500 characters. `must_match` defaults to true.
- **`content.json_path_equals`.** `path` uses `opseval.paths.dig`, shared with
  `output.payload_equals`. `equals` is the expected value, and `source: message | result` says where
  to look. An agent message that is not JSON fails, since the agent did not give the structured
  answer. A path the profile never reads is a load error where it can be told.

All content checks:

- see the redacted text ([ADR-0033](0033-redacting-credential-values.md)), as judges do;
- are on the `output` axis;
- return `infra_error` only for harness reasons, never for an answer they did not like.

## Consequences

- Teams express their own definition of good in YAML, versioned with the catalogue, and cheap checks
  no longer pay for a judge.
- Custom rubrics vary in quality. Calibration per criterion and the quote rule are the defence. The
  guided form warns when a rubric has no `must` criterion and a threshold below 1.
- `judge.rubric` and the `content.*` checks go into the scorer reference
  (`scripts/reference_docs.py`) and the JSON Schemas.

## Implementation (2026-10-30)

Built as decided; the ADR stays *Proposed* until the regex question below is settled
(ATTENTION D20). Where the implementation had to choose, it chose as follows.

- **Code.** `scoring/rubric.py` (`judge.rubric`, `RubricParams`, `RubricCriterion`) and
  `scoring/content.py` (the four `content.*` checks). The judge reuses the shared parts of
  `scoring/judges.py`: the fenced transcript, `harness_facts`, the quote check, the vendor-family
  guard and the ballot rotation (now `judges.rotate`, unchanged for the built-in judges).
- **Answers.** Each criterion is answered `"yes"` (a pass) or `"no"`, so a question is phrased
  with "yes" as the good answer. A bad answer, a missing entry or an invalid quote spoils that
  criterion's vote in that ballot only, never its siblings'. A whole ballot lost (judge error,
  no `criteria` object) spoils every criterion it asked.
- **Order and whitespace.** The criteria are hashed and presented in **name order**, so moving a
  criterion in the YAML changes neither the prompt nor the rubric digest. A question is stripped
  of outer whitespace when it loads; every other change to it, inner whitespace included,
  changes the digest. The axis is in the digest too. Ballot *k* rotates the criteria it asks by
  *k* (a seeded shuffle after a full turn), as `judges.rotate` does for the built-in judges.
- **`quotes: on_fail`.** The names of the criteria allowed an unquoted pass are listed outside the
  criteria fence: they are slugs Opseval validated, never free text. A pass with a quote still
  has the quote checked.
- **The score.** A criterion is *passed*, *failed* or *unmeasured* (split vote, too few valid
  votes). The status is decided in code from these, as the ADR says; the number reported is the
  weighted share passed, and with unmeasured criteria it is the bound that decided the status:
  the most it could be for a fail, the least it could be for a degraded pass (`details.score_is`).
  A failed `must` fails the score even when its share clears the threshold. The undecided case is
  `JUDGE_CRITERIA_UNMEASURED`, naming each criterion and why.
- **Axis.** `output` (default), `flow`, `tools` or `security`. `tools` is accepted beside the
  three the ADR names, for criteria about tool use read from the conversation. `harness` is a load
  error with the reason. Every score carries the spec's axis, the ones the engine makes itself
  too (a crash, a contract violation, speech it could not hear): `Scorer.axis_for(spec)`, the
  class's axis by default. The vendor-family guard reads it as the agent's axis, which every
  allowed value is.
- **One spec per scorer id** (review of #94). `effective_specs` keeps the first spec of each id,
  because a score is keyed by its scorer id in the record, the verdict, rescore and calibration.
  So a scenario that lists an id twice is now a **load error** naming both positions, instead of a
  second check that silently never runs. Two different checks of one kind are combined
  (`all_of`/`any_of`, more criteria) or go in separate scenarios. Keying scores by spec would
  change the result layout and was not needed for L2.
- **No agent text** is *not measured*, as for the other judges that read the agent:
  `conversation.agent_replied` fails the silence.
- **Calibration.** Labels name a criterion as `judge.rubric:<name>`; `calibrate` reads each
  criterion's own result from `details.criteria`, so agreement and the false-pass rate are per
  criterion. An unmeasured criterion is excluded like any `infra_error`. A criterion label on a
  run whose rubric was never graded (no judge) is *not measured*, not a misspelling; a name the
  run graded nowhere is refused. `--template-for judge.rubric:<name>` writes the blank file. The
  label format is unchanged.
- **Datasets.** Placeholders (ADR-0060) already reach every string under `scorers.*.params`, so a
  criterion's question is filled per row before the params are validated and hashed; each row
  gets its own digest. A placeholder in a `name` must still produce a slug.
- **Advice.** A rubric with two or more criteria, none a `must`, under a threshold below 1 is a
  warning (`opseval validate`, the editor's check).
- **The guided form** offers `judge.rubric` with its params read from the model: `criteria` is a
  JSON field (a list of objects), `axis` a choice. No form change was needed.
- **`content.contains` / `not_contains`.** Text is compared after NFKC and collapsing whitespace,
  case-folded unless `case_sensitive`; `ignore_diacritics` also drops combining marks (Arabic
  harakat, accents) and the tatweel on both sides. `any_turn` asks that each `all_of` string
  appears in some turn and one `any_of` string in some turn; `every_turn` asks it of each turn;
  `last_turn` of the speaker's last turn. `not_contains` is defined on its own, not as the
  negation of `contains` (review of #94: negated, `every_turn` or `all_of` passed when one turn
  of three broke the rule): every string in `all_of` and `any_of` is forbidden, and one of them in
  one examined turn fails it, whatever the scope, as `content.regex` with `must_match: false`
  does. An agent that sent no text fails `contains` (it did not say what was
  required) and is a vacuous pass for `not_contains`; a customer with no text is *not measured*.
- **`content.regex`.** `pattern` is compiled when the scenario loads. Two layers protect a run
  from a slow pattern (tightened in the review of #94):
  - *At load*, refused: more than 500 characters; a quantifier inside a quantified group
    (`(a+)+`, `(\w*)*`); alternation inside a quantified group (`(a|ab)*`, `(?:foo|bar)+`); two
    unbounded quantifiers with no literal character between them (`.*.*`, `\s+\w+`); more than
    three unbounded quantifiers.
  - *At run time*, the search runs in a fresh child Python process (`-I`), killed after 5 seconds
    (`REGEX_TIMEOUT`, not measured: the author's pattern, not the agent). A thread was tried and
    rejected: CPython's `re` holds the GIL for the whole search, so a thread can be neither stopped
    nor even let `asyncio.timeout` fire before the search ends. A child that cannot run is
    `REGEX_ERROR`, a harness error.
  - A turn longer than 10,000 characters, counted after normalisation, is *not measured*
    (`REGEX_INPUT_TOO_LONG`), never cut, since a match past the cut would be a false fail.
  - `ignore_diacritics` strips only combining marks and the tatweel from the text (no NFKC, no
    whitespace folding, so escapes mean what they say); a pattern that names a mark, or a letter
    that carries one, is refused at load, since it could never match. `case_sensitive` defaults to **true** here: a pattern says what
  it means, and `(?i)` or `case_sensitive: false` relaxes it.
- **`content.json_path_equals`.** `source: result` (default; the received result frames),
  `message` (the agent's text as JSON, the whole text or one fenced code block) or `checkpoint`
  (with `checkpoint:`; the approved occurrence, else the last, as `output.payload_equals`). The
  checkpoint name is checked against the profile at load. `scope` picks the last (default), any
  or every message or result. Equality is JSON equality: `true` is not `1`, `1` is `1.0`. No
  result or message at all fails.
- **Reading text on a voice attempt.** All four `content.*` checks declare `reads_text`, the
  conservative direction, so they are *not measured* when Opseval's own STT was proven broken
  (ADR-0053), `json_path_equals` on a result payload included.

## Not decided here

- How `content.regex` is protected against catastrophic backtracking in server mode, where one
  member's pattern runs on a shared worker. The options are the `regex` package with a timeout, an
  RE2 binding, or a restricted syntax. Each needs a licence check.
- A shared rubric library (rubrics referenced by id across scenarios).
