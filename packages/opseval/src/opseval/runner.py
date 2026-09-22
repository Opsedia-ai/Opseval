# SPDX-License-Identifier: Apache-2.0
"""One launch pipeline for the CLI (and, later, the server)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from opseval import __version__
from opseval.catalogue import load_catalogue, load_profile
from opseval.config import Settings, check_url_allowed, config_digest
from opseval.driver import Driver
from opseval.errors import ConfigError
from opseval.llm import ChatClient, UsageMeter
from opseval.models.profile import SutProfile
from opseval.models.results import SuiteResult, TestVerdict, VerdictStatus
from opseval.models.scenario import Scenario
from opseval.reporting import render_html
from opseval.scoring import INVARIANTS, REGISTRY, ScoringEngine
from opseval.simulator import LLMSimulator, ScriptedSimulator, Simulator, attempt_seed
from opseval.storage import FileStore
from opseval.transport import HttpJsonTransport, Transport, WebSocketTransport
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
    transport_factory: TransportFactory | None = None


def _llm_client(settings: Settings, role: str, meter: UsageMeter) -> ChatClient | None:
    base = getattr(settings, f"{role}_base_url")
    model = getattr(settings, f"{role}_model")
    key = getattr(settings, f"{role}_api_key")
    if not base or not model:
        return None
    if key is not None:
        check_url_allowed(base, settings.allowlist(), secure_schemes=("https",))
    return ChatClient(
        base_url=base,
        model=model,
        api_key=key.get_secret_value() if key else None,
        role=role,
        meter=meter,
    )


def build_transport_factory(options: RunOptions, settings: Settings) -> TransportFactory:
    if options.transport_factory is not None:
        return options.transport_factory
    if not options.target:
        raise ConfigError("no target: pass --target (e.g. ws://localhost:8765)")
    target = options.target
    token = settings.static_token.get_secret_value() if settings.static_token else None
    if options.transport == "websocket":
        check_url_allowed(target, settings.allowlist(), secure_schemes=("wss",))
        return lambda profile: WebSocketTransport(target, profile, token=token)
    if options.transport == "http-json":
        check_url_allowed(target, settings.allowlist(), secure_schemes=("https",))
        return lambda profile: HttpJsonTransport(target, profile, token=token)
    raise ConfigError(f"unknown transport {options.transport!r}; use websocket or http-json")


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
    meter = UsageMeter()
    sim_client = _llm_client(settings, "simulator", meter)
    judge_client = _llm_client(settings, "judge", meter)
    engine = ScoringEngine(judge=judge_client, judge_concurrency=settings.judge_concurrency)
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
            "config": config_digest(settings),
        },
    )

    verdicts: list[TestVerdict] = []
    for scenario in catalogue.scenarios:
        if options.repeats:
            scenario = scenario.model_copy(
                update={
                    "execution": scenario.execution.model_copy(update={"repeats": options.repeats})
                }
            )
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
            path = store.save_record(record)
            scores = await engine.score(record, scenario, profile)
            verdict = attempt_verdict(record, scores, str(path))
            attempts.append(verdict)
            state = (
                "not judged" if verdict.passed is None else ("pass" if verdict.passed else "FAIL")
            )
            log(f"   attempt {i}: {state} ({record.terminal_state.value})")
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
    store.save_result(result)  # written last: marks the run as complete
    return result
