# SPDX-License-Identifier: Apache-2.0
"""One launch pipeline for the CLI (and, later, the server)."""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

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
    only_ids: list[str] | None = None
    """Run only these scenario ids (any of them, unlike ``filters``, which must all hold).
    An id that is not in the catalogue is a configuration error, never silently dropped."""


def build_transport_factory(options: RunOptions, settings: Settings) -> TransportFactory:
    if options.transport_factory is not None:
        return options.transport_factory
    if not options.target:
        raise ConfigError("no target: pass --target (e.g. ws://localhost:8765)")
    target = options.target
    token = settings.static_token.get_secret_value() if settings.static_token else None
    signing_key = settings.token_signing_key

    def credentials(profile: SutProfile) -> str | TokenSource | None:
        """A fresh token source per attempt: HS256 when the profile asks for it."""
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

    if options.transport == "websocket":
        check_url_allowed(target, settings.allowlist(), secure_schemes=("wss",))
        return lambda profile: WebSocketTransport(target, profile, token=credentials(profile))
    if options.transport == "http-json":
        check_url_allowed(target, settings.allowlist(), secure_schemes=("https",))
        return lambda profile: HttpJsonTransport(target, profile, token=credentials(profile))
    if options.transport == "openai-chat":
        check_url_allowed(target, settings.allowlist(), secure_schemes=("https",))
        return lambda profile: OpenAIChatTransport(target, profile, token=credentials(profile))
    if options.transport == "sse":
        check_url_allowed(target, settings.allowlist(), secure_schemes=("https",))
        return lambda profile: SseTransport(target, profile, token=credentials(profile))
    raise ConfigError(
        f"unknown transport {options.transport!r}; use websocket, http-json, sse or openai-chat"
    )


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
            target=make_transport(profile).target,
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
                f"the agent is not reachable at {make_transport(profile).target}: "
                f"{sut.detail}. Nothing was run. Check the target and the network, or "
                "pass --no-preflight to try anyway."
            )
        for check in health.blocking():
            log(f"preflight: {check.name} unavailable — {check.detail}")

    started = datetime.now(UTC)
    run_id = options.run_id or new_run_id(started)
    target_label = make_transport(profile).target
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

    verdicts: list[TestVerdict] = []
    for scenario in catalogue.scenarios:
        if options.repeats:
            scenario = scenario.model_copy(
                update={
                    "execution": scenario.execution.model_copy(update={"repeats": options.repeats})
                }
            )
        store.save_scenario(run_id, scenario)
        if scenario.is_blocked:
            verdicts.append(skipped(scenario, VerdictStatus.BLOCKED, "tagged 'blocked'"))
            continue
        blocked_by = _unmet(scenario, health, sim_client is None)
        if blocked_by is not None:
            verdicts.append(skipped(scenario, VerdictStatus.SKIPPED_PRECONDITION, blocked_by))
            continue
        if _needs_llm_persona(scenario) and sim_client is None:
            verdicts.append(
                skipped(
                    scenario,
                    VerdictStatus.SKIPPED_PRECONDITION,
                    "needs an LLM persona: set OPSEVAL_SIMULATOR_BASE_URL and "
                    "OPSEVAL_SIMULATOR_MODEL",
                )
            )
            continue
        if scenario.conversation.stop_at == "outcome_committed" and not options.allow_commit:
            verdicts.append(
                skipped(
                    scenario,
                    VerdictStatus.SKIPPED_PRECONDITION,
                    "commit guard: this scenario performs the irreversible outcome; "
                    "re-run with --allow-commit against a test tenant",
                )
            )
            continue
        log(f"▶ {scenario.id} · {scenario.name}")
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
                transport=make_transport(profile),
                simulator=sim,
                run_id=run_id,
                attempt_index=i,
                seed=seed,
                allow_commit=options.allow_commit,
            )
            record = await driver.run()
            if trace_source is not None:
                record = await enrich(record, trace_source, profile)
            path = store.save_record(record)
            scores = await engine.score(record, scenario, profile)
            verdict = attempt_verdict(record, scores, str(path))
            attempts.append(verdict)
            state = (
                "not judged" if verdict.passed is None else ("pass" if verdict.passed else "FAIL")
            )
            coverage = " · traces" if record.trace_coverage == "full" else ""
            log(f"   attempt {i}: {state} ({record.terminal_state.value}{coverage})")
        verdicts.append(fold_attempts(scenario, attempts))
        if options.fail_fast and verdicts[-1].status is VerdictStatus.FAILED:
            log("fail-fast: stopping after the first failed scenario")
            break

    gate = release_gate(
        verdicts, fail_on_flaky=settings.gate_fail_on_flaky, load_errors=len(catalogue.problems)
    )
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
