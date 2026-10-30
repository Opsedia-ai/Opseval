# Your own criteria: rubric judges and content checks

The built-in judges grade fixed things: goal achievement, language, writing quality, efficiency,
security. Two kinds of check let a team say what *good* means for its own agent
([ADR-0061](../adr/0061-rubric-judges-and-content-checks.md)):

- **`judge.rubric`**: yes/no questions you write, answered by the judge with a quote from the
  conversation. For things only a reader can decide: "did it offer a human handover after the
  second complaint?"
- **`content.*`**: deterministic checks with no LLM. For things a comparison can decide: "the
  reply contains the order id", "the JSON answer has `status: approved`". They cost nothing and
  never vary, so prefer them whenever they can decide the question.

Both go in a scenario's `scorers:` list, like any other check. **Each scorer id runs once per
scenario**, so listing one twice is a load error rather than a check that never runs: put several
strings in one `content.contains` (`all_of`, `any_of`), several criteria in one `judge.rubric`, or
the second check in a scenario of its own.

## A rubric

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
        - name: handover_offered
          question: >-
            After the customer complained a second time, did the agent offer to pass the case
            to a human?
```

Each criterion:

| Field | Default | Meaning |
|---|---|---|
| `name` | required | A slug (`a-z`, `0-9`, `_`), unique in the rubric. Results and calibration labels use it. |
| `question` | required | A yes/no question, at most 500 characters, phrased so that **"yes" is the good answer**. |
| `weight` | 1 | Its share of the score. |
| `votes` | 1 | 1, 3 or 5 independent answers; the majority decides, and a split is never a pass. |
| `must` | false | A failed `must` fails the whole score, whatever the weights and the threshold. |
| `quotes` | `always` | `on_fail`: a pass may be unquoted, for something the agent did *not* do. A fail always needs the quote that shows it. |

At most 20 criteria. `params.axis` says where a failure counts: `output` (default), `flow`,
`tools` or `security`. It can never be `harness`: your rubric fails the agent, it cannot
invalidate an attempt.

### How it is scored

- The judge answers every criterion with "yes" or "no" and a quote copied from the conversation
  or from a fact the harness recorded (a checkpoint decision, how the conversation ended). A
  quote that is not there makes that answer invalid. **Opseval computes the score; the model
  never gives a number.**
- The score is the weighted share of criteria answered "yes", against the `threshold` (default
  1: every criterion must pass).
- A criterion can be **unmeasured**: its answers were invalid, too few were valid, or its votes
  split. An unmeasured criterion is never dropped from the arithmetic, because that would grade a
  smaller rubric than the one you wrote. Instead:
    - the score **fails** if it would fail even had every unmeasured criterion passed (a failed
      `must` is one such case);
    - it **passes, marked degraded**, only if it would pass even had every unmeasured criterion
      failed (so an unmeasured `must` never passes); the degraded reason names them;
    - otherwise it is **not measured** (`JUDGE_CRITERIA_UNMEASURED`), naming each criterion and
      why.
- With `votes: 3` on some criteria, the judge is called three times, not once per criterion:
  call *k* answers every criterion with more than *k* votes, each time in a different order.

`opseval validate` warns about a rubric with no `must` criterion under a threshold below 1: it
passes with some criteria failed, whichever they are.

### The criteria are data, not instructions

Questions may come from a dataset row or a generated draft, so they are treated like the
conversation: they reach the judge as JSON lines inside their own fence, named with a fresh
random nonce on every call. Nothing inside a question can change the reply format, the quote rule
or the score.

### Placeholders

With a [dataset](../adr/0060-datasets-and-matrices.md), a question can name the row's values:

```yaml
dataset: datasets/orders.csv
scorers:
  - id: judge.rubric
    params:
      criteria:
        - name: order_confirmed
          question: Did the agent confirm order {{order_id}} by its number?
```

Each row gets its own question, and its own rubric digest.

### Rescoring and calibration

- The rubric digest covers every criterion (name, question, weight, votes, must, quotes) and the
  axis, so `opseval rescore` re-grades whenever any of them changes, and never reuses a grade
  given under other criteria. Reordering the criteria, or spaces around a question, does not
  change it.
- `opseval calibrate` accepts labels per criterion, `judge.rubric:<name>`, and reports agreement
  and the false-pass rate for each one. Label the criteria that matter most first:

```bash
opseval calibrate reports/<run> --template-for judge.rubric:return_window > labels.yaml
# replace every REVIEW with passed or failed, then:
opseval calibrate reports/<run> --labels labels.yaml
```

- The judge independence guard applies as to every judge: a judge from the same vendor family as
  `profile.sut_model` is refused at launch.

### Arabic

Write the question in the language you think in; the judge quotes the conversation as it is.

```yaml
persona: {role: عميل يريد إرجاع طلب, language: ar, dialect: egyptian}
scorers:
  - id: judge.rubric
    params:
      criteria:
        - name: return_window
          question: هل ذكر الوكيل أن الإرجاع مقبول خلال ١٤ يومًا من الاستلام؟
          must: true
        - name: same_dialect
          question: هل ردّ الوكيل باللهجة المصرية أو بعربية فصحى واضحة؟
```

## Content checks

All four read the redacted text (a credential the agent said is `[redacted:KIND]`), are on the
`output` axis, and are *not measured* only for a harness reason, never for an answer they did
not like.

### `content.contains` and `content.not_contains`

```yaml
- id: content.contains
  params:
    all_of: ["10482"]          # every one of these
    any_of: ["refund", "credit"]  # and at least one of these
    scope: any_turn            # any_turn | last_turn | every_turn
    speaker: agent             # agent | customer
    case_sensitive: false
    ignore_diacritics: false
- id: content.not_contains
  params:
    any_of: ["CompetitorShop", "cheaper elsewhere"]
```

- Text is compared after Unicode NFKC and with whitespace runs read as one space; case is ignored
  unless `case_sensitive: true`.
- `ignore_diacritics: true` also ignores combining marks and the Arabic tatweel on both sides, so
  `مرحبا` finds `مَرْحَبًا` and `cafe` finds `café`. Arabic-Indic digits (`١٠٤٨٢`) are not the
  same as `10482`: name the form the agent writes.
- `any_turn`: each `all_of` string somewhere, and one `any_of` string somewhere. `every_turn`:
  in each of the speaker's turns. `last_turn`: in the speaker's last turn.
- `content.not_contains`: every string in `all_of` and `any_of` is forbidden, and one of them
  in one examined turn fails it, whatever the scope (`last_turn` examines only the last turn;
  `any_turn` and `every_turn` examine them all). It is the same rule as `content.regex` with
  `must_match: false`.
- An agent that said nothing fails `contains` and is a *vacuous* pass of `not_contains`, named as
  one; `conversation.agent_replied` is the check that fails the silence.

### `content.regex`

```yaml
- id: content.regex
  params:
    pattern: "خلال \\d+ (يوم|أيام)"   # "within N days", Arabic
    must_match: true          # false: it must not be found in any examined turn
    scope: any_turn
```

- Python's `re` syntax (`\d` matches Arabic-Indic digits too). Case-sensitive by default:
  `case_sensitive: false` or `(?i)` relaxes it.
- The pattern is compiled when the scenario loads, so a bad one is an error at its field.
- `ignore_diacritics: true` strips combining marks and the Arabic tatweel from the text, and
  nothing else (whitespace and escapes mean what they say). A pattern that names a mark, or a
  letter that carries one (`é`), is refused at load, since it could never match.
- **The guard.** Python's `re` can take exponential or polynomial time on some patterns, so these
  are refused at load:
    - a pattern over 500 characters;
    - a quantifier inside a quantified group: `(a+)+`, `(\w*)*`;
    - alternation inside a quantified group: `(a|ab)*`, `(?:foo|bar)+` (use a character class,
      `[xy]+`, where you can);
    - two unbounded quantifiers (`*`, `+`, `{n,}`) with no literal character between them:
      `.*.*`, `\s+\w+` (put a literal between them, as in `\d+-\d+`, or bound one, `{1,40}`);
    - more than three unbounded quantifiers.
- The search runs in a separate Python process, killed after 5 seconds: a timeout is *not
  measured* (`REGEX_TIMEOUT`), since a slow pattern is the author's problem, not the agent's. A
  turn longer than 10,000 characters, counted after `ignore_diacritics`, is *not measured*
  (`REGEX_INPUT_TOO_LONG`) rather than cut, since a match past the cut would be a false fail.
  How to run team patterns on a shared server worker is still to decide (ADR-0061).

### `content.json_path_equals`

```yaml
- id: content.json_path_equals
  params:
    source: result            # result | message | checkpoint
    path: data.refunds.0.status
    equals: approved
```

or, in another scenario (one `content.json_path_equals` per scenario):

```yaml
- id: content.json_path_equals
  params:
    source: checkpoint
    checkpoint: proposal_approval
    path: proposal.method
    equals: original_card
```

- `result`: the agent's result frames. `message`: the agent's text parsed as JSON (the whole text,
  or one fenced code block); a message that is not JSON fails, since the agent did not give the
  structured answer. `checkpoint`: that checkpoint's payload (the approved occurrence, else the
  last), and the name is checked against the profile at load.
- `scope`: `last_turn` (default), `any_turn` or `every_turn` over the messages or results.
- `path` walks objects by key and lists by index, as `output.payload_equals` does. `equals` is any
  JSON value; `true` is not `1`, and `1` equals `1.0`.

Every parameter is listed in the [scorer reference](../reference/scorers.md).
