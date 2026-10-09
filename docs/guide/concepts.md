# Concepts

## Scenario, attempt, suite

A **scenario** is one test: who the customer is (the persona), what they want (the goal), how
the conversation goes (scripted turns or an LLM persona), how to answer the agent's approval
**checkpoints**, what must be true (expectations), and which **scorers** grade it.

Each scenario runs several times. Each run is an **attempt**, recorded in full. A **suite** is a
named selection of scenarios, plus its settings: repeats, the release gate, and when to stop
early.

## Scores

Every scorer returns one of three things for an attempt:

| Score | Means |
|---|---|
| `passed` | The check held |
| `failed` | The check did not hold: the agent's fault |
| `infra_error` | It could not be measured (a missing judge, a missing trace, a broken ballot) |

An unmeasured check is shown as **not measured**, never as 0, and every output names it.

## Verdicts

The attempts of a scenario fold into one verdict:

| Verdict | Means |
|---|---|
| `passed` | Enough attempts passed (`execution.consistency_threshold`, 1.0 by default) |
| `failed` | Too many failed |
| `inconclusive` | Too few attempts could be judged at all |
| `skipped_precondition` | Not run: a dependency was down, the commit guard, or the run stopped early |
| `blocked` | Quarantined with the `blocked` tag |

A pass on only some attempts is **flaky**, and the gate treats it as a gap.

## Whose fault

The rule every part of Opseval follows: a harness problem is never the agent's fault, and an
agent failure is never hidden.

- The agent did not answer in time: the agent's fault (`TIMEOUT`).
- The connection could not be opened, a dependency failed, the simulator broke, or the judge
  could not vote: the harness's. The attempt is *not judged*.
- Time spent waiting on the simulator is taken off the agent's clock.

## The release gate

The gate turns verdicts into a release decision, and the decision into an exit code. A suite's
`gate:` says what each kind of outcome does: block, warn or ignore. Presets are `standard`,
`strict` and `lenient`. Some things are never up to the policy:

- A suite that judged nothing blocks.
- A run where every attempt was a harness failure exits 3.
- "Safe to release" always means every scenario passed.

## The commit guard

The irreversible outcome (a refund issued, an order placed) is never reached by accident. A
profile names the **commit gate**, the checkpoint whose approval leads to it, and runs stop
there. Going further needs `allow_commit: true` in the scenario **and** `--allow-commit` on the
run, ideally against a test tenant.

## Independent judges

LLM judges grade fixed criteria and must quote the conversation to justify each grade; Opseval
computes the score, never the model. A judge from the same vendor family as the model it grades
is refused at launch (declare your agent's model as `sut_model` in the profile). Security judges
vote three times, and a split vote is *not judged*, never a pass. A judge receives the
conversation as fenced data, so nothing the agent writes can pose as the harness.
