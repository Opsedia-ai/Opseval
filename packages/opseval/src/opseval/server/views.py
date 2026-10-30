# SPDX-License-Identifier: Apache-2.0
"""Pure read functions: the run store in, API shapes out. No HTTP in here, and no path: a run
is read through :class:`~opseval.storage.RunStore` only, whichever store it is (ADR-0051)."""

from __future__ import annotations

from pathlib import Path
from statistics import fmean
from typing import Any

import yaml

from opseval.analytics import AnalyticsStore, UsageFilter
from opseval.catalogue import load_catalogue
from opseval.config import Settings, check_url_allowed, config_digest
from opseval.errors import ConfigError
from opseval.models.gate import (
    PRESETS,
    SETTINGS,
    GatePolicy,
    choices,
    describe,
    flatten,
    looser_than_standard,
    overrides,
    resolve,
    to_yaml_block,
)
from opseval.models.profile import SutProfile
from opseval.models.record import RunRecord
from opseval.models.results import ModelUsage, SuiteResult, TestVerdict, VerdictStatus
from opseval.models.stop import StopPolicy
from opseval.redaction import redact_text
from opseval.scoring import INVARIANTS, REGISTRY
from opseval.server import schemas as s
from opseval.storage import PRE_USAGE_LAYOUTS, RunStore, process_gone
from opseval.transcript import connection, short, transcript
from opseval.verdicts import EXIT_FAIL, EXIT_HARNESS, gate_state

JUDGED_STATUSES = {VerdictStatus.PASSED, VerdictStatus.FAILED}


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


def _unfinished(store: RunStore, run_id: str) -> s.RunSummary | None:
    """A run without a result: still running, or it could not be carried out."""
    m = store.load_manifest(run_id)
    error = store.load_error(run_id)
    if not m and error is None:
        return None  # not a run folder
    if not store.layout_known(run_id):
        # Written by a newer Opseval: shown as an error the reader can act on (upgrade),
        # never read with rules that may not apply to it, and never silently dropped.
        error = {
            "code": "UNKNOWN_LAYOUT",
            "message": f"layout {store.layout(run_id)!r} "
            "was written by a newer Opseval; upgrade to read this run",
        }
    common = {
        "id": str(m.get("run_id", run_id)),
        "suite": str(m.get("suite", "")),
        "started_at": m.get("started_at") or (error or {}).get("at"),
        "passed": None,  # not measured: never draw an unfinished run as zeros
        "failed": None,
        "unjudged": None,
        "flaky": None,
    }
    if error is None and process_gone(m):
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
    return s.RunSummary(**common, state="running", gate=None, progress=_progress(store, run_id))


def _progress(store: RunStore, run_id: str) -> s.RunProgress | None:
    """The runner's own account of how far it got (progress.json), or ``None``."""
    p = store.load_progress(run_id)
    if p is None:
        return None
    rows = [
        s.ScenarioProgressOut(id=r.id, state=r.state, attempts=r.attempts, repeats=r.repeats)
        for r in p.scenarios
    ]
    return s.RunProgress(
        stage=p.stage,
        scenarios_planned=len(rows),
        scenarios_done=sum(r.state in ("done", "skipped") for r in rows),
        attempts_recorded=sum(r.attempts for r in rows),
        current=p.current,
        scenarios=rows,
        cancel_requested=store.cancel_requested(run_id),
    )


def load_results(store: RunStore) -> list[SuiteResult]:
    """Every finished run, newest first. A run without result.json is still in flight."""
    return store.finished_results()


def _in_flight(store: RunStore) -> list[s.RunSummary]:
    out: list[s.RunSummary] = []
    for run_id in store.run_ids():
        if store.has_result(run_id) and store.layout_known(run_id):
            continue
        summary = _unfinished(store, run_id)
        if summary is not None:
            out.append(summary)
    return sorted(out, key=lambda r: r.started_at or "", reverse=True)


def list_runs(store: RunStore) -> list[s.RunSummary]:
    return _in_flight(store) + [summarise(r) for r in load_results(store)]


class RunInProgress(Exception):
    """The run exists but has not finished: there is no detail yet, only its progress."""

    def __init__(self, run_id: str, progress: s.RunProgress | None) -> None:
        super().__init__(run_id)
        self.progress = progress


def _error_detail(store: RunStore, run_id: str) -> s.RunDetail | None:
    summary = _unfinished(store, run_id)
    if summary is None:
        return None  # no such run
    if summary.state == "running":
        raise RunInProgress(run_id, summary.progress)
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


def run_detail(store: RunStore, run_id: str) -> s.RunDetail | None:
    """A finished run, or one that could not be carried out; ``None`` if there is no such run.

    Raises :class:`RunInProgress` while it is still running, so a caller can tell "wait"
    from "not found" (a console that waits on a typo waits for ever).
    """
    if not store.has_result(run_id) or not store.layout_known(run_id):
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
            skip_kind=t.skip_kind,
            cut_short_of=t.cut_short_of,
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
        gate_policy=describe(r.gate.policy) if r.gate.policy else None,
        looser_than_standard=looser_than_standard(r.gate.policy) if r.gate.policy else [],
        ignored=r.gate.ignored,
        tests=tests,
        usage=[
            s.UsageOut(
                role=u.role,
                model=u.model,
                calls=u.calls,
                input_tokens=u.input_tokens,
                output_tokens=u.output_tokens,
                cost_usd=u.cost_usd,
                audio_in_seconds=u.audio_in_seconds,
                audio_out_seconds=u.audio_out_seconds,
                characters=u.characters,
                reasoning_tokens=u.reasoning_tokens,
                cached_input_tokens=u.cached_input_tokens,
                failed_calls=u.failed_calls,
                reported=s.UsageReportedOut.model_validate(u.reported.model_dump())
                if u.reported
                else None,
            )
            for u in r.usage
        ],
        load_errors=r.load_errors,
        degraded_scorers=r.degraded_scorers,
        vacuous_scorers=r.vacuous_scorers,
        unmeasured_scorers=r.unmeasured_scorers,
        sinks=[s.SinkOut(sink=o.sink, published=o.published, detail=o.detail) for o in r.sinks],
        stop_policy=stop_out(r.stop) if r.stop else None,
        stopped=(
            s.StoppedOut(by=r.stopped.by, after=r.stopped.after, detail=r.stopped.detail)
            if r.stopped
            else None
        ),
    )


def _total(values: list[float | None]) -> float | None:
    """The sum of what was measured; ``None`` when nothing was (never 0)."""
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def _count(values: list[int | None]) -> int | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def usage(store: RunStore) -> list[s.RunUsage]:
    """Per run: tokens per LLM role, speech audio and characters, and the cost. The cost is
    what the priced models cost, with ``cost_complete`` saying whether every model was priced:
    one unpriced model makes the total a floor, never hides the part that is known. Token
    totals carry the same flag (``*_tokens_complete``). Costs are the ones stored with each
    call when it was made, as on the dashboards (ADR-0066)."""
    rows: list[s.RunUsage] = []
    for r in load_results(store):

        def tokens(role: str, r: SuiteResult = r) -> tuple[int | None, bool]:
            """The role's tokens over the models that reported them, and whether every model
            did (a model whose calls did not all report them is left out: a floor)."""
            counts = [_tokens(u) for u in r.usage if u.role == role]
            return _count(counts), bool(counts) and None not in counts

        costs = [u.cost_usd for u in r.usage]
        known = _total(costs)
        simulator, judge = tokens("simulator"), tokens("judge")
        rows.append(
            s.RunUsage(
                run=r.run_id,
                simulator_tokens=simulator[0],
                simulator_tokens_complete=simulator[1],
                judge_tokens=judge[0],
                judge_tokens_complete=judge[1],
                cost_usd=round(known, 4) if known is not None else None,
                cost_complete=bool(costs) and all(c is not None for c in costs),
                audio_in_seconds=_total([u.audio_in_seconds for u in r.usage]),
                audio_out_seconds=_total([u.audio_out_seconds for u in r.usage]),
                characters=_count([u.characters for u in r.usage]),
                per_call=store.layout(r.run_id) not in PRE_USAGE_LAYOUTS,
            )
        )
    return rows


def _tokens(u: ModelUsage) -> int | None:
    """A row's input plus output tokens; ``None`` unless both are known (a missing count is
    never added as 0, ADR-0066)."""
    if u.input_tokens is None or u.output_tokens is None:
        return None
    return u.input_tokens + u.output_tokens


def cost_dashboard(analytics: AnalyticsStore, filters: UsageFilter) -> s.CostDashboard:
    """The Cost dashboard (ADR-0066): every number from the analytics store."""
    return s.CostDashboard.model_validate(analytics.cost(filters).model_dump(mode="json"))


def tokens_dashboard(analytics: AnalyticsStore, filters: UsageFilter) -> s.TokensDashboard:
    """The Tokens dashboard (ADR-0066)."""
    return s.TokensDashboard.model_validate(analytics.tokens(filters).model_dump(mode="json"))


def trend(store: RunStore) -> list[s.TrendPoint]:
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


def scenario_trend(store: RunStore, *, suite: str | None, limit: int) -> s.ScenarioTrend:
    """Every scenario's verdict across the last ``limit`` finished runs (of one suite).

    Computed here, never in the browser (ADR-0029). A scenario that was not in a run has an
    empty cell, which is not a skip and not a failure; ``flips`` counts only changes between
    judged verdicts of the same suite, so an outage between two passes is not instability,
    and neither is a scenario that one suite passes and another fails.
    """
    results = load_results(store)
    suites = sorted({r.suite for r in results})
    chosen = [r for r in results if suite is None or r.suite == suite][:limit]
    chosen.reverse()  # oldest first, left to right
    by_run = [{t.test_id: t for t in r.tests} for r in chosen]
    names = {tid: t.name for tests in by_run for tid, t in tests.items()}  # newest name wins
    rows = []
    for test_id in sorted(names):
        cells = []
        for r, tests in zip(chosen, by_run, strict=True):
            found = tests.get(test_id)
            cells.append(
                s.TrendCell(
                    run=r.run_id,
                    status=found.status if found else None,
                    pass_rate=found.pass_rate if found else None,
                    flaky=found.flaky if found else False,
                )
            )
        # Within one suite only: two suites can run a scenario differently (repeats,
        # thresholds), so alternating between them is not the scenario changing.
        flips = 0
        for name in {r.suite for r in chosen}:
            judged = [
                c.status
                for c, r in zip(cells, chosen, strict=True)
                if r.suite == name and c.status in JUDGED_STATUSES
            ]
            flips += sum(1 for a, b in zip(judged, judged[1:], strict=False) if a != b)
        rows.append(s.ScenarioTrendRow(id=test_id, name=names[test_id], cells=cells, flips=flips))
    runs = [
        s.TrendRun(run=r.run_id, label=r.started_at.strftime("%b %-d %H:%M"), suite=r.suite)
        for r in chosen
    ]
    return s.ScenarioTrend(suites=suites, runs=runs, scenarios=rows)


def _area_scores(r: SuiteResult) -> dict[str, float | None]:
    buckets: dict[str, list[float]] = {a: [] for a in ("output", "flow", "tools", "security")}
    for t in r.tests:
        for a in t.attempts:
            for x in a.scores:
                if x.score is not None and x.axis.value in buckets:
                    buckets[x.axis.value].append(x.score)
    return {k: (fmean(v) if v else None) for k, v in buckets.items()}


def compare(store: RunStore, base_id: str, cand_id: str) -> s.Comparison | None:
    for rid in (base_id, cand_id):
        if not store.has_result(rid) or not store.layout_known(rid):
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
            scenario_root,
            profile,
            known_scorers=REGISTRY,
            known_invariants=set(INVARIANTS),
            root=catalogue,  # where datasets/ is, whatever the folder is called (ADR-0060)
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
                file=_relative(catalogue, cat.sources[sc.id]),
            )
            for sc in cat.scenarios
        ],
        # Catalogue-relative, so the console can open a broken file in the editor.
        problems=[
            s.Problem(path=_relative(catalogue, p.path), field=p.field, message=p.message)
            for p in cat.problems
        ],
    )


def _relative(root: Path, path: str) -> str:
    resolved, base = Path(path).resolve(), root.resolve()
    return resolved.relative_to(base).as_posix() if resolved.is_relative_to(base) else path


def list_suites(
    catalogue: Path | None, profile: SutProfile | None, *, fail_on_flaky: bool = False
) -> list[s.SuiteOut]:
    if catalogue is None or profile is None:
        return []
    out = []
    for f in sorted((catalogue / "suites").glob("[!.]*.yaml")):  # dot-files are in-progress
        try:
            raw: Any = yaml.safe_load(f.read_text("utf-8")) or {}
            if not isinstance(raw, dict):
                raise ConfigError("a suite file must be a mapping")
            cat, _ = load_catalogue(
                f,
                profile,
                known_scorers=REGISTRY,
                known_invariants=set(INVARIANTS),
                root=catalogue,
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
                # load_catalogue validated the file, so its gate block is well-formed here.
                gate=gate_out(
                    GatePolicy.model_validate(raw.get("gate") or {}), fail_on_flaky=fail_on_flaky
                ),
                stop=stop_out(StopPolicy.model_validate(raw["stop"])) if raw.get("stop") else None,
            )
        )
    return out


def stop_out(policy: StopPolicy) -> s.StopOut:
    return s.StopOut(**policy.model_dump())


def gate_out(policy: GatePolicy, *, fail_on_flaky: bool = False) -> s.GateOut:
    """A gate block as the console shows it, resolved exactly as a run would resolve it
    (``OPSEVAL_GATE_FAIL_ON_FLAKY`` included). Resolution happens here, never in the browser.
    """
    resolved = resolve(policy, fail_on_flaky=fail_on_flaky)
    block = to_yaml_block(policy)
    return s.GateOut(
        preset=policy.preset,
        overrides=overrides(policy),
        effective=flatten(resolved),
        describe=describe(resolved),
        looser_than_standard=looser_than_standard(resolved),
        yaml="" if block is None else yaml.safe_dump({"gate": block}, sort_keys=False),
    )


def gate_catalog() -> s.GateCatalogOut:
    """Every gate setting with the values it accepts, and every preset, for the editor."""
    return s.GateCatalogOut(
        settings=[
            s.GateSettingOut(key=key, label=label, help=text, choices=list(choices(key)))
            for key, label, text in SETTINGS
        ],
        presets={name: flatten(p) for name, p in PRESETS.items()},
    )


def _nested(raw: dict[str, Any], *path: str) -> Any:
    cur: Any = raw
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _endpoint(
    item: str, label: str, settings: Settings, url: str | None, ready: str, unset: str
) -> s.ReadinessItem:
    """Ready only when configured *and* allowed: a run would refuse the host otherwise."""
    if not url:
        return s.ReadinessItem(id=item, label=label, ready=False, detail=unset)
    try:
        check_url_allowed(url, settings.allowlist(), secure_schemes=("https",))
    except ConfigError as exc:
        return s.ReadinessItem(id=item, label=label, ready=False, detail=f"refused: {exc}")
    return s.ReadinessItem(id=item, label=label, ready=True, detail=ready)


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
        _endpoint(
            "simulator",
            "Simulator model",
            settings,
            settings.simulator_base_url if settings.simulator_model else None,
            "LLM personas can run",
            "scripted scenarios only: set OPSEVAL_SIMULATOR_BASE_URL and _MODEL",
        ),
        _endpoint(
            "judge",
            "Judge model",
            settings,
            settings.judge_base_url if settings.judge_model else None,
            "LLM judges can run",
            "LLM judges are not measured: set OPSEVAL_JUDGE_BASE_URL and _MODEL",
        ),
        s.ReadinessItem(
            id="allowlist",
            label="Host allowlist",
            ready=bool(settings.allowlist()),
            detail="hosts allowed: " + ", ".join(sorted(settings.allowlist()))
            if settings.allowlist()
            else "no host may be contacted, loopback included: set OPSEVAL_ALLOWED_HOSTS",
        ),
        _endpoint(
            "tracing",
            "Tool-call tracing",
            settings,
            settings.langfuse_host if settings.langfuse_secret_key else None,
            "trace-backed Tools checks can run",
            "Tools checks fall back to frame evidence (degraded) or are not measured",
        ),
    ]


class RecordUnreadable(Exception):
    """A record exists but cannot be read: a harness problem, never "no such attempt"."""


def _redacted(text: str | None) -> str | None:
    # Records written before the driver redacted exception text carry it raw.
    return None if text is None else redact_text(text)[0]


def attempt_record(
    store: RunStore, run_id: str, test_id: str, index: int, *, record: RunRecord | None = None
) -> s.AttemptRecordOut | None:
    """One attempt's conversation; ``None`` if that attempt has no record (yet). ``record``
    is the attempt's record when the caller has read it already: it is not read again."""
    if record is None:
        try:
            record = store.load_record(run_id, test_id, index)
        except (OSError, ValueError) as exc:
            raise RecordUnreadable(f"{test_id} attempt {index}: {type(exc).__name__}") from exc
    if record is None:
        return None
    persona = record.persona or {}
    return s.AttemptRecordOut(
        test_id=record.test_id,
        index=record.attempt_index,
        terminal_state=record.terminal_state.value,
        stop_reason=_redacted(record.stop_reason),
        started_at=record.started_at.isoformat(),
        ended_at=record.ended_at.isoformat(),
        language=persona.get("language"),
        dialect=persona.get("dialect"),
        user_turns=record.user_turns,
        trace_coverage=record.trace_coverage,
        entries=[
            s.TranscriptEntry(
                seq=e.seq,
                kind=e.kind,
                who=e.who,
                text=e.text,
                phase=e.phase,
                latency_ms=e.latency_ms,
                detail=e.detail,
            )
            for e in transcript(record)
        ],
        tool_calls=[
            s.ToolCallOut(
                name=c.name,
                ok=c.ok,
                arguments=None if c.arguments is None else short(c.arguments, 2000),
            )
            for c in record.tool_calls
        ],
        infra_errors=[_redacted(e) or "" for e in record.infra_errors],
        connection=connection(record),
        probes=[
            f"{p.name}: passed" if p.passed else f"{p.name}: failed ({p.reason or 'no reason'})"
            for p in record.probes
        ],
        planted_injections=list(record.planted_injections),
        redaction_checked=record.redaction.applied,
        redacted_kinds=list(record.redaction.found),
        created_artifacts=[
            s.ArtifactOut(
                kind=a.kind,
                id="" if a.id_redacted else a.id,
                seq=a.seq,
                id_redacted=a.id_redacted,
                id_truncated=a.id_truncated,
            )
            for a in record.created_artifacts
        ],
        artifacts_checked=record.artifacts_checked,
    )
