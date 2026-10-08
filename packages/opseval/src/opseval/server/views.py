# SPDX-License-Identifier: Apache-2.0
"""Pure read functions: files on disk in, API shapes out. No HTTP in here."""

from __future__ import annotations

import os
import socket
from pathlib import Path
from statistics import fmean
from typing import Any

import yaml

from opseval.catalogue import load_catalogue
from opseval.config import Settings, config_digest
from opseval.errors import ConfigError
from opseval.models.profile import SutProfile
from opseval.models.results import SuiteResult, TestVerdict, VerdictStatus
from opseval.scoring import INVARIANTS, REGISTRY
from opseval.server import schemas as s
from opseval.storage import FileStore
from opseval.verdicts import EXIT_FAIL, EXIT_HARNESS

JUDGED_STATUSES = {VerdictStatus.PASSED, VerdictStatus.FAILED}


def gate_state(result: SuiteResult) -> s.GateState:
    """What a finished run means for the release. The one place this is decided.

    A harness error is never drawn as an agent failure, and an agent failure is never
    drawn as green: exit 1 under a blocking gate is ``blocked``, under a passing gate
    (only normal scenarios failed) it is ``gaps``.
    """
    if result.exit_code == EXIT_HARNESS:
        return "harness_error"
    if result.gate.status != "pass":
        # What blocked decides it: an agent failure (or a flaky scenario, when flaky
        # blocks) is "blocked"; only inconclusive or nothing-judged blocks are "not_judged".
        # The reasons are the strings verdicts.release_gate writes; the gate policy (next)
        # makes them structured.
        by_agent = any(": failed (" in b or b.endswith(": flaky") for b in result.gate.blocking)
        return "blocked" if by_agent else "not_judged"
    return "safe" if result.safe_to_release else "gaps"


def _counts(tests: list[TestVerdict]) -> tuple[int, int, int, int]:
    passed = sum(t.status is VerdictStatus.PASSED for t in tests)
    failed = sum(t.status is VerdictStatus.FAILED for t in tests)
    return passed, failed, len(tests) - passed - failed, sum(t.flaky for t in tests)


def summarise(result: SuiteResult) -> s.RunSummary:
    passed, failed, unjudged, flaky = _counts(result.tests)
    return s.RunSummary(
        id=result.run_id,
        suite=result.suite,
        started_at=result.started_at.isoformat(),
        state="finished",
        gate=gate_state(result),
        passed=passed,
        failed=failed,
        unjudged=unjudged,
        flaky=flaky,
    )


def _unfinished(store: FileStore, folder: Path) -> s.RunSummary | None:
    """A run folder without result.json: still running, or it could not be carried out."""
    m = store.load_manifest(folder.name)
    error = store.load_error(folder.name)
    if not m and error is None:
        return None  # not a run folder
    common = {
        "id": str(m.get("run_id", folder.name)),
        "suite": str(m.get("suite", "")),
        "started_at": m.get("started_at") or (error or {}).get("at"),
        "passed": None,  # not measured: never draw an unfinished run as zeros
        "failed": None,
        "unjudged": None,
        "flaky": None,
    }
    if error is None and _process_gone(m):
        error = {
            "code": "INTERRUPTED",
            "message": f"the process running it (pid {m.get('pid')}) exited before it finished",
        }
    if error is not None:
        return s.RunSummary(
            **common,
            state="harness_error",
            gate="harness_error",
            error=f"{error.get('code', 'HARNESS_ERROR')}: {error.get('message', '')}",
        )
    return s.RunSummary(**common, state="running", gate=None)


def _process_gone(manifest: dict[str, Any]) -> bool:
    """True only when we can tell: same host, and that process no longer exists.

    A run from another machine, or an older manifest without a pid, stays "running":
    guessing it dead could hide a run that is in fact still going.
    """
    pid, host = manifest.get("pid"), manifest.get("host")
    if os.name == "nt" or not isinstance(pid, int) or host != socket.gethostname():
        # On Windows os.kill(pid, 0) is TerminateProcess: the probe would kill the run.
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:  # exists but not ours (EPERM): alive
        return False
    return False


def load_results(store: FileStore) -> list[SuiteResult]:
    """Every finished run, newest first. A run without result.json is still in flight."""
    out: list[SuiteResult] = []
    for folder in store.root.glob("*/result.json"):
        try:
            out.append(store.load_result(folder.parent.name))
        except (ValueError, OSError):
            continue  # a half-written or foreign folder is not a run
    return sorted(out, key=lambda r: r.started_at, reverse=True)


def _in_flight(store: FileStore) -> list[s.RunSummary]:
    out: list[s.RunSummary] = []
    if not store.root.is_dir():
        return out
    for folder in store.root.iterdir():
        if not folder.is_dir() or (folder / "result.json").is_file():
            continue
        summary = _unfinished(store, folder)
        if summary is not None:
            out.append(summary)
    return sorted(out, key=lambda r: r.started_at or "", reverse=True)


def list_runs(store: FileStore) -> list[s.RunSummary]:
    return _in_flight(store) + [summarise(r) for r in load_results(store)]


class RunInProgress(Exception):
    """The run exists but has not finished: there is no detail to show yet."""


def _error_detail(store: FileStore, run_id: str) -> s.RunDetail | None:
    folder = store.run_dir(run_id)
    summary = _unfinished(store, folder) if folder.is_dir() else None
    if summary is None:
        return None  # no such run
    if summary.state == "running":
        raise RunInProgress(run_id)
    m = store.load_manifest(run_id)
    return s.RunDetail(
        summary=summary,
        target=str(m.get("target", "")),
        profile=str(m.get("profile", "")),
        gate_status="block",
        gate_summary=(
            "Not judged: Opseval could not carry out this run. This is a harness or "
            f"configuration problem, not an agent failure. {summary.error}"
        ),
        blocking=[],
        warnings=[],
        exit_code=EXIT_HARNESS,
        safe_to_release=False,
        tests=[],
        usage=[],
        load_errors=list(m.get("load_errors", [])),
    )


def run_detail(store: FileStore, run_id: str) -> s.RunDetail | None:
    """A finished run, or one that could not be carried out; ``None`` if there is no such run.

    Raises :class:`RunInProgress` while it is still running, so a caller can tell "wait"
    from "not found" (a console that waits on a typo waits for ever).
    """
    path = store.run_dir(run_id) / "result.json"
    if not path.is_file():
        return _error_detail(store, run_id)
    r = store.load_result(run_id)
    tests = [
        s.TestOut(
            id=t.test_id,
            name=t.name,
            criticality=t.criticality.value,
            status=t.status.value,
            reason=t.reason,
            pass_rate=t.pass_rate,
            flaky=t.flaky,
            unmeasured_scorers=t.unmeasured_scorers,
            attempts=[
                s.AttemptOut(
                    index=a.attempt_index,
                    passed=a.passed,
                    terminal_state=a.terminal_state.value,
                    invalid_reason=a.invalid_reason,
                    scores=[
                        s.ScoreOut(
                            scorer_id=x.scorer_id,
                            axis=x.axis.value,
                            status=x.status.value,
                            score=x.score,
                            threshold=x.threshold,
                            reason=x.reason,
                            degraded=x.degraded,
                            vacuous=x.details.get("vacuous") is True,
                        )
                        for x in a.scores
                    ],
                )
                for a in t.attempts
            ],
        )
        for t in r.tests
    ]
    return s.RunDetail(
        summary=summarise(r),
        target=r.target,
        profile=r.profile,
        gate_status=r.gate.status,
        gate_summary=r.gate.summary,
        blocking=r.gate.blocking,
        warnings=r.gate.warnings,
        exit_code=r.exit_code,
        safe_to_release=r.safe_to_release,
        tests=tests,
        usage=[
            s.UsageOut(
                role=u.role,
                model=u.model,
                calls=u.calls,
                input_tokens=u.input_tokens,
                output_tokens=u.output_tokens,
                cost_usd=u.cost_usd,
            )
            for u in r.usage
        ],
        load_errors=r.load_errors,
        degraded_scorers=r.degraded_scorers,
        vacuous_scorers=r.vacuous_scorers,
        unmeasured_scorers=r.unmeasured_scorers,
        sinks=[s.SinkOut(sink=o.sink, published=o.published, detail=o.detail) for o in r.sinks],
    )


def usage(store: FileStore) -> list[s.RunUsage]:
    rows: list[s.RunUsage] = []
    for r in load_results(store):
        if not r.usage:
            rows.append(
                s.RunUsage(run=r.run_id, simulator_tokens=None, judge_tokens=None, cost_usd=None)
            )
            continue

        def tokens(role: str, r: SuiteResult = r) -> int | None:
            hits = [u for u in r.usage if u.role == role]
            return sum(u.input_tokens + u.output_tokens for u in hits) if hits else None

        costs = [u.cost_usd for u in r.usage]
        total = None if any(c is None for c in costs) else round(sum(c for c in costs if c), 4)
        rows.append(
            s.RunUsage(
                run=r.run_id,
                simulator_tokens=tokens("simulator"),
                judge_tokens=tokens("judge"),
                cost_usd=total,
            )
        )
    return rows


def trend(store: FileStore) -> list[s.TrendPoint]:
    points = []
    for r in reversed(load_results(store)):
        passed, failed, _, _ = _counts(r.tests)
        judged = passed + failed
        points.append(
            s.TrendPoint(
                run=r.run_id,
                label=r.started_at.strftime("%b %-d %H:%M"),
                # A run that could not be fully judged (exit 2) or measured nothing (exit 3)
                # is a gap in the line, never a point that looks like a pass rate.
                pass_rate=passed / judged if judged and r.exit_code in (0, EXIT_FAIL) else None,
            )
        )
    return points


def _area_scores(r: SuiteResult) -> dict[str, float | None]:
    buckets: dict[str, list[float]] = {a: [] for a in ("output", "flow", "tools", "security")}
    for t in r.tests:
        for a in t.attempts:
            for x in a.scores:
                if x.score is not None and x.axis.value in buckets:
                    buckets[x.axis.value].append(x.score)
    return {k: (fmean(v) if v else None) for k, v in buckets.items()}


def compare(store: FileStore, base_id: str, cand_id: str) -> s.Comparison | None:
    for rid in (base_id, cand_id):
        if not (store.run_dir(rid) / "result.json").is_file():
            return None
    base, cand = store.load_result(base_id), store.load_result(cand_id)
    before = {t.test_id: t for t in base.tests}
    regressed, improved, unjudged = [], [], []
    for t in cand.tests:
        b = before.get(t.test_id)
        if b is None or b.status == t.status:
            continue
        change = s.Change(
            test_id=t.test_id, name=t.name, before=b.status.value, after=t.status.value
        )
        if t.status not in JUDGED_STATUSES or b.status not in JUDGED_STATUSES:
            unjudged.append(change)  # an outage is not a regression
        elif t.status is VerdictStatus.FAILED:
            regressed.append(change)
        else:
            improved.append(change)
    ba, ca = _area_scores(base), _area_scores(cand)
    return s.Comparison(
        baseline=base_id,
        candidate=cand_id,
        regressed=regressed,
        improved=improved,
        unjudged=unjudged,
        areas=[s.AreaDelta(area=a, before=ba[a], after=ca[a]) for a in ba],
    )


def checks() -> list[s.CheckOut]:
    return [
        s.CheckOut(
            id=sid,
            question=cls.description or sid,
            area=cls.axis.value,
            kind="judge" if sid.startswith("judge.") or cls.requires == "llm" else "deterministic",
            reads={"llm": "chat", "trace": "trace"}.get(cls.requires, "frames"),
        )
        for sid, cls in sorted(REGISTRY.items())
    ]


def settings_rows(settings: Settings) -> list[s.SettingRow]:
    rows = []
    for key, value in sorted(config_digest(settings).items()):
        redacted = value == "***redacted***"
        rows.append(
            s.SettingRow(
                key=f"opseval_{key}",
                value=None if redacted or value in (None, "") else str(value),
                redacted=redacted,
            )
        )
    return rows


def load_scenarios(catalogue: Path | None, profile: SutProfile | None) -> s.CatalogueOut:
    if catalogue is None or profile is None:
        return s.CatalogueOut(scenarios=[], problems=[])
    scenario_root = catalogue / "scenarios" if (catalogue / "scenarios").is_dir() else catalogue
    try:
        cat, _ = load_catalogue(
            scenario_root, profile, known_scorers=REGISTRY, known_invariants=set(INVARIANTS)
        )
    except ConfigError:
        return s.CatalogueOut(scenarios=[], problems=[])
    return s.CatalogueOut(
        scenarios=[
            s.ScenarioOut(
                id=sc.id,
                title=sc.name,
                difficulty=sc.difficulty.value,
                criticality=sc.criticality.value,
                stop_at=sc.conversation.stop_at,
                repeats=sc.execution.repeats,
                tags=sc.tags,
                allow_commit=sc.conversation.allow_commit,
                language=sc.persona.language,
                persona_role=sc.persona.role,
            )
            for sc in cat.scenarios
        ],
        problems=[s.Problem(path=p.path, field=p.field, message=p.message) for p in cat.problems],
    )


def list_suites(catalogue: Path | None, profile: SutProfile | None) -> list[s.SuiteOut]:
    if catalogue is None or profile is None:
        return []
    out = []
    for f in sorted((catalogue / "suites").glob("[!.]*.yaml")):  # dot-files are in-progress
        try:
            raw: Any = yaml.safe_load(f.read_text("utf-8")) or {}
            if not isinstance(raw, dict):
                raise ConfigError("a suite file must be a mapping")
            cat, _ = load_catalogue(
                f, profile, known_scorers=REGISTRY.keys(), known_invariants=set(INVARIANTS)
            )
            count = len(cat.scenarios)
        except (ConfigError, yaml.YAMLError, OSError) as exc:
            # One broken file is reported on its own row, never a 500 for the whole page.
            out.append(
                s.SuiteOut(
                    file=f.stem,
                    display_name=f.stem,
                    description="",
                    include=[],
                    scenario_count=0,
                    problem=str(exc)[:500],
                )
            )
            continue
        out.append(
            s.SuiteOut(
                file=f.stem,
                display_name=str(raw.get("name", f.stem)),
                description=str(raw.get("description", "")),
                include=list(raw.get("include", [])),
                scenario_count=count,
                repeats=_nested(raw, "overrides", "execution", "repeats"),
                stop_at=_nested(raw, "defaults", "conversation", "stop_at"),
            )
        )
    return out


def _nested(raw: dict[str, Any], *path: str) -> Any:
    cur: Any = raw
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def readiness(
    settings: Settings, catalogue: Path | None, profile: SutProfile | None
) -> list[s.ReadinessItem]:
    """What a run could and could not do right now. Computed once, here."""
    found = load_scenarios(catalogue, profile)
    return [
        s.ReadinessItem(
            id="catalogue",
            label="Catalogue",
            ready=bool(found.scenarios) and not found.problems,
            detail=(
                f"{len(found.scenarios)} scenarios load cleanly"
                if found.scenarios and not found.problems
                else f"{len(found.problems)} file(s) failed validation, or no scenarios found"
            ),
        ),
        s.ReadinessItem(
            id="simulator",
            label="Simulator model",
            ready=bool(settings.simulator_base_url and settings.simulator_model),
            detail="LLM personas can run"
            if settings.simulator_base_url and settings.simulator_model
            else "scripted scenarios only: set OPSEVAL_SIMULATOR_BASE_URL and _MODEL",
        ),
        s.ReadinessItem(
            id="judge",
            label="Judge model",
            ready=bool(settings.judge_base_url and settings.judge_model),
            detail="LLM judges can run"
            if settings.judge_base_url and settings.judge_model
            else "LLM judges are not measured: set OPSEVAL_JUDGE_BASE_URL and _MODEL",
        ),
        s.ReadinessItem(
            id="allowlist",
            label="Host allowlist",
            ready=bool(settings.allowlist()),
            detail="remote hosts allowed"
            if settings.allowlist()
            else "loopback targets only: set OPSEVAL_ALLOWED_HOSTS",
        ),
        s.ReadinessItem(
            id="tracing",
            label="Tool-call tracing",
            ready=bool(settings.langfuse_host and settings.langfuse_secret_key),
            detail="trace-backed Tools checks can run"
            if settings.langfuse_host and settings.langfuse_secret_key
            else "Tools checks fall back to frame evidence (degraded) or are not measured",
        ),
    ]
