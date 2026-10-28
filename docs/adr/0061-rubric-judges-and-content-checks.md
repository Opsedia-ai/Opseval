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

## Not decided here

- How `content.regex` is protected against catastrophic backtracking in server mode, where one
  member's pattern runs on a shared worker. The options are the `regex` package with a timeout, an
  RE2 binding, or a restricted syntax. Each needs a licence check.
- A shared rubric library (rubrics referenced by id across scenarios).
