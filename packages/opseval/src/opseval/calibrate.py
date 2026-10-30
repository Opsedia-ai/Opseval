# SPDX-License-Identifier: Apache-2.0
"""``opseval calibrate``: measure a scorer against human labels (ADR-0018).

Offline and read-only. It reads a finished run, pairs each human label with the score
the run recorded for the same attempt, and reports agreement per scorer.

The core rule applies to calibration too:

- a score that was ``infra_error`` was never measured, so it cannot agree or disagree and
  is excluded from the comparison rather than counted as a match;
- too few usable labels is ``not_measured`` (exit 2), never "agrees";
- a rate with an empty denominator is ``None``, never ``0.0``;
- the judge passing what a human failed (a false green) is reported separately from the
  judge failing what a human passed, because only the first one ships a broken agent.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import yaml

from opseval import __version__
from opseval.errors import ConfigError
from opseval.models.calibration import (
    REVIEW_SENTINEL,
    AgreementStatus,
    CalibrationResult,
    HumanLabel,
    Label,
    LabelSet,
    ScorerAgreement,
)
from opseval.models.results import ScoreResult, ScoreStatus, SuiteResult
from opseval.storage import FileStore
from opseval.verdicts import EXIT_FAIL, EXIT_NOT_JUDGED, EXIT_PASS

JudgeCell = tuple[str, int, str]
"""(test_id, attempt_index, scorer_id)."""


def load_labels(path: str | Path) -> LabelSet:
    """Read and validate a label file. Raises :class:`ConfigError` (exit code 3)."""
    file = Path(path)
    try:
        raw = yaml.safe_load(file.read_text("utf-8"))
    except OSError as exc:
        raise ConfigError(f"{file}: cannot read label file ({exc})") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{file}: not valid YAML ({exc})") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{file}: expected a mapping with a 'labels' list")
    rows = raw.get("labels")
    if isinstance(rows, list):
        left = sum(1 for r in rows if isinstance(r, dict) and r.get("verdict") == REVIEW_SENTINEL)
        if left:
            raise ConfigError(
                f"{file}: {left} label(s) still say {REVIEW_SENTINEL}. Replace each with "
                "passed or failed, or delete the row - an unreviewed label would be "
                "scored as agreement."
            )
    try:
        labels = LabelSet.model_validate(raw)
    except ValueError as exc:
        raise ConfigError(f"{file}: {exc}") from exc
    seen: set[JudgeCell] = set()
    for item in labels.labels:
        cell = (item.test_id, item.attempt_index, item.scorer_id)
        if cell in seen:
            raise ConfigError(
                f"{file}: duplicate label for {item.scorer_id!r} on {item.test_id!r} "
                f"attempt {item.attempt_index}"
            )
        seen.add(cell)
    return labels


CRITERION = ":"
"""``judge.rubric:return_window`` labels one criterion of a rubric score (ADR-0061)."""

_CRITERION_STATUS = {
    "passed": ScoreStatus.PASSED,
    "failed": ScoreStatus.FAILED,
    "unmeasured": ScoreStatus.INFRA_ERROR,
}


def _criteria(score: ScoreResult) -> dict[str, ScoreStatus]:
    """Each criterion's own outcome in a score that grades several (``details.criteria``
    rows with a ``name`` and a ``result``, as ``judge.rubric`` writes them). An unmeasured
    criterion is ``infra_error``: never measured, so it cannot agree."""
    rows = score.details.get("criteria")
    if not isinstance(rows, list):
        return {}
    out = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("name"), str):
            status = _CRITERION_STATUS.get(str(row.get("result")))
            if status is not None:
                out[row["name"]] = status
    return out


def _judge_scores(result: SuiteResult) -> dict[JudgeCell, ScoreStatus]:
    """Every score by (test, attempt, scorer), and every criterion of a rubric score by
    (test, attempt, ``scorer:criterion``)."""
    out: dict[JudgeCell, ScoreStatus] = {}
    for test in result.tests:
        for attempt in test.attempts:
            for score in attempt.scores:
                out[(test.test_id, attempt.attempt_index, score.scorer_id)] = score.status
                for name, status in _criteria(score).items():
                    cell = (test.test_id, attempt.attempt_index, f"{score.scorer_id}:{name}")
                    out[cell] = status
    return out


def _lookup(scores: dict[JudgeCell, ScoreStatus], item: HumanLabel) -> ScoreStatus | None:
    """The score a label is compared with. A criterion label on an attempt whose whole rubric
    score was not measured (no judge, bad params) is not measured too, never unmatched."""
    status = scores.get((item.test_id, item.attempt_index, item.scorer_id))
    if status is not None or CRITERION not in item.scorer_id:
        return status
    base = item.scorer_id.split(CRITERION, 1)[0]
    whole = scores.get((item.test_id, item.attempt_index, base))
    return whole if whole is ScoreStatus.INFRA_ERROR else None


def _kappa(table: dict[tuple[Label, Label], int], total: int) -> tuple[float | None, str | None]:
    """Cohen's kappa for a 2x2 table, or ``None`` with the reason it is undefined.

    Kappa is undefined when either rater used a single category: expected agreement is
    then 1 and the denominator collapses. Reporting 0.0 there would read as "no better
    than chance" when the truth is "this sample cannot tell".
    """
    if total == 0:
        return None, "no usable labels"
    observed = sum(n for (h, j), n in table.items() if h == j) / total
    human_pass = sum(n for (h, _), n in table.items() if h is Label.PASSED) / total
    judge_pass = sum(n for (_, j), n in table.items() if j is Label.PASSED) / total
    # Either rater being uniform makes kappa meaningless: with a degenerate marginal the
    # statistic collapses toward 0 regardless of how well the two actually agree, which
    # reads as "no better than chance" on a sample that simply cannot tell.
    uniform = [
        who
        for who, rate in (("the humans", human_pass), ("the judge", judge_pass))
        if rate in (0.0, 1.0)
    ]
    if uniform:
        return None, (
            f"undefined: {' and '.join(uniform)} used only one verdict in this sample "
            "(label some of the opposite outcome too)"
        )
    expected = human_pass * judge_pass + (1 - human_pass) * (1 - judge_pass)
    if expected >= 1.0:  # pragma: no cover - unreachable once a uniform rater is caught
        return None, "undefined: expected agreement is 1 for this sample"
    return round((observed - expected) / (1 - expected), 4), None


def _agreement(
    scorer_id: str,
    labels: list[HumanLabel],
    scores: dict[JudgeCell, ScoreStatus],
    *,
    min_labels: int,
    min_agreement: float,
    max_false_pass_rate: float,
) -> ScorerAgreement:
    table: dict[tuple[Label, Label], int] = defaultdict(int)
    out = ScorerAgreement(
        scorer_id=scorer_id, status=AgreementStatus.NOT_MEASURED, labels=len(labels)
    )
    for item in labels:
        status = _lookup(scores, item)
        if status is None:
            out.unmatched += 1
            continue
        if status is ScoreStatus.INFRA_ERROR:
            out.not_measured += 1  # never measured, so it cannot agree
            continue
        judged = Label.PASSED if status is ScoreStatus.PASSED else Label.FAILED
        table[(item.verdict, judged)] += 1
        out.usable += 1

    if out.usable < min_labels:
        out.reason = (
            f"{out.usable} usable label(s), fewer than the {min_labels} required "
            f"({out.not_measured} not measured, {out.unmatched} with no score in this run)"
        )
        return out

    out.agreed = sum(n for (h, j), n in table.items() if h == j)
    out.agreement = round(out.agreed / out.usable, 4)
    out.kappa, out.kappa_note = _kappa(table, out.usable)
    out.false_pass = table[(Label.FAILED, Label.PASSED)]
    out.false_fail = table[(Label.PASSED, Label.FAILED)]
    human_failed = sum(n for (h, _), n in table.items() if h is Label.FAILED)
    out.false_pass_rate = round(out.false_pass / human_failed, 4) if human_failed else None

    problems = []
    if out.agreement < min_agreement:
        problems.append(f"agreement {out.agreement:.0%} is below {min_agreement:.0%}")
    if out.false_pass_rate is not None and out.false_pass_rate > max_false_pass_rate:
        problems.append(
            f"it passed {out.false_pass_rate:.0%} of the attempts humans failed "
            f"(limit {max_false_pass_rate:.0%})"
        )
    out.status = AgreementStatus.DISAGREES if problems else AgreementStatus.AGREES
    out.reason = "; ".join(problems) or None
    return out


def calibrate_run(
    run_dir: str | Path,
    labels_path: str | Path,
    *,
    min_labels: int = 5,
    min_agreement: float = 0.8,
    max_false_pass_rate: float = 0.1,
    labelled_by: str | None = None,
) -> CalibrationResult:
    """Compare a run's scores with human labels. Raises :class:`ConfigError` for exit 3."""
    folder = Path(run_dir)
    if not (folder / "result.json").is_file():
        raise ConfigError(f"{folder}: not a finished run (no result.json)")
    store, run_id = FileStore(folder.parent), folder.name
    result = store.load_result(run_id)
    label_set = load_labels(labels_path)

    scores = _judge_scores(result)
    known_cells = {(t, a) for t, a, _ in scores}
    unknown = sorted(
        {
            f"{item.test_id} attempt {item.attempt_index}"
            for item in label_set.labels
            if (item.test_id, item.attempt_index) not in known_cells
        }
    )
    if unknown:
        raise ConfigError(
            f"labels refer to attempts that are not in run {run_id}: {', '.join(unknown[:5])}"
            + (" …" if len(unknown) > 5 else "")
            + ". Check the run folder, or the test_id and attempt_index in the label file."
        )

    scored_ids = {scorer for _t, _a, scorer in scores}
    # A criterion of a rubric that was never graded anywhere in the run (no judge) is not a
    # misspelling: its labels are counted as not measured.
    graded: dict[str, bool] = {}  # scorer id -> measured at least once in the run
    for (_t, _a, sid), status in scores.items():
        if CRITERION not in sid:
            graded[sid] = graded.get(sid, False) or status is not ScoreStatus.INFRA_ERROR
    ungraded = {sid for sid, measured in graded.items() if not measured}
    misspelled = sorted(
        sid
        for sid in {i.scorer_id for i in label_set.labels} - scored_ids
        if sid.split(CRITERION, 1)[0] not in ungraded or CRITERION not in sid
    )
    if misspelled:
        raise ConfigError(
            f"labels name scorers that run {run_id} never scored: {', '.join(misspelled)}. "
            f"Scored in this run: {', '.join(sorted(scored_ids))}."
        )

    by_scorer: dict[str, list[HumanLabel]] = defaultdict(list)
    for item in label_set.labels:
        by_scorer[item.scorer_id].append(item)

    agreements = [
        _agreement(
            scorer_id,
            items,
            scores,
            min_labels=min_labels,
            min_agreement=min_agreement,
            max_false_pass_rate=max_false_pass_rate,
        )
        for scorer_id, items in sorted(by_scorer.items())
    ]

    disagreeing = [a for a in agreements if a.status is AgreementStatus.DISAGREES]
    unmeasured = [a for a in agreements if a.status is AgreementStatus.NOT_MEASURED]
    if disagreeing:
        exit_code = EXIT_FAIL
        summary = "Do not trust: " + "; ".join(f"{a.scorer_id} ({a.reason})" for a in disagreeing)
    elif unmeasured:
        exit_code = EXIT_NOT_JUDGED
        summary = "Not measured: " + "; ".join(f"{a.scorer_id} ({a.reason})" for a in unmeasured)
    else:
        exit_code = EXIT_PASS
        summary = f"{len(agreements)} scorer(s) agree with the human labels."

    return CalibrationResult(
        opseval_version=__version__,
        run_id=run_id,
        suite=result.suite,
        labelled_by=labelled_by,
        scorers=agreements,
        min_labels=min_labels,
        min_agreement=min_agreement,
        max_false_pass_rate=max_false_pass_rate,
        exit_code=exit_code,
        summary=summary,
    )


def save_calibration(run_dir: str | Path, result: CalibrationResult) -> Path:
    """Write the calibration beside the run, never over the run's own files.

    Each calibration gets its own file so a second reviewer's labels cannot silently
    replace the first's. Raises :class:`ConfigError` (exit code 3) if the folder cannot
    be written — a read-only CI artifact directory is a configuration problem, and
    letting ``OSError`` escape would exit 1, which in Opseval means the agent failed.
    """
    folder = Path(run_dir)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = folder / f"calibration-{stamp}.json"
    try:
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot write {path}: {exc}") from exc
    return path


def calibration_summary(result: CalibrationResult) -> str:
    """A console table. Unmeasured values say so; they never render as 0."""

    def num(value: float | None, fmt: str) -> str:
        return "not measured" if value is None else format(value, fmt)

    lines = [f"Calibration of run {result.run_id} · suite {result.suite}", ""]
    for a in result.scorers:
        icon = {
            AgreementStatus.AGREES: "AGREES      ",
            AgreementStatus.DISAGREES: "DISAGREES   ",
            AgreementStatus.NOT_MEASURED: "NOT MEASURED",
        }[a.status]
        lines.append(
            f"  {icon} {a.scorer_id:<32} n={a.usable:<4} "
            f"agreement {num(a.agreement, '.0%'):<13} kappa {num(a.kappa, '.2f')}"
        )
        if a.kappa_note:
            lines.append(f"  {'':<13} ↳ kappa {a.kappa_note}")
        if a.false_pass or a.false_fail:
            lines.append(
                f"  {'':<13} ↳ false passes {a.false_pass} "
                f"(rate {num(a.false_pass_rate, '.0%')}) · false fails {a.false_fail}"
            )
        if a.not_measured or a.unmatched:
            lines.append(
                f"  {'':<13} ↳ excluded: {a.not_measured} not measured, {a.unmatched} with no score"
            )
        if a.reason:
            lines.append(f"  {'':<13} ↳ {a.reason}")
    lines += ["", result.summary, f"Exit code: {result.exit_code}"]
    return "\n".join(lines)


def _labels(score: ScoreResult, scorer_id: str) -> bool:
    """Whether a label for ``scorer_id`` would be about ``score``: the scorer itself, or, for
    ``scorer:criterion``, a score of that scorer that graded that criterion."""
    if CRITERION not in scorer_id:
        return score.scorer_id == scorer_id
    base, name = scorer_id.split(CRITERION, 1)
    return score.scorer_id == base and name in _criteria(score)


def label_template(result: SuiteResult, scorer_id: str) -> str:
    """A ready-to-fill label file for one scorer across every attempt in a run."""
    rows = [
        {
            "test_id": test.test_id,
            "attempt_index": attempt.attempt_index,
            "scorer_id": scorer_id,
            # Not a real verdict: an unedited template must fail to load rather than
            # report that every label agrees with the judge.
            "verdict": "REVIEW",
        }
        for test in result.tests
        for attempt in test.attempts
        if any(_labels(score, scorer_id) for score in attempt.scores)
    ]
    if not rows:
        raise ConfigError(
            f"run {result.run_id} has no {scorer_id!r} scores to label "
            "(check `opseval list-scorers` and the suite that was run)"
        )
    head = (
        f"# Human labels for {scorer_id} on run {result.run_id}.\n"
        "# Replace every REVIEW with what a human reviewer concluded: passed | failed.\n"
        "# Delete rows you have not reviewed; a guessed label is worse than a missing one.\n"
        "# A leftover REVIEW is rejected on load, so a half-filled file cannot be scored.\n"
    )
    body = yaml.safe_dump(
        {"schema": "opseval/v1", "labels": rows}, sort_keys=False, allow_unicode=True
    )
    return head + body


def read_calibration(path: str | Path) -> CalibrationResult:
    return CalibrationResult.model_validate(json.loads(Path(path).read_text("utf-8")))


__all__ = [
    "calibrate_run",
    "calibration_summary",
    "label_template",
    "load_labels",
    "read_calibration",
    "save_calibration",
]
