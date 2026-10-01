# SPDX-License-Identifier: Apache-2.0
"""One launch pipeline for the CLI (and, later, the server)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from opseval import __version__
from opseval.auth import HS256Tokens, TokenSource
from opseval.catalogue import load_catalogue, load_profile
from opseval.config import Settings, check_url_allowed, config_digest
from opseval.driver import Driver
from opseval.errors import ConfigError
from opseval.llm import UsageMeter, client_for_role
from opseval.models.profile import SutProfile
from opseval.models.results import SuiteResult, TestVerdict, VerdictStatus
from opseval.models.scenario import Scenario
from opseval.reporting import render_html
from opseval.reporting.junit import render_junit
from opseval.scoring import INVARIANTS, REGISTRY, ScoringEngine
from opseval.simulator import LLMSimulator, ScriptedSimulator, Simulator, attempt_seed
from opseval.storage import FileStore
from opseval.traces import TraceSource, enrich
from opseval.traces.langfuse import LangfuseTraceSource
from opseval.transport import (
    HttpJsonTransport,
    OpenAIChatTransport,
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
    raise ConfigError(
        f"unknown transport {options.transport!r}; use websocket, http-json or openai-chat"
    )


def build_trace_source(
    options: RunOptions, settings: Settings, profile: SutProfile
) -> TraceSource | None:
    if not options.traces or profile.traces is None:
        return None
    if options.trace_source is not None:
        return options.trace_source
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


async def launch(
    catalogue_target: str | Path,
    options: RunOptions,
    settings: Settings | None = None,
    *,
    log: Callable[[str], None] = lambda _msg: None,
) -> SuiteResult:
    """Load, run, score and persist a suite. Raises ConfigError for exit code 3."""
    settings = settings or Settings()
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
    make_transport = build_transport_factory(options, settings)
    trace_source = build_trace_source(options, settings, profile)
    meter = UsageMeter()
    sim_client = client_for_role(settings, "simulator", meter)
    judge_client = client_for_role(settings, "judge", meter)
    engine = ScoringEngine(
        judge=judge_client,
        judge_concurrency=settings.judge_concurrency,
        simulator_model=sim_client.model if sim_client else None,
        judge_independence=settings.judge_independence,
    )
    store = FileStore(settings.reports_dir)

    started = datetime.now(UTC)
    run_id = f"{started:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
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
            "traces": trace_source.name if trace_source else None,
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
    )
    store.save_report(run_id, render_html(result, store.load_records(run_id)))
    store.save_junit(run_id, render_junit(result))
    store.save_result(result)  # written last: marks the run as complete
    return result
