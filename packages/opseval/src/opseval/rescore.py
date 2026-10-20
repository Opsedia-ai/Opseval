# SPDX-License-Identifier: Apache-2.0
"""Offline re-scoring of a finished run (``opseval rescore``).

Record first, judge later: the saved run records are scored again with the current
scorers and judges. Nothing talks to the agent or the simulator. The original run is
never modified; the new result, report and JUnit file go to ``<run>/rescore-<time>/``
with a :class:`RescoreInfo` marker that lists which scorer versions and judge prompts
changed and which verdicts moved.

Judges: with a judge configured they are re-run. Without one, a judge score is reused
from the original run only when its prompt hash is unchanged; otherwise it becomes
``infra_error`` (never a guess).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from opseval import __version__
from opseval.catalogue import load_catalogue, load_profile
from opseval.config import Settings, config_digest
from opseval.errors import ConfigError
from opseval.llm import UsageMeter, client_for_role
from opseval.models.gate import Preset
from opseval.models.gate import resolve as resolve_gate
from opseval.models.profile import SutProfile
from opseval.models.record import RunRecord
from opseval.models.results import (
    AttemptVerdict,
    RescoreInfo,
    ScorerChange,
    ScoreResult,
    SuiteResult,
    TestVerdict,
    VerdictChange,
)
from opseval.models.scenario import Scenario, ScorerSpec
from opseval.reporting import render_html
from opseval.reporting.junit import render_junit
from opseval.scoring import INVARIANTS, REGISTRY, ScoringEngine, effective_specs
from opseval.scoring.base import Scorer, check_judge_independence, infra_error
from opseval.storage import FileStore
from opseval.verdicts import (
    attempt_verdict,
    exit_code,
    fold_attempts,
    fold_cut_short,
    release_gate,
)


@dataclass
class RescoreOutcome:
    result: SuiteResult
    info: RescoreInfo
    out_dir: Path


def _scenarios(
    store: FileStore,
    run_id: str,
    catalogue: str | Path | None,
    profile_path: str | Path | None,
) -> tuple[SutProfile, dict[str, Scenario], str]:
    if catalogue is None and profile_path is None:
        # Read only when used: a snapshot that no longer loads must not block the
        # --catalogue/--profile route, which is the way around it.
        snapshot = store.load_snapshot(run_id)
        if snapshot is None:
            raise ConfigError(
                "this run has no saved scenarios (it predates re-scoring support); pass "
                "--catalogue and --profile to score it against the current files"
            )
        return snapshot[0], snapshot[1], "snapshot"
    if catalogue is None or profile_path is None:
        raise ConfigError("pass both --catalogue and --profile, or neither (use the snapshot)")
    profile = load_profile(profile_path)
    cat, _ = load_catalogue(
        catalogue, profile, known_scorers=REGISTRY.keys(), known_invariants=set(INVARIANTS)
    )
    if cat.problems:
        raise ConfigError("the catalogue has problems: " + "; ".join(str(p) for p in cat.problems))
    return profile, {s.id: s for s in cat.scenarios}, "catalogue"


def _versions(scores: list[ScoreResult]) -> dict[tuple[str, str], str | None]:
    out: dict[tuple[str, str], str | None] = {}
    for s in scores:
        out[(s.scorer_id, "version")] = s.scorer_version
        if "prompt_hash" in s.details:
            out[(s.scorer_id, "prompt_hash")] = str(s.details["prompt_hash"])
        if "model" in s.details:
            out[(s.scorer_id, "model")] = str(s.details["model"])
    return out


def _changes(before: list[ScoreResult], after: list[ScoreResult]) -> list[ScorerChange]:
    old, new = _versions(before), _versions(after)
    changes = []
    for key in sorted(set(old) | set(new)):
        if key in old and key in new and old[key] != new[key]:
            changes.append(
                ScorerChange(scorer_id=key[0], what=key[1], before=old[key], after=new[key])
            )
    return changes


class _Rescorer:
    def __init__(
        self, engine: ScoringEngine, judge_configured: bool, scenario_source: str = "catalogue"
    ) -> None:
        self.engine = engine
        self.judge_configured = judge_configured
        self.scenario_source = scenario_source
        self.reused = 0

    async def score(
        self, record: RunRecord, scenario: Scenario, profile: SutProfile, original: AttemptVerdict
    ) -> list[ScoreResult]:
        scores = await self.engine.score(record, scenario, profile)
        if self.judge_configured:
            return scores
        previous = {s.scorer_id: s for s in original.scores}
        specs = {spec.id: spec for spec in effective_specs(scenario)}
        # Always-on harness checks (ALWAYS_ON) have no spec: they are deterministic, so the
        # fresh score is the one to keep.
        return [
            self._reuse(s, previous.get(s.scorer_id), spec)
            if (spec := specs.get(s.scorer_id)) is not None
            else s
            for s in scores
        ]

    def _reuse(self, fresh: ScoreResult, old: ScoreResult | None, spec: ScorerSpec) -> ScoreResult:
        cls = REGISTRY.get(fresh.scorer_id)
        if cls is None or cls.requires != "llm":
            return fresh
        current_hash = getattr(cls, "prompt_hash", lambda: None)()
        if old is None:
            return self._unavailable(fresh, spec, cls, "it was never scored")
        if old.details.get("prompt_hash") != current_hash:
            return self._unavailable(fresh, spec, cls, "the judge prompt changed")
        # prompt_hash is a class property, so it cannot see scenario-supplied criteria
        # (the goal, the persona language, the policy rules). The rubric digest does.
        #
        # Only worth checking when the scenarios came from --catalogue. Against the run's
        # own snapshot the scenario *is* the one that produced the score, so the rubric
        # cannot have changed, and comparing digests there can only refuse a score for a
        # harness-version reason - which would drop every pre-upgrade judge out of the
        # measured set and let a mixed suite report green with its judges gone.
        if self.scenario_source == "catalogue":
            old_rubric = old.details.get("rubric_hash")
            if old_rubric != fresh.details.get("rubric_hash"):
                why = (
                    "it predates rubric digests, so the scenario it was graded against "
                    "cannot be verified"
                    if old_rubric is None
                    else "the scenario data it grades against changed"
                )
                return self._unavailable(fresh, spec, cls, why)
        self.reused += 1
        return old.model_copy(update={"details": {**old.details, "reused": True}})

    def _unavailable(
        self, fresh: ScoreResult, spec: ScorerSpec, cls: type[Scorer], why: str
    ) -> ScoreResult:
        return infra_error(
            fresh.scorer_id,
            fresh.axis,
            spec,
            f"JUDGE_UNAVAILABLE: {why}, and no judge is configured to re-run it",
            version=cls.version,
        )


async def rescore_run(
    run_dir: str | Path,
    settings: Settings | None = None,
    *,
    catalogue: str | Path | None = None,
    profile: str | Path | None = None,
    gate: Preset | None = None,
    log: Callable[[str], None] = lambda _msg: None,
) -> RescoreOutcome:
    """Re-score ``run_dir`` offline and write the result next to it (never over it).

    The new result is gated with the run's own policy (ADR-0032), or with ``gate``, a
    preset, to see what a stricter or looser gate would have said about the same run.
    """
    settings = settings or Settings()
    folder = Path(run_dir)
    if not (folder / "result.json").is_file():
        raise ConfigError(f"{folder}: not a finished run (no result.json)")
    store, run_id = FileStore(folder.parent), folder.name
    original = store.load_result(run_id)
    prof, scenarios, source = _scenarios(store, run_id, catalogue, profile)
    records = {(r.test_id, r.attempt_index): r for r in store.load_records(run_id)}

    meter = UsageMeter()
    judge = client_for_role(settings, "judge", meter)
    # The simulator the run actually used, not whatever is configured now. Runs recorded
    # before the key existed fall back to the config digest, which over-reports (it holds
    # OPSEVAL_SIMULATOR_MODEL even when no simulator client was built) but keeps the check
    # live on old runs instead of silently inert.
    manifest = store.load_manifest(run_id)
    if "simulator_model" in manifest:
        recorded = manifest["simulator_model"]
    else:
        config = manifest.get("config")
        recorded = config.get("simulator_model") if isinstance(config, dict) else None
    sim_model = recorded if isinstance(recorded, str) else None
    check_judge_independence(
        scenarios.values(),
        prof,
        judge_model=judge.model if judge else None,
        simulator_model=sim_model,
        mode=settings.judge_independence,
        log=log,
    )
    rescorer = _Rescorer(
        ScoringEngine(
            judge=judge,
            judge_concurrency=settings.judge_concurrency,
            simulator_model=sim_model,
            judge_independence=settings.judge_independence,
        ),
        judge is not None,
        source,
    )
    tests: list[TestVerdict] = []
    before_scores: list[ScoreResult] = []
    after_scores: list[ScoreResult] = []
    verdict_changes: list[VerdictChange] = []
    for old in original.tests:
        scenario = scenarios.get(old.test_id)
        if not old.attempts or scenario is None:
            if old.attempts:
                log(f"  {old.test_id}: not in the catalogue; keeping the original verdict")
            tests.append(old)  # skipped/blocked tests have nothing to re-score
            continue
        # Fold over the attempts that actually ran (a --repeats override or an edited
        # catalogue must not turn a judged run into "inconclusive"). A scenario a cancel cut
        # short is folded against the repeats it planned, by the same rule as the run.
        planned = old.cut_short_of or len(old.attempts)
        scenario = scenario.model_copy(
            update={"execution": scenario.execution.model_copy(update={"repeats": planned})}
        )
        attempts = []
        for a in old.attempts:
            record = records.get((old.test_id, a.attempt_index))
            if record is None:
                raise ConfigError(f"record missing for {old.test_id} attempt {a.attempt_index}")
            scores = await rescorer.score(record, scenario, prof, a)
            before_scores += a.scores
            after_scores += scores
            attempts.append(attempt_verdict(record, scores, a.record_path))
        new = (
            fold_cut_short(scenario, attempts)
            if old.cut_short_of
            else fold_attempts(scenario, attempts)
        )
        tests.append(new)
        if new.status is not old.status:
            verdict_changes.append(
                VerdictChange(test_id=old.test_id, before=old.status, after=new.status)
            )
            log(f"  {old.test_id}: {old.status.value} → {new.status.value}")

    now = datetime.now(UTC)
    info = RescoreInfo(
        of_run_id=original.run_id,
        rescored_at=now,
        scenario_source=source,
        scorer_changes=_changes(before_scores, after_scores),
        verdict_changes=verdict_changes,
        reused_judge_scores=rescorer.reused,
    )
    if gate is not None:
        policy = resolve_gate(preset=gate, fail_on_flaky=settings.gate_fail_on_flaky)
    elif original.gate.policy is not None:
        policy = original.gate.policy
    else:  # a run from before gate policies was gated by the standard rules
        policy = resolve_gate(fail_on_flaky=settings.gate_fail_on_flaky)
    new_gate = release_gate(tests, policy=policy, load_errors=len(original.load_errors))
    result = SuiteResult(
        opseval_version=__version__,
        run_id=f"{original.run_id}.rescore-{now:%Y%m%dT%H%M%SZ}",
        suite=original.suite,
        target=original.target,
        profile=prof.name,
        started_at=now,
        ended_at=datetime.now(UTC),
        tests=tests,
        gate=new_gate,
        exit_code=exit_code(tests, load_errors=len(original.load_errors), gate=new_gate),
        usage=meter.snapshot(),
        config_digest=config_digest(settings),
        load_errors=original.load_errors,
        rescore=info,
        # A re-score changes no scenario that was not run: why the run stopped still holds.
        stop=original.stop,
        stopped=original.stopped,
    )
    out = store.save_rescore(
        run_id,
        f"{now:%Y%m%dT%H%M%S%fZ}",
        result,
        render_html(result, list(records.values()), prof),
        render_junit(result),
    )
    return RescoreOutcome(result=result, info=info, out_dir=out)


__all__ = ["RescoreOutcome", "rescore_run"]
