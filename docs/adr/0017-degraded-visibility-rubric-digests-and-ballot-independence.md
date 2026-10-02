# ADR-0017: Degraded evidence must be visible, rubric digests for every judge, and genuinely independent ballots

- **Status:** Proposed
- **Date:** 2026-10-02
- **Deciders:** Opseval maintainers

## Context

Three gaps that ADR-0016 named as follow-up, all of the same shape: a caveat existed in the
data but never reached the person reading the result.

1. **`degraded` was almost invisible.** A degraded score still counts toward the verdict, but
   `degraded` was rendered only as a badge in the HTML report. `verdicts.py`, `exit_code()`, the
   JUnit writer and the Markdown gate summary — the last of which is what `opseval summary` and
   the GitHub Action post onto a pull request — all ignored it. So a score made on frame evidence
   instead of traces, or by a judge from the graded model's own vendor family under
   `OPSEVAL_JUDGE_INDEPENDENCE=warn`, arrived in CI as an ordinary green. That is not a false
   pass, but it is a measurement presented as stronger than it is.
2. **Rubric digests covered one judge.** ADR-0016 added `rubric_hash` to
   `security.policy_adherence` because its criteria come from the scenario. But *every* judge
   grades against scenario data that `prompt_hash` cannot see: `judge.language_consistency` reads
   the persona's language, `judge.goal_achievement` and `judge.efficiency` read the goal and the
   expected outcome, `security.no_injection_takeover` reads the declared payloads. Change a
   persona from `ar` to `en`, run `opseval rescore` offline, and the old Arabic grades came back
   marked `reused: True` and were presented as current.
3. **Multi-vote ballots were not independent.** ADR-0014's three ballots were sequential calls
   with an identical prompt at `temperature=0.0`, differing only by `seed`. Most
   OpenAI-compatible gateways ignore `seed`, so three ballots could be one response three times —
   `JUDGE_DISAGREEMENT` could essentially never fire, while costing three round-trips.

## Decision

**1. `degraded` is reported wherever the verdict is.** `TestVerdict.degraded_scorers` and
`SuiteResult.degraded_scorers` aggregate it, and it appears in:

- the Markdown gate summary, as a named warning that says the scores *counted toward the
  verdict* — including on a green summary, which is the case that matters, because nothing else
  there hints at it;
- the JUnit XML, as a suite property, a per-case property, and a `system-out` note. The note
  matters because a passed case carries no `failure` or `error` element, so without it the
  weaker evidence has nowhere to appear;
- the console summary, under the scenario it belongs to.

Degradation does **not** change a status, a pass rate or an exit code. It is a caveat on a
score, not a verdict of its own, and the JUnit counts are asserted unchanged. Making it fail
would silently convert "measured, with a caveat" into "failed", which is the mirror of the
mistake ADR-0016 fixed.

**2. Every judge declares its rubric.** `Judge.rubric(ctx)` returns the scenario-derived data
that judge grades against, and the base `rubric_hash` digests it with the scorer id. Each judge
overrides `rubric`, not `rubric_hash`.

The rubric must be derived from the **scenario only, never the record**. `judge.efficiency`
shows why: its prompt carries turn counts, which are record data. A digest including those would
differ on every attempt, so no score could ever match and nothing would be reusable — the check
would degrade into "never reuse", which is useless rather than honest.

`opseval rescore` compares the digest before reusing a score and now says *which* condition
failed: the judge was never scored, the prompt changed, the scenario data changed, or the run
predates rubric digests so the rubric cannot be verified. That last case refuses reuse for every
pre-existing run. That is a real loss of convenience and the honest answer: an unverifiable
rubric is not a verified one, and ADR-0013 exists to stop a verdict moving without an
attributable cause.

**3. Ballots are concurrent and differ by construction.** The votes of a multi-vote judge are
cast with `asyncio.gather` (inside the engine's existing judge slot, so the concurrency bound is
unchanged), and each ballot presents the criteria in the declared order **rotated by its vote
index**.

Rotation rather than a seeded shuffle: with three criteria a shuffle collides often and can even
reproduce the declared order, so distinctness would be a coin flip. Rotation is guaranteed
distinct for as many ballots as there are criteria, which covers every judge here, and it spreads
position bias because each criterion leads in some ballot. Beyond that count it repeats, and a
seeded shuffle breaks the tie. Vote 0 keeps the declared order so the primary ballot stays
canonical.

`temperature` stays `0.0`. The variation is in the prompt, not in the sampling, so a run remains
reproducible — a judge that disagreed with itself because of temperature would be noise, not a
signal. The score is computed from the declared order whatever the presentation, and
`prompt_hash` is unchanged, so earlier results stay comparable.

## Consequences

- `warn` becomes a defensible setting rather than a silent one: a self-graded or frame-backed
  pass is now named in the PR comment, the JUnit XML and the console.
- Re-scoring a run recorded before this change no longer reuses judge scores; those scorers
  report `JUDGE_UNAVAILABLE` with the reason, and re-running them needs a judge configured. Runs
  recorded from now on carry the digest and reuse normally.
- Multi-vote judges still cost one call per vote, but the calls now overlap, so a 3-vote judge is
  roughly one round-trip of latency rather than three.
- A judge with more votes than criteria falls back to a seeded shuffle for the surplus ballots,
  where distinctness is likely but not guaranteed. No judge is configured that way today.
- Library callers driving `ScoringEngine` directly still bypass the launch-time independence
  check (ADR-0016), but their degraded scores are now visible in any of the three outputs they
  render. Moving the check into the engine itself remains open.
