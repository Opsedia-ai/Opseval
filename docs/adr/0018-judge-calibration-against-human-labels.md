# ADR-0018: Judge calibration against human labels

- **Status:** Proposed
- **Date:** 2026-10-02
- **Deciders:** Opseval maintainers

## Context

ADR-0016 refuses a judge that shares a vendor family with the model it grades. That is a proxy:
it infers unreliability from the vendor name because nothing better was available. The direct
question — *does this judge agree with our reviewers on our own conversations?* — had no answer
in the tool, so a team adopting Opseval had to take the judges on trust.

The risk is specific. A judge can look fine on aggregate agreement and still be useless, because
the two ways of being wrong are not symmetric:

- the judge **fails** what a human passed: annoying, visible, someone investigates;
- the judge **passes** what a human failed: a false green. The release gate opens and a broken
  agent ships. Nobody investigates, because nothing looked wrong.

A single agreement percentage hides the second case whenever most attempts genuinely pass.

## Decision

`opseval calibrate <run> --labels labels.yaml` compares the scores a finished run recorded with
human labels for the same attempts. It is offline and read-only — an import-linter contract
forbids it from reaching transports, the driver, the simulator or the runner, like
`opseval rescore` — and it writes a timestamped `calibration-<stamp>.json` beside the run without touching it — one
file per calibration, so a second reviewer's labels cannot silently replace the first's. A folder
it cannot write (a read-only CI artifact directory) is a `ConfigError` → exit 3; letting `OSError`
escape would exit 1, which in Opseval's scheme means the thing under test failed.

**Labels are explicit and validated.** A label is `{test_id, attempt_index, scorer_id, verdict}`
with `verdict: passed | failed`, in a file with `extra="forbid"`. A duplicate label for the same
cell is a load error rather than last-one-wins: two reviewers disagreeing is real information,
and silently keeping one of them is not an answer to it. `--template-for <scorer id>` prints a
ready-to-fill file for every attempt in the run that has that score. Each row's verdict is the
sentinel `REVIEW`, which is **rejected on load**: pre-filling "passed" would mean an unedited or
half-filled template reported 100% agreement and exit 0, certifying a judge nobody had reviewed.
A label naming a scorer the run never scored is likewise a load error, not a silently dropped
row — the module's own promise is that a mistake in a label file cannot quietly shrink the sample
a judge is measured on.

**Per scorer, it reports:** usable labels, agreement, Cohen's kappa, false passes, false fails,
and the false-pass *rate* — the share of the attempts humans failed that the judge passed. That
last number is the one that matters, and it is reported separately precisely because aggregate
agreement hides it.

**The core rule applies to calibration itself.** Calibration is a measurement, so it has to be
as honest as the thing it measures:

- A score that was `infra_error` was never measured, so it cannot agree or disagree. Those labels
  are excluded from the comparison and counted under `not_measured`, never scored as a match.
- Fewer usable labels than `--min-labels` (default 5) is `not_measured` → **exit 2**, never
  "agrees". A judge is not vindicated by a sample too small to test it.
- A rate over an empty denominator is `None`, not `0.0`. If humans failed nothing, there is no
  false-pass rate, and printing 0% would read as "this judge never lets a failure through" on
  evidence that says nothing at all.
- **Kappa is `None` when undefined.** If *either* rater used a single verdict across the sample,
  the statistic is meaningless: with a degenerate marginal it collapses toward 0 however well the
  two actually agree. Ten labels all marked "passed" against a judge that passed eight gives
  kappa 0.0, which reads as "no better than chance" on a sample that simply cannot tell. The
  guard therefore tests each rater's marginal rather than only the combined expected agreement,
  and the result carries a `kappa_note` naming which rater was uniform.
- A label naming an attempt that is not in the run is a configuration error (exit 3), not a
  skipped row. A typo that silently shrank the sample would quietly weaken the very check being
  run.

**Exit codes match the suite** (ADR-0006), so calibration can gate CI like anything else:
`0` agrees · `1` disagrees · `2` not enough usable labels to tell · `3` configuration error. A
scorer disagrees if its agreement is below `--min-agreement` (default 0.8) **or** its false-pass
rate exceeds `--max-false-pass-rate` (default 0.1) — either one is disqualifying on its own, so a
judge with 90% agreement that passed the single attempt humans failed still fails.

## Consequences

- A team can now answer the judge-trust question with their own data instead of inferring it from
  a vendor name, which is what ADR-0016 said this work was for. The family check stays a cheap
  default; calibration is the real evidence.
- Labelling is manual and that is the point: the cost is the reviewer's time, and the tool refuses
  to pretend a thin sample is enough.
- The defaults (0.8 agreement, 0.1 false-pass rate, 5 labels) are starting points, not findings.
  Five labels is a low bar chosen so the command is usable on a small run; a team gating releases
  on calibration should raise it substantially.
- Calibration measures a scorer against labels from one run. It does not track agreement over
  time, weight criticality, or detect a judge that drifts after a model upgrade. Storing results
  per run is the groundwork for that, but trend analysis is not in scope here.
- Kappa on a two-rater, two-category table is a blunt instrument, and with uniform labels it is
  unavailable exactly when a team most wants reassurance. The honest reading of
  `kappa: not measured` is "label some failures too", which the `kappa_note` says.
- Nothing here feeds the release gate automatically. A calibration result does not change a
  scorer's weight, threshold or status in a later run; it informs the humans configuring the
  suite. Making a stale or failing calibration block a run is a larger decision about where
  trust is recorded, and is deliberately left open.
