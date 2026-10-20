# SPDX-License-Identifier: Apache-2.0
"""One launch pipeline for the CLI (and, later, the server)."""

from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from opseval import __version__
from opseval.auth import HS256Tokens, TokenSource
from opseval.catalogue import load_catalogue, load_profile
from opseval.config import Settings, check_url_allowed, config_digest
from opseval.demo import catalogue_dir as demo_catalogue_dir
from opseval.driver import RATE_LIMITED_NOTE, Driver
from opseval.errors import ConfigError
from opseval.llm import UsageMeter, client_for_role
from opseval.models.gate import Preset, ResolvedGatePolicy
from opseval.models.gate import resolve as resolve_gate
from opseval.models.preflight import PreflightReport
from opseval.models.profile import SutProfile
from opseval.models.results import (
    SkipKind,
    StopCause,
    StoppedEarly,
    SuiteResult,
    TestVerdict,
    VerdictStatus,
)
from opseval.models.scenario import Criticality, Scenario
from opseval.models.stop import StopPolicy
from opseval.models.telemetry import TelemetryEvent
from opseval.models.tenant import Tenant, TenantPool, lane_count
from opseval.plugins import load_scorers
from opseval.preflight import llm_roles_needed, preflight
from opseval.progress import ProgressLog
from opseval.reporting import render_html
from opseval.reporting.junit import render_junit
from opseval.scoring import INVARIANTS, REGISTRY, ScoringEngine, effective_specs
from opseval.scoring.base import check_judge_independence
from opseval.simulator import LLMSimulator, ScriptedSimulator, Simulator, attempt_seed
from opseval.sinks import build_sinks, publish_all
from opseval.storage import FileStore
from opseval.telemetry import report as report_telemetry
from opseval.tenants import TenantLeases, default_lock_dir, load_pool, refreshing, token_for
from opseval.traces import TraceSource, enrich
from opseval.traces.langfuse import LangfuseTraceSource
from opseval.traces.otlp import OtlpFileTraceSource
from opseval.transport import (
    HttpJsonTransport,
    OpenAIChatTransport,
    SseTransport,
    Transport,
    WebSocketTransport,
)
from opseval.verdicts import (
    attempt_verdict,
    exit_code,
    failure_blocks,
    fold_attempts,
    fold_cut_short,
    release_gate,
    skipped,
)

TransportFactory = Callable[[SutProfile], Transport]


def new_run_id(started: datetime | None = None) -> str:
    """A sortable, unique run id: UTC start time plus a random suffix."""
    started = started or datetime.now(UTC)
    return f"{started:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


@dataclass
class RunOptions:
    target: str | None = None
    transport: str = "websocket"
    profile: str | Path | None = None
    repeats: int | None = None
    allow_commit: bool = False
    fail_fast: bool = False
    """Shorthand for ``stop.fail_fast`` (kept for callers that set only this)."""
    stop: StopPolicy | None = None
    """Stop-policy fields that replace the suite's (``opseval run`` flags, the console)."""
    filters: list[str] = field(default_factory=list)
    traces: bool = True
    """Read the agent's traces when the profile declares a trace source."""
    transport_factory: TransportFactory | None = None
    trace_source: TraceSource | None = None
    """Inject a trace source (tests); otherwise one is built from settings."""
    preflight: bool = True
    """Check dependencies before running, so an outage skips instead of failing."""
    verify_llm: bool = False
    """Spend one token per LLM role proving the credentials actually work."""
    run_id: str | None = None
    """Use this run id instead of minting one, so a caller (the console server) can point
    at the run before it starts and record a failure against it."""
    gate: Preset | None = None
    """A gate preset that replaces the suite's policy whole (``opseval run --gate``)."""
    only_ids: list[str] | None = None
    """Run only these scenario ids (any of them, unlike ``filters``, which must all hold).
    An id that is not in the catalogue is a configuration error, never silently dropped."""


class _TransportCtor(Protocol):
    """What every network transport's constructor takes."""

    def __call__(
        self, target: str, profile: SutProfile, /, *, token: str | TokenSource | None = None
    ) -> Transport: ...


TRANSPORTS: dict[str, tuple[_TransportCtor, tuple[str, ...]]] = {
    "websocket": (WebSocketTransport, ("wss",)),
    "http-json": (HttpJsonTransport, ("https",)),
    "openai-chat": (OpenAIChatTransport, ("https",)),
    "sse": (SseTransport, ("https",)),
}
"""Transport class and the schemes that count as secure for it: one table, so a tenant's own
target is checked exactly like the default one."""

TenantTransportFactory = Callable[[SutProfile, "Tenant | None"], Transport]
"""Builds the transport for one attempt. With a pool, the tenant supplies its own target and
credentials (ADR-0041)."""


def build_transport_factory(options: RunOptions, settings: Settings) -> TenantTransportFactory:
    if options.transport_factory is not None:
        injected = options.transport_factory
        return lambda profile, _tenant: injected(profile)
    if not options.target:
        raise ConfigError("no target: pass --target (e.g. ws://localhost:8765)")
    default_target = options.target
    token = settings.static_token.get_secret_value() if settings.static_token else None
    signing_key = settings.token_signing_key

    def credentials(profile: SutProfile, tenant: Tenant | None) -> str | TokenSource | None:
        """A fresh token source per attempt: HS256 when the profile asks for it.

        A tenant's own token wins over OPSEVAL_STATIC_TOKEN, because the whole point of
        the pool is that each tenant authenticates as itself.
        """
        own = token_for(tenant, settings) if tenant else None
        if own:
            return own
        if profile.token is None:
            return token
        if signing_key is None:
            raise ConfigError(
                f"profile {profile.name!r} mints HS256 tokens: set OPSEVAL_TOKEN_SIGNING_KEY"
            )
        return HS256Tokens(
            signing_key.get_secret_value(),
            ttl_seconds=profile.token.ttl_seconds,
            refresh_before_seconds=profile.token.refresh_before_seconds,
            claims=profile.token.claims,
        )

    try:
        cls, secure = TRANSPORTS[options.transport]
    except KeyError:
        raise ConfigError(
            f"unknown transport {options.transport!r}; use {', '.join(sorted(TRANSPORTS))}"
        ) from None
    check_url_allowed(default_target, settings.allowlist(), secure_schemes=secure)

    def build(profile: SutProfile, tenant: Tenant | None) -> Transport:
        where = tenant.target if tenant and tenant.target else default_target
        return cls(where, profile, token=credentials(profile, tenant))

    return build


def _check_pool(
    pool: TenantPool, profile: SutProfile, options: RunOptions, settings: Settings
) -> None:
    """Every tenant's credential and target, checked before the first frame (ADR-0041).

    A minted HS256 token carries the profile's claims and knows nothing about tenants, so
    every lane would authenticate as the *same* account while believing it was isolated:
    the shared-state corruption the pool exists to prevent, recorded as agent misbehaviour.
    """
    if options.transport_factory is not None:
        raise ConfigError(
            "a tenant pool needs a network transport: an injected transport has no tenant "
            "identity to apply, so every lane would share one"
        )
    minted = profile.token is not None
    secure = TRANSPORTS.get(options.transport, (None, ("https",)))[1]
    for tenant in pool.tenants:
        own = token_for(tenant, settings)
        if minted and not own:
            raise ConfigError(
                f"profile {profile.name!r} mints its own tokens, which cannot tell one "
                f"tenant from another, so tenant {tenant.id!r} would authenticate as "
                "every other one. Give each tenant a token_env."
            )
        if tenant.target:
            check_url_allowed(tenant.target, settings.allowlist(), secure_schemes=secure)


def build_trace_source(
    options: RunOptions, settings: Settings, profile: SutProfile
) -> TraceSource | None:
    if not options.traces or profile.traces is None:
        return None
    if options.trace_source is not None:
        return options.trace_source
    if profile.traces.source == "otlp-file":
        if not settings.otlp_spans_path:
            raise ConfigError(
                f"profile {profile.name!r} reads OTLP spans: set OPSEVAL_OTLP_SPANS_PATH "
                "to the file or directory the collector writes, or pass --no-traces"
            )
        return OtlpFileTraceSource(settings.otlp_spans_path)
    host = settings.langfuse_host
    public, secret = settings.langfuse_public_key, settings.langfuse_secret_key
    if not (host and public and secret):
        raise ConfigError(
            f"profile {profile.name!r} reads traces from Langfuse: set OPSEVAL_LANGFUSE_HOST, "
            "OPSEVAL_LANGFUSE_PUBLIC_KEY and OPSEVAL_LANGFUSE_SECRET_KEY, or pass --no-traces"
        )
    check_url_allowed(host, settings.allowlist(), secure_schemes=("https",))
    return LangfuseTraceSource(host, public, secret.get_secret_value())


def _needs_llm_persona(scenario: Scenario) -> bool:
    return not scenario.conversation.scripted_turns


def _unmet(
    scenario: Scenario, health: PreflightReport, no_simulator: bool
) -> tuple[str, SkipKind] | None:
    """Why preflight says this scenario cannot produce a verdict, if it says so.

    Only *dependency* outages reach here. An unreachable agent is refused by ``launch``
    instead, because a skip is merely a gate warning and would let a suite that tested
    nothing report "safe to release".

    Only dependencies that would leave *nothing* measurable count. A scenario with both
    deterministic and judge scorers still produces a real verdict without the judge, so
    skipping it would throw away the checks that can run.
    """
    needs_persona = _needs_llm_persona(scenario) and not no_simulator
    if needs_persona and (reason := health.reason("simulator")) is not None:
        return reason, "simulator"
    requires = [
        cls.requires
        for spec in effective_specs(scenario)
        if (cls := REGISTRY.get(spec.id)) is not None
    ]
    all_llm = bool(requires) and all(r == "llm" for r in requires)
    if all_llm and (reason := health.reason("judge")) is not None:
        return f"every scorer needs a judge, and {reason}", "dependency"
    all_trace = bool(requires) and all(r == "trace" for r in requires)
    if all_trace and (reason := health.reason("traces")) is not None:
        return f"every scorer needs traces, and {reason}", "dependency"
    return None


STOP_KINDS: tuple[SkipKind, ...] = ("fail_fast", "stopped", "cancelled")


class _RunControl:
    """Decides whether the next scenario starts: stop policies and cancel requests (ADR-0035).

    It never changes a verdict. A scenario it holds back gets a ``skipped_precondition``
    verdict naming the policy, so a stopped run reports what it did not run.
    """

    def __init__(
        self,
        stop: StopPolicy,
        store: FileStore,
        run_id: str,
        policy: ResolvedGatePolicy,
        declared: bool,
    ) -> None:
        self.stop = stop
        self.store = store
        self.run_id = run_id
        self.policy = policy
        self.declared = declared
        self.started = time.monotonic()
        self.stopped: StoppedEarly | None = None
        self._last: str | None = None
        self._consecutive = 0

    def _halt(self, by: StopCause, detail: str) -> None:
        if self.stopped is None:
            self.stopped = StoppedEarly(by=by, after=self._last, detail=detail)

    def before_scenario(self, scenario: Scenario) -> TestVerdict | None:
        """The verdict for a scenario that must not start, or ``None`` to run it."""
        if self.stopped is None and self.store.cancel_requested(self.run_id):
            self._halt("cancelled", "cancelled by request")
        limit = self.stop.max_wall_clock_seconds
        if self.stopped is None and limit is not None:
            elapsed = time.monotonic() - self.started
            if elapsed >= limit:
                self._halt(
                    "max_wall_clock_seconds",
                    f"max_wall_clock_seconds: the run had taken {elapsed:.0f}s of {limit:g}s",
                )
        if self.stopped is None:
            return None
        by = self.stopped.by
        kind: SkipKind = by if by in ("fail_fast", "cancelled") else "stopped"
        return skipped(
            scenario, VerdictStatus.SKIPPED_PRECONDITION, f"not run: {self.stopped.detail}", kind
        )

    def cancel_requested(self, scenario: Scenario, *, done: int) -> bool:
        """Checked before each attempt, so a cancel takes effect within one attempt."""
        if self.stopped is None and self.store.cancel_requested(self.run_id):
            self._halt(
                "cancelled", f"cancelled by request during {scenario.id} (after {done} attempts)"
            )
        return self.stopped is not None and self.stopped.by == "cancelled"

    def after_scenario(
        self, scenario: Scenario, verdict: TestVerdict, *, rate_limited: bool
    ) -> None:
        self._last = scenario.id
        if verdict.status is VerdictStatus.PASSED:
            self._consecutive = 0
        failed = verdict.status is VerdictStatus.FAILED
        if failed:
            self._consecutive += 1
        stop = self.stop
        if (
            stop.fail_fast
            and failed
            # A failure the policy would not block is no reason to stop.
            and failure_blocks(self.policy, scenario.criticality, self.declared)
        ):
            self._halt("fail_fast", f"fail_fast: {scenario.id} failed")
        elif (
            stop.stop_on_critical_failure
            and failed
            and scenario.criticality
            in (
                Criticality.CRITICAL,
                Criticality.BLOCKER,
            )
        ):
            self._halt(
                "stop_on_critical_failure",
                f"stop_on_critical_failure: {scenario.id} ({scenario.criticality.value}) failed",
            )
        elif (
            stop.max_consecutive_failures is not None
            and self._consecutive >= stop.max_consecutive_failures
        ):
            self._halt(
                "max_consecutive_failures",
                f"max_consecutive_failures: {self._consecutive} failed scenarios in a row, "
                f"the last {scenario.id}",
            )
        elif stop.stop_on_rate_limit and rate_limited:
            self._halt(
                "stop_on_rate_limit",
                f"stop_on_rate_limit: a rate limit outlasted its retries in {scenario.id}",
            )


async def launch(
    catalogue_target: str | Path,
    options: RunOptions,
    settings: Settings | None = None,
    *,
    log: Callable[[str], None] = lambda _msg: None,
) -> SuiteResult:
    """Load, run, score and persist a suite. Raises ConfigError for exit code 3."""
    settings = settings or Settings()
    # Before the catalogue is validated, so a suite may name a plugin's scorer and a
    # broken plugin is reported rather than becoming an UNKNOWN_SCORER later.
    load_scorers()
    if options.profile is None:
        raise ConfigError("no SUT profile: pass --profile path/to/profile.yaml")
    profile = load_profile(options.profile)
    catalogue, suite_name = load_catalogue(
        catalogue_target,
        profile,
        known_scorers=REGISTRY.keys(),
        known_invariants=set(INVARIANTS),
        filters=options.filters,
    )
    if options.only_ids is not None:
        wanted = set(options.only_ids)
        missing = sorted(wanted - {sc.id for sc in catalogue.scenarios})
        if missing:
            raise ConfigError(f"no valid scenario with id {', '.join(map(repr, missing))}")
        catalogue.scenarios = [sc for sc in catalogue.scenarios if sc.id in wanted]
        # A broken file elsewhere in the folder is not part of this run. (A broken file
        # that *was* chosen has no valid id, so it already failed as "no valid scenario".)
        catalogue.problems = []
    # Loaded and fully validated before the run, like the sinks: an unset tenant token or a
    # tenant target that is not allowlisted, found mid-suite, would fail the run with records
    # on disk; with one lane the pool's later tenants might never be drawn at all.
    pool = load_pool(settings.tenants_file) if settings.tenants_file else None
    if pool is None and settings.lanes is not None and settings.lanes > 1:
        raise ConfigError(
            f"--lanes {settings.lanes} needs --tenants: scenarios may only run in parallel "
            "once you have said which isolated accounts they may use"
        )
    lanes = lane_count(len(pool.tenants), settings.lanes, settings.lane_cap) if pool else 1
    if pool:
        _check_pool(pool, profile, options, settings)
    # Built before the run: a destination that is not allowlisted is a configuration
    # error, and finding that out after a suite has something to publish is too late.
    sinks = build_sinks(settings)
    make_transport = build_transport_factory(options, settings)
    trace_source = build_trace_source(options, settings, profile)
    meter = UsageMeter()
    roles = llm_roles_needed(catalogue.scenarios)
    sim_client = client_for_role(settings, "simulator", meter) if "simulator" in roles else None
    judge_client = client_for_role(settings, "judge", meter) if "judge" in roles else None
    check_judge_independence(
        catalogue.scenarios,
        profile,
        judge_model=judge_client.model if judge_client else None,
        simulator_model=sim_client.model if sim_client else None,
        mode=settings.judge_independence,
        log=log,
    )
    engine = ScoringEngine(
        judge=judge_client,
        judge_concurrency=settings.judge_concurrency,
        simulator_model=sim_client.model if sim_client else None,
        judge_independence=settings.judge_independence,
    )
    store = FileStore(settings.reports_dir)

    health = PreflightReport()
    if options.preflight:
        health = await preflight(
            catalogue.scenarios,
            profile,
            settings,
            target=make_transport(profile, None).target,
            transport=options.transport,
            traces_enabled=options.traces and profile.traces is not None,
            verify_llm=options.verify_llm,
            probe_target_host=options.transport_factory is None,
        )
        sut = health.get("sut")
        if sut is not None and sut.blocking:
            # Not a skip. A skipped scenario is only a gate *warning*, so an unreachable
            # agent would report "safe to release" on a suite that tested nothing. We
            # could not reach what we were asked to test: that is a config error (exit 3).
            raise ConfigError(
                f"the agent is not reachable at {make_transport(profile, None).target}: "
                f"{sut.detail}. Nothing was run. Check the target and the network, or "
                "pass --no-preflight to try anyway."
            )
        for check in health.blocking():
            log(f"preflight: {check.name} unavailable — {check.detail}")

    started = datetime.now(UTC)
    run_id = options.run_id or new_run_id(started)
    target_label = make_transport(profile, None).target
    store.write_manifest(
        run_id,
        {
            "run_id": run_id,
            "suite": suite_name,
            "target": target_label,
            "profile": profile.name,
            "planned": [s.id for s in catalogue.scenarios],
            "load_errors": [str(p) for p in catalogue.problems],
            "started_at": started.isoformat(),
            "opseval_version": __version__,
            # Who is running it, so a reader can tell "still running" from "its process
            # died before it finished" instead of showing it as running forever.
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "traces": trace_source.name if trace_source else None,
            "simulator_model": sim_client.model if sim_client else None,
            "judge_model": judge_client.model if judge_client else None,
            "config": config_digest(settings),
        },
    )
    store.save_profile(run_id, profile)

    policy = resolve_gate(
        catalogue.gate, preset=options.gate, fail_on_flaky=settings.gate_fail_on_flaky
    )
    declared = any(sc.criticality is not Criticality.NORMAL for sc in catalogue.scenarios)
    verdicts: list[TestVerdict] = []
    tracker = ProgressLog(store, run_id, [sc.id for sc in catalogue.scenarios])
    stop = (catalogue.stop or StopPolicy()).merged(options.stop)
    if options.fail_fast:
        stop = stop.model_copy(update={"fail_fast": True})
    control = _RunControl(stop, store, run_id, policy, declared)
    # Opt-in: without a tenant pool the run is sequential with no leases, as before.
    lock_dir = settings.tenant_lock_dir or (str(default_lock_dir(pool)) if pool else None)
    leases = (
        TenantLeases(
            pool,
            lock_dir or "",
            holder=f"{run_id}:{os.getpid()}",
            stale_after=settings.tenant_stale_after_seconds,
        )
        if pool
        else None
    )
    if pool:
        log(f"tenant pool: {len(pool.tenants)} tenant(s), {lanes} lane(s) · locks {lock_dir}")

    async def run_one(scenario: Scenario) -> TestVerdict:
        """One scenario from start to verdict: run control, skips, the lease, attempts."""
        halt = control.before_scenario(scenario)
        if halt is not None:
            # Named, so what was not run cannot vanish from the result.
            return halt
        if options.repeats:
            scenario = scenario.model_copy(
                update={
                    "execution": scenario.execution.model_copy(update={"repeats": options.repeats})
                }
            )
        store.save_scenario(run_id, scenario)
        tracker.started(scenario.id, scenario.execution.repeats)
        if scenario.is_blocked:
            return skipped(scenario, VerdictStatus.BLOCKED, "tagged 'blocked'", "quarantine")
        blocked_by = _unmet(scenario, health, sim_client is None)
        if blocked_by is not None:
            reason, kind = blocked_by
            return skipped(scenario, VerdictStatus.SKIPPED_PRECONDITION, reason, kind)
        if _needs_llm_persona(scenario) and sim_client is None:
            return skipped(
                scenario,
                VerdictStatus.SKIPPED_PRECONDITION,
                "needs an LLM persona: set OPSEVAL_SIMULATOR_BASE_URL and OPSEVAL_SIMULATOR_MODEL",
                "simulator",
            )
        if scenario.conversation.stop_at == "outcome_committed" and not options.allow_commit:
            return skipped(
                scenario,
                VerdictStatus.SKIPPED_PRECONDITION,
                "commit guard: this scenario performs the irreversible outcome; "
                "re-run with --allow-commit against a test tenant",
                "commit_guard",
            )
        tenant: Tenant | None = None
        lease = None
        if leases is not None:
            lease = await leases.acquire_waiting(timeout=settings.tenant_wait_seconds)
            if lease is None:
                # Not the agent's fault: the harness could not obtain a resource. Two
                # scenarios sharing a tenant would produce false agent failures, far worse
                # than a skip (ADR-0041).
                return skipped(
                    scenario,
                    VerdictStatus.SKIPPED_PRECONDITION,
                    "no free test tenant: every tenant in the pool is leased by a live run "
                    f"(waited {settings.tenant_wait_seconds:g}s)",
                    "dependency",
                )
            tenant = lease.tenant
        try:
            where = f" · tenant {tenant.id}" if tenant else ""
            log(f"▶ {scenario.id} · {scenario.name}{where}")
            attempts = []
            rate_limited = False
            for i in range(scenario.execution.repeats):
                if control.cancel_requested(scenario, done=i):
                    break
                seed = attempt_seed(scenario.id, i)
                sim: Simulator = (
                    ScriptedSimulator(scenario, seed)
                    if scenario.conversation.scripted_turns or sim_client is None
                    else LLMSimulator(scenario, seed, sim_client)
                )
                driver = Driver(
                    scenario=scenario,
                    profile=profile,
                    transport=make_transport(profile, tenant),
                    simulator=sim,
                    run_id=run_id,
                    attempt_index=i,
                    seed=seed,
                    allow_commit=options.allow_commit,
                    simulator_timeout_seconds=settings.simulator_timeout_seconds,
                )
                async with refreshing(lease):
                    record = await driver.run()
                rate_limited = rate_limited or RATE_LIMITED_NOTE in record.notes
                if trace_source is not None:
                    record = await enrich(record, trace_source, profile)
                path = store.save_record(record)
                tracker.attempt(scenario.id)
                scores = await engine.score(record, scenario, profile)
                verdict = attempt_verdict(record, scores, str(path))
                attempts.append(verdict)
                state = (
                    "not judged"
                    if verdict.passed is None
                    else ("pass" if verdict.passed else "FAIL")
                )
                coverage = " · traces" if record.trace_coverage == "full" else ""
                # With lanes in flight the lines interleave: say whose attempt it is.
                whose = f"{scenario.id} " if lanes > 1 else ""
                log(f"   {whose}attempt {i}: {state} ({record.terminal_state.value}{coverage})")
        finally:
            if lease is not None:
                lease.release()
        if len(attempts) < scenario.execution.repeats:
            # Cancelled part-way: never a pass from part of the repeats, never a failure
            # hidden as "not run" (fold_cut_short). Records stay on disk.
            return fold_cut_short(scenario, attempts)
        folded = fold_attempts(scenario, attempts)
        control.after_scenario(scenario, folded, rate_limited=rate_limited)
        return folded

    async def decided(scenario: Scenario) -> TestVerdict:
        verdict = await run_one(scenario)
        tracker.ended(verdict)
        return verdict

    if lanes <= 1:
        for scenario in catalogue.scenarios:
            verdicts.append(await decided(scenario))
    else:
        # Bounded by the lane count, which is bounded by the pool, so no two scenarios in
        # flight want the same tenant. A stop policy stops new scenarios from starting; it
        # cannot un-start those already running (ADR-0041). Results keep catalogue order.
        slots = asyncio.Semaphore(lanes)

        async def lane(scenario: Scenario) -> TestVerdict:
            async with slots:
                return await decided(scenario)

        verdicts = list(await asyncio.gather(*(lane(sc) for sc in catalogue.scenarios)))
    if control.stopped is not None and not any(t.skip_kind in STOP_KINDS for t in verdicts):
        # The policy fired on the last scenario, or a cancel came after it: nothing was
        # left out, so the run did not stop early.
        control.stopped = None
    if control.stopped is not None:
        log(f"stopped early: {control.stopped.detail}")

    tracker.finishing()
    gate = release_gate(verdicts, policy=policy, load_errors=len(catalogue.problems))
    result = SuiteResult(
        opseval_version=__version__,
        run_id=run_id,
        suite=suite_name,
        target=target_label,
        profile=profile.name,
        started_at=started,
        ended_at=datetime.now(UTC),
        tests=verdicts,
        gate=gate,
        exit_code=exit_code(verdicts, load_errors=len(catalogue.problems), gate=gate),
        usage=meter.snapshot(),
        config_digest=config_digest(settings),
        load_errors=[str(p) for p in catalogue.problems],
        preflight=health if options.preflight else None,
        stop=stop if stop.active else None,
        stopped=control.stopped,
    )
    # After the verdict exists and before it is written, so the outcomes are part of the
    # saved result. A sink can never change the verdict; publish_all catches everything.
    result = result.model_copy(update={"sinks": await publish_all(sinks, result, log=log)})
    store.save_report(run_id, render_html(result, store.load_records(run_id), profile))
    store.save_junit(run_id, render_junit(result))
    store.save_result(result)  # written last: marks the run as complete
    # After the result is on disk, so a slow or dead collector cannot cost anything.
    await report_telemetry(
        result,
        settings,
        event=TelemetryEvent.RUN,
        transports=[options.transport],
        languages=[s.persona.language for s in catalogue.scenarios],
        trace_source=trace_source.name if trace_source else None,
        demo_target=str(catalogue_target).startswith(str(demo_catalogue_dir())),
        log=log,
    )
    return result
