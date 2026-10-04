# SPDX-License-Identifier: Apache-2.0
"""One launch pipeline for the CLI (and, later, the server)."""

from __future__ import annotations

import asyncio
import os
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
from opseval.driver import Driver
from opseval.errors import ConfigError
from opseval.llm import UsageMeter, client_for_role
from opseval.models.preflight import PreflightReport
from opseval.models.profile import SutProfile
from opseval.models.results import SuiteResult, TestVerdict, VerdictStatus
from opseval.models.scenario import Scenario
from opseval.models.telemetry import TelemetryEvent
from opseval.models.tenant import Tenant, TenantPool, lane_count
from opseval.plugins import load_scorers
from opseval.preflight import preflight
from opseval.reporting import render_html
from opseval.reporting.junit import render_junit
from opseval.scoring import INVARIANTS, REGISTRY, ScoringEngine, effective_specs
from opseval.scoring.base import check_judge_independence
from opseval.simulator import LLMSimulator, ScriptedSimulator, Simulator, attempt_seed
from opseval.sinks import build_sinks, publish_all
from opseval.storage import FileStore
from opseval.telemetry import report as report_telemetry
from opseval.tenants import (
    TenantLeases,
    default_lock_dir,
    load_pool,
    refreshing,
    token_for,
)
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
    fold_attempts,
    release_gate,
    skipped,
)

TransportFactory = Callable[[SutProfile, "Tenant | None"], Transport]
"""Builds a transport for one attempt. The tenant, when the run uses a pool,
supplies its own target and credentials (ADR-0029)."""


@dataclass
class RunOptions:
    target: str | None = None
    transport: str = "websocket"
    profile: str | Path | None = None
    repeats: int | None = None
    allow_commit: bool = False
    fail_fast: bool = False
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


class _TransportCtor(Protocol):
    """What every transport's constructor takes. ``Transport`` itself is the behavioural
    interface and says nothing about construction, so the table needs this."""

    def __call__(
        self, target: str, profile: SutProfile, /, *, token: str | TokenSource | None = None
    ) -> Transport: ...


TRANSPORTS: dict[str, tuple[_TransportCtor, tuple[str, ...]]] = {
    "websocket": (WebSocketTransport, ("wss",)),
    "http-json": (HttpJsonTransport, ("https",)),
    "openai-chat": (OpenAIChatTransport, ("https",)),
    "sse": (SseTransport, ("https",)),
}
"""Transport class and the schemes that count as secure for it. One table so that a
change to how a target is resolved cannot be made in three places and missed in the
fourth, and so the error message lists what actually exists."""


def build_transport_factory(options: RunOptions, settings: Settings) -> TransportFactory:
    if options.transport_factory is not None:
        return options.transport_factory
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

    # Eagerly, so a bad host fails before anything runs - like preflight and the sinks.
    # A tenant with its own target is checked in _check_pool, also before the first frame.
    check_url_allowed(default_target, settings.allowlist(), secure_schemes=secure)

    def build(profile: SutProfile, tenant: Tenant | None) -> Transport:
        where = tenant.target if tenant and tenant.target else default_target
        return cls(where, profile, token=credentials(profile, tenant))

    return build


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


def _unmet(scenario: Scenario, health: PreflightReport, no_simulator: bool) -> str | None:
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
        return reason
    requires = [
        cls.requires
        for spec in effective_specs(scenario)
        if (cls := REGISTRY.get(spec.id)) is not None
    ]
    all_llm = bool(requires) and all(r == "llm" for r in requires)
    if all_llm and (reason := health.reason("judge")) is not None:
        return f"every scorer needs a judge, and {reason}"
    all_trace = bool(requires) and all(r == "trace" for r in requires)
    if all_trace and (reason := health.reason("traces")) is not None:
        return f"every scorer needs traces, and {reason}"
    return None


def _check_pool(
    pool: TenantPool, profile: SutProfile, options: RunOptions, settings: Settings
) -> None:
    """Every tenant's credential and target, checked before the first frame.

    A minted HS256 token is the trap here: it carries the profile's claims and knows
    nothing about tenants, so every lane would authenticate as the *same* account while
    believing it was isolated - the shared-state corruption the pool exists to prevent,
    with the harness recording the result as agent misbehaviour.
    """
    minted = profile.token is not None
    for tenant in pool.tenants:
        own = token_for(tenant, settings)
        if minted and not own:
            raise ConfigError(
                f"profile {profile.name!r} mints its own tokens, which cannot tell one "
                f"tenant from another, so tenant {tenant.id!r} would authenticate as "
                "every other one. Give each tenant a token_env."
            )
        if tenant.target:
            check_url_allowed(
                tenant.target,
                settings.allowlist(),
                secure_schemes=("wss",) if options.transport == "websocket" else ("https",),
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
    # Loaded and fully validated before the run, for the same reason the sinks are: an
    # unset tenant token or a non-allowlisted tenant target discovered mid-suite kills
    # the run after records are already on disk - and with lanes=1 the pool's later
    # tenants may never be drawn at all, so the misconfiguration would go unnoticed and
    # the pool would silently degrade to one account.
    pool = load_pool(settings.tenants_file) if settings.tenants_file else None
    if pool is None and settings.lanes is not None and settings.lanes > 1:
        raise ConfigError(
            f"--lanes {settings.lanes} needs --tenants: scenarios may only run in "
            "parallel once you have said which isolated accounts they may use"
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
    sim_client = client_for_role(settings, "simulator", meter)
    judge_client = client_for_role(settings, "judge", meter)
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
    run_id = f"{started:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
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
            "traces": trace_source.name if trace_source else None,
            "simulator_model": sim_client.model if sim_client else None,
            "judge_model": judge_client.model if judge_client else None,
            "config": config_digest(settings),
        },
    )
    store.save_profile(run_id, profile)

    # Opt-in: without a tenant pool the run is sequential with no leases, exactly as
    # before, so nothing changes for anyone not using one.
    lock_dir = settings.tenant_lock_dir or (default_lock_dir(pool) if pool else None)
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
        if options.repeats:
            scenario = scenario.model_copy(
                update={
                    "execution": scenario.execution.model_copy(update={"repeats": options.repeats})
                }
            )
        store.save_scenario(run_id, scenario)
        if scenario.is_blocked:
            return skipped(scenario, VerdictStatus.BLOCKED, "tagged 'blocked'")
        blocked_by = _unmet(scenario, health, sim_client is None)
        if blocked_by is not None:
            return skipped(scenario, VerdictStatus.SKIPPED_PRECONDITION, blocked_by)
        if _needs_llm_persona(scenario) and sim_client is None:
            return skipped(
                scenario,
                VerdictStatus.SKIPPED_PRECONDITION,
                "needs an LLM persona: set OPSEVAL_SIMULATOR_BASE_URL and OPSEVAL_SIMULATOR_MODEL",
            )
        if scenario.conversation.stop_at == "outcome_committed" and not options.allow_commit:
            return skipped(
                scenario,
                VerdictStatus.SKIPPED_PRECONDITION,
                "commit guard: this scenario performs the irreversible outcome; "
                "re-run with --allow-commit against a test tenant",
            )

        tenant: Tenant | None = None
        lease = None
        if leases is not None:
            lease = await leases.acquire_waiting(timeout=settings.tenant_wait_seconds)
            if lease is None:
                # Not the agent's fault: the harness could not obtain a resource. Two
                # scenarios sharing a tenant would produce false agent failures, which
                # is far worse than a skip.
                return skipped(
                    scenario,
                    VerdictStatus.SKIPPED_PRECONDITION,
                    "no free test tenant: every tenant in the pool is leased by a live run "
                    f"(waited {settings.tenant_wait_seconds:g}s)",
                )
            tenant = lease.tenant
        try:
            where = f" · tenant {tenant.id}" if tenant else ""
            log(f"▶ {scenario.id} · {scenario.name}{where}")
            attempts = []
            for i in range(scenario.execution.repeats):
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
                )
                async with refreshing(lease):
                    record = await driver.run()
                if trace_source is not None:
                    record = await enrich(record, trace_source, profile)
                path = store.save_record(record)
                scores = await engine.score(record, scenario, profile)
                verdict = attempt_verdict(record, scores, str(path))
                attempts.append(verdict)
                state = (
                    "not judged"
                    if verdict.passed is None
                    else ("pass" if verdict.passed else "FAIL")
                )
                coverage = " · traces" if record.trace_coverage == "full" else ""
                # With lanes in flight the lines interleave, so an unlabelled "attempt 0"
                # reads as belonging to whichever header happened to print last.
                whose = f"{scenario.id} " if lanes > 1 else ""
                log(f"   {whose}attempt {i}: {state} ({record.terminal_state.value}{coverage})")
            return fold_attempts(scenario, attempts)
        finally:
            if lease is not None:
                lease.release()

    verdicts: list[TestVerdict] = []
    if lanes <= 1:
        for scenario in catalogue.scenarios:
            verdicts.append(await run_one(scenario))
            if options.fail_fast and verdicts[-1].status is VerdictStatus.FAILED:
                log("fail-fast: stopping after the first failed scenario")
                break
    else:
        # Bounded by the lane count, which is bounded by the pool, so no two scenarios
        # in flight can want the same tenant.
        slots = asyncio.Semaphore(lanes)

        async def lane(scenario: Scenario) -> TestVerdict:
            async with slots:
                return await run_one(scenario)

        verdicts = list(await asyncio.gather(*(lane(s) for s in catalogue.scenarios)))
        if options.fail_fast and any(v.status is VerdictStatus.FAILED for v in verdicts):
            # Honest about the limit: with lanes in flight, fail-fast cannot un-start
            # work that had already begun.
            log("fail-fast: a scenario failed; scenarios already in flight still finished")

    gate = release_gate(verdicts, fail_on_flaky=settings.gate_fail_on_flaky)
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
        exit_code=exit_code(verdicts, load_errors=len(catalogue.problems)),
        usage=meter.snapshot(),
        config_digest=config_digest(settings),
        load_errors=[str(p) for p in catalogue.problems],
        preflight=health if options.preflight else None,
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
