# SPDX-License-Identifier: Apache-2.0
"""One launch pipeline for the CLI (and, later, the server)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from opseval import __version__
from opseval.auth import (
    CredentialContext,
    CredentialProvider,
    GuardedTokens,
    HS256Tokens,
    TokenSource,
)
from opseval.catalogue import load_catalogue, load_profile
from opseval.config import Settings, check_url_allowed, config_digest
from opseval.demo import catalogue_dir as demo_catalogue_dir
from opseval.driver import RATE_LIMITED_NOTE, Driver
from opseval.errors import ConfigError
from opseval.langpacks import effective_language
from opseval.llm import UsageMeter, client_for_role
from opseval.models.gate import Preset, ResolvedGatePolicy
from opseval.models.gate import resolve as resolve_gate
from opseval.models.preflight import PreflightReport
from opseval.models.profile import SutProfile
from opseval.models.record import RunRecord
from opseval.models.results import (
    AttemptVerdict,
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
from opseval.plugins import load_credential_provider, load_scorers
from opseval.preflight import llm_roles_needed, preflight, probe_target
from opseval.progress import ProgressLog
from opseval.reporting import render_html
from opseval.reporting.junit import render_junit
from opseval.scoring import INVARIANTS, REGISTRY, ScoringEngine, effective_specs
from opseval.scoring.base import check_judge_independence
from opseval.simulator import LLMSimulator, ScriptedSimulator, Simulator, attempt_seed
from opseval.sinks import build_sinks, publish_all
from opseval.storage import FileStore
from opseval.telemetry import report as report_telemetry
from opseval.tenants import (
    Lease,
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
from opseval.transport.audio import DUPLEX, AudioTransport
from opseval.transport.audio_http import AudioHttpTransport
from opseval.transport.audio_ws import AudioWsTransport
from opseval.verdicts import (
    TENANT_LEASE_LOST,
    attempt_verdict,
    exit_code,
    failure_blocks,
    fold_attempts,
    fold_cut_short,
    release_gate,
    skipped,
)
from opseval.voice import AudioPlan, AudioRecorder, SpeechKit, VoiceTransport, check_audio_privacy

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
    synthetic_voices: bool = False
    """The voices are the demo's own synthetic ones (``opseval demo --voice``): exempt from
    the rule that recorded audio must be encrypted (ADR-0053). Never set for a real agent."""


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


class _AudioCtor(Protocol):
    def __call__(
        self, target: str, profile: SutProfile, /, *, token: str | TokenSource | None = None
    ) -> AudioTransport: ...


VOICE_TRANSPORTS: dict[str, tuple[_AudioCtor, tuple[str, ...]]] = {
    "audio-ws": (AudioWsTransport, ("wss",)),
    "audio-http": (AudioHttpTransport, ("https",)),
}
"""Speaking agents, reached through the voice bridge (ADR-0053)."""


def secure_schemes(transport: str) -> tuple[str, ...]:
    """The schemes that count as secure for a transport, text or voice."""
    if transport in VOICE_TRANSPORTS:
        return VOICE_TRANSPORTS[transport][1]
    return TRANSPORTS.get(transport, (None, ("https",)))[1]


def speech_kit(settings: Settings, profile: SutProfile, meter: UsageMeter) -> SpeechKit:
    """The guarded TTS, STT and optional second STT a voice run needs, or
    :class:`ConfigError` (exit 3) before anything is sent: a voice run without a TTS or an STT
    could not speak or listen, and every URL is allowlisted when it is built (ADR-0054)."""
    from opseval.speech.providers import (
        check_speech_settings,
        second_speech_to_text,
        speech_to_text,
        text_to_speech,
    )

    if profile.audio is None:
        raise ConfigError(
            f"profile {profile.name!r} has no audio: block, which a voice transport needs "
            "(codec, rate, end_of_turn_silence_ms; ADR-0053)"
        )
    check_speech_settings(settings)
    tts = text_to_speech(settings, meter)
    stt = speech_to_text(settings, meter)
    if tts is None or stt is None:
        missing = [n for n, p in (("OPSEVAL_TTS_*", tts), ("OPSEVAL_STT_*", stt)) if p is None]
        raise ConfigError(
            f"a voice run speaks and listens: configure {' and '.join(missing)} "
            "(URL and model, or a provider plugin)"
        )
    return SpeechKit(
        tts=tts,
        stt=stt,
        second=second_speech_to_text(settings, meter),
        min_confidence=settings.stt_min_confidence,
        canary_max_wer=settings.stt_canary_max_wer,
    )


TenantTransportFactory = Callable[[SutProfile, "Tenant | None"], Transport]
"""Builds the transport for one attempt. With a pool, the tenant supplies its own target and
credentials (ADR-0041)."""


def build_transport_factory(
    options: RunOptions, settings: Settings, *, kit: SpeechKit | None = None
) -> TenantTransportFactory:
    """``kit`` is the speech a voice transport (``audio-ws``, ``audio-http``) needs; each
    attempt then gets a :class:`VoiceTransport` to give its own recorder and language."""
    if options.transport_factory is not None:
        injected = options.transport_factory
        return lambda profile, _tenant: injected(profile)
    if not options.target:
        raise ConfigError("no target: pass --target (e.g. ws://localhost:8765)")
    default_target = options.target
    token = settings.static_token.get_secret_value() if settings.static_token else None
    signing_key = settings.token_signing_key
    provider = credential_provider(settings)

    def credentials(
        profile: SutProfile, tenant: Tenant | None, where: str
    ) -> str | TokenSource | None:
        """A fresh token source per attempt: a provider plugin's, or HS256 when the profile
        asks for it.

        A tenant's own token wins over everything else, because the whole point of the pool
        is that each tenant authenticates as itself.
        """
        own = token_for(tenant, settings) if tenant else None
        if own:
            return own
        if provider is not None:
            _no_two_minters(profile, settings)
            context = CredentialContext(
                profile=profile.name,
                target=where,
                tenant=tenant.id if tenant else None,
                value=settings.secret_named,
            )
            return GuardedTokens(provider, context)
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

    if options.transport in VOICE_TRANSPORTS:
        audio_cls, voice_secure = VOICE_TRANSPORTS[options.transport]
        if kit is None:
            raise ConfigError(f"the {options.transport} transport needs TTS and STT settings")
        check_url_allowed(default_target, settings.allowlist(), secure_schemes=voice_secure)
        voice_kit = kit

        def build_voice(profile: SutProfile, tenant: Tenant | None) -> Transport:
            where = tenant.target if tenant and tenant.target else default_target
            audio = audio_cls(where, profile, token=credentials(profile, tenant, where))
            return VoiceTransport(audio, profile, voice_kit)

        return build_voice
    try:
        cls, secure = TRANSPORTS[options.transport]
    except KeyError:
        known = sorted([*TRANSPORTS, *VOICE_TRANSPORTS])
        raise ConfigError(
            f"unknown transport {options.transport!r}; use {', '.join(known)}"
        ) from None
    check_url_allowed(default_target, settings.allowlist(), secure_schemes=secure)

    def build(profile: SutProfile, tenant: Tenant | None) -> Transport:
        where = tenant.target if tenant and tenant.target else default_target
        return cls(where, profile, token=credentials(profile, tenant, where))

    return build


def _tenant_targets(pool: TenantPool | None, default: str | None) -> set[str]:
    """Every host the pool's attempts will reach (a tenant's own target, else --target)."""
    if pool is None:
        return set()
    return {t.target or default or "" for t in pool.tenants} - {""}


def _shown(url: str) -> str:
    """A target as it may be published: scheme, host, port and path, never credentials or a
    query string (a target URL can carry a token)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}{parts.path}" if parts.scheme else url


def credential_provider(settings: Settings) -> CredentialProvider | None:
    """The provider plugin named in ``OPSEVAL_CREDENTIALS``, if any (ADR-0046). Loaded before
    the run: a missing or broken plugin is a configuration error, exit 3."""
    name = settings.credentials.strip()
    if not name:
        return None
    provider: CredentialProvider = load_credential_provider(name)
    return provider


def _no_two_minters(profile: SutProfile, settings: Settings) -> None:
    if settings.credentials.strip() and profile.token is not None:
        raise ConfigError(
            f"profile {profile.name!r} mints HS256 tokens and OPSEVAL_CREDENTIALS names a "
            f"credential provider ({settings.credentials.strip()!r}): choose one, so it is "
            "clear which identity the agent sees"
        )


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
    secure = secure_schemes(options.transport)
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
    # A provider is told each attempt's tenant, so a tenant needs no token_env or target to
    # be a distinct identity; issuing one per tenant is then the provider's job (ADR-0046).
    provided = bool(settings.credentials.strip())
    if len(pool.tenants) > 1:
        # Isolation means each tenant is a different account: its own token or its own
        # deployment. Two tenants with neither (or the same of both) are one account
        # under two names, and lanes on it would share state (review of #62).
        identities: dict[tuple[str | None, str | None], str] = {}
        for tenant in pool.tenants:
            if tenant.token_env is None and tenant.target is None and not provided:
                raise ConfigError(
                    f"tenant {tenant.id!r} declares neither token_env nor target, so it is "
                    "not isolated from the other tenants: give each its own token or target"
                )
            who = tenant.token_env or (f"credentials:{tenant.id}" if provided else None)
            key = (who, tenant.target or options.target)
            if key in identities:
                raise ConfigError(
                    f"tenants {identities[key]!r} and {tenant.id!r} have the same token_env "
                    "and target: they are one account under two names"
                )
            identities[key] = tenant.id


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
    return scenario.conversation.llm_persona


def _unmet(
    scenario: Scenario, profile: SutProfile, health: PreflightReport, no_simulator: bool
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
        for spec in effective_specs(scenario, profile)
        if (cls := REGISTRY.get(spec.id)) is not None
    ]
    all_llm = bool(requires) and all(r == "llm" for r in requires)
    if all_llm and (reason := health.reason("judge")) is not None:
        return f"every scorer needs a judge, and {reason}", "dependency"
    all_trace = bool(requires) and all(r == "trace" for r in requires)
    if all_trace and (reason := health.reason("traces")) is not None:
        return f"every scorer needs traces, and {reason}", "dependency"
    return None


def _check_voice_scenarios(scenarios: list[Scenario], transport: str) -> None:
    """A scenario's ``voice:`` block (ADR-0055) on a run that cannot honour it is refused
    before anything is sent (exit 3): left out, its barge-ins, silences and noise would never
    happen, and the run would look as if it had tested them."""
    voiced = [s.id for s in scenarios if s.voice is not None]
    if voiced and transport not in VOICE_TRANSPORTS:
        raise ConfigError(
            f"scenario(s) {', '.join(voiced)} declare voice: (ADR-0055), which only a voice "
            f"transport (audio-ws, audio-http) can play; this run uses {transport}"
        )
    duplex = [s.id for s in scenarios if s.voice is not None and s.voice.runs_duplex]
    if duplex and transport in VOICE_TRANSPORTS:
        cls = VOICE_TRANSPORTS[transport][0]
        if DUPLEX not in getattr(cls, "capabilities", frozenset()):
            raise ConfigError(
                f"scenario(s) {', '.join(duplex)} run full duplex (voice.duplex or "
                f"voice.barge_in), which {transport} cannot: it hears the caller only once a "
                "turn is over. Use audio-ws"
            )


def _voice_language(scenarios: list[Scenario], profile: SutProfile) -> str:
    """The language the preflight canary is spoken in: the profile's, else the first
    scenario's persona's."""
    if profile.audio is not None and profile.audio.language:
        return profile.audio.language
    return scenarios[0].persona.language if scenarios else "en"


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
    kits: list[SpeechKit] = []
    try:
        return await _launch(catalogue_target, options, settings, log=log, kits=kits)
    finally:
        # The speech providers' HTTP clients, closed however the run ended.
        for kit in kits:
            with contextlib.suppress(Exception):  # closing must never change the outcome
                await kit.aclose()


async def _launch(
    catalogue_target: str | Path,
    options: RunOptions,
    settings: Settings | None,
    *,
    log: Callable[[str], None],
    kits: list[SpeechKit],
) -> SuiteResult:
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
        known_scorers=REGISTRY,
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
    _no_two_minters(profile, settings)
    if pool:
        _check_pool(pool, profile, options, settings)
    # Built before the run: a destination that is not allowlisted is a configuration
    # error, and finding that out after a suite has something to publish is too late.
    sinks = build_sinks(settings)
    meter = UsageMeter()
    voice = options.transport in VOICE_TRANSPORTS and options.transport_factory is None
    _check_voice_scenarios(catalogue.scenarios, options.transport)
    kit: SpeechKit | None = None
    audio_plan = AudioPlan("hashes")
    if voice:
        # Before anything is sent: audio that would be stored unencrypted, or a run that
        # could not speak or listen, is refused here (exit 3, ADR-0053).
        audio_plan = check_audio_privacy(settings, synthetic=options.synthetic_voices)
        kit = speech_kit(settings, profile, meter)
        kits.append(kit)
    make_transport = build_transport_factory(options, settings, kit=kit)
    trace_source = build_trace_source(options, settings, profile)
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

    default_where = make_transport(profile, None).target
    health = PreflightReport()
    if options.preflight:
        health = await preflight(
            catalogue.scenarios,
            profile,
            settings,
            target=default_where,
            transport=options.transport,
            traces_enabled=options.traces and profile.traces is not None,
            verify_llm=options.verify_llm,
            # With every tenant on its own deployment, no attempt goes to --target.
            probe_target_host=options.transport_factory is None
            and (pool is None or default_where in _tenant_targets(pool, options.target)),
            voice_scenarios=len(catalogue.scenarios) if voice else 0,
            voice_language=_voice_language(catalogue.scenarios, profile),
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
        for name in ("tts", "stt"):
            speech = health.get(name)
            if voice and speech is not None and speech.blocking:
                # Every scenario of a voice run speaks and listens, so a speech outage
                # silences all of them. A skip is only a gate warning; refusing says plainly
                # that nothing could be measured (as for an unreachable agent, ADR-0030).
                raise ConfigError(
                    f"the {name.upper()} provider is not usable: {speech.detail}. Nothing was "
                    "run: a voice run cannot be measured without speaking and listening"
                )
        for check in health.blocking():
            log(f"preflight: {check.name} unavailable — {check.detail}")
        # Tenants with their own deployment are probed too: the attempts go there, not to
        # --target (review of #62).
        for where in _tenant_targets(pool, options.target) - {default_where}:
            probe = await probe_target(where, options.transport)
            if probe.blocking:
                raise ConfigError(
                    f"tenant target {where} is not reachable: {probe.detail}. Nothing was run."
                )

    started = datetime.now(UTC)
    run_id = options.run_id or new_run_id(started)
    # The hosts attempts actually went to: with per-tenant targets, --target may not be one.
    used = sorted(_tenant_targets(pool, options.target)) if pool else [default_where]
    target_label = ", ".join(_shown(u) for u in used) if pool else default_where
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
        if scenario.is_blocked:
            return skipped(scenario, VerdictStatus.BLOCKED, "tagged 'blocked'", "quarantine")
        blocked_by = _unmet(scenario, profile, health, sim_client is None)
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
        # Started only now: a scenario still waiting for a tenant is not running.
        tracker.started(scenario.id, scenario.execution.repeats)
        try:
            async with refreshing(lease):
                return await attempts_of(scenario, tenant, lease)
        finally:
            if lease is not None:
                lease.release()

    async def one_attempt(
        scenario: Scenario, i: int, driver: Driver, lease: Lease | None
    ) -> tuple[RunRecord, AttemptVerdict]:
        """Drive, enrich, save and score one attempt. Every call it pays for (the simulator,
        speech, the judges) is in the meter's attempt block, and goes into its record."""
        record = await driver.run()
        if lease is not None and not lease.check():
            # Another run holds this tenant's lease now: the attempt may have shared
            # the account, so it is the harness's problem, never a verdict.
            record = record.model_copy(
                update={
                    "infra_errors": (
                        *record.infra_errors,
                        f"tenant lease lost: {lease.tenant.id} may have been shared",
                    ),
                    "notes": (*record.notes, TENANT_LEASE_LOST),
                }
            )
        if trace_source is not None:
            record = await enrich(record, trace_source, profile)
        path = store.save_record(record)
        tracker.attempt(scenario.id)
        scores = await engine.score(record, scenario, profile)
        verdict = attempt_verdict(record, scores, str(path))
        # Saved again with the calls the attempt paid for, judges included (ADR-0066). The
        # record was on disk before scoring, so a crash while judging still leaves it.
        record = record.model_copy(update={"usage_events": meter.events_for(scenario.id, i)})
        store.save_record(record)
        return record, verdict

    async def attempts_of(
        scenario: Scenario, tenant: Tenant | None, lease: Lease | None
    ) -> TestVerdict:
        """The scenario's attempts, under its lease for the whole time (conversation,
        enrichment and scoring), so a long scenario never looks abandoned."""
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
            transport = make_transport(profile, tenant)
            if isinstance(transport, VoiceTransport):
                recorder = AudioRecorder(
                    store.run_dir(run_id),
                    scenario.id,
                    i,
                    audio_plan.mode,
                    key=audio_plan.key,
                    run_id=run_id,
                )
                transport = transport.for_attempt(
                    recorder, scenario.persona.language, scenario.voice
                )
            driver = Driver(
                scenario=scenario,
                profile=profile,
                transport=transport,
                simulator=sim,
                run_id=run_id,
                attempt_index=i,
                seed=seed,
                allow_commit=options.allow_commit,
                simulator_timeout_seconds=settings.simulator_timeout_seconds,
            )
            with meter.attempt(scenario.id, i):
                record, verdict = await one_attempt(scenario, i, driver, lease)
            rate_limited = rate_limited or RATE_LIMITED_NOTE in record.notes
            attempts.append(verdict)
            state = (
                "not judged" if verdict.passed is None else ("pass" if verdict.passed else "FAIL")
            )
            coverage = " · traces" if record.trace_coverage == "full" else ""
            # With lanes in flight the lines interleave: say whose attempt it is.
            whose = f"{scenario.id} " if lanes > 1 else ""
            log(f"   {whose}attempt {i}: {state} ({record.terminal_state.value}{coverage})")
            if lease is not None and lease.lost:
                # Another run holds this tenant now: running more attempts on it would
                # disturb that run's agent conversations. Stop here.
                log(f"   {scenario.id}: tenant {lease.tenant.id} was taken by another run")
                break
        if lease is not None and lease.lost:
            cut = fold_cut_short(scenario, attempts)
            if cut.status is VerdictStatus.SKIPPED_PRECONDITION:
                cut = cut.model_copy(
                    update={
                        "skip_kind": "dependency",
                        "reason": f"tenant lease lost after {len(attempts)} of "
                        f"{scenario.execution.repeats} attempts: another run took the tenant",
                    }
                )
            return cut
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

        # A TaskGroup, not gather: if one lane fails, the others are cancelled (and their
        # leases released) instead of driving the agent for a run already reported failed.
        try:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(lane(sc)) for sc in catalogue.scenarios]
        except ExceptionGroup as failed:
            # Every other lane has been cancelled and its lease released by now. Re-raise
            # the first error itself, so a ConfigError stays a clean exit 3.
            raise failed.exceptions[0] from failed
        verdicts = [t.result() for t in tasks]
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
        usage_events=meter.outside_attempts(),
        config_digest=config_digest(settings),
        load_errors=[str(p) for p in catalogue.problems],
        preflight=health if options.preflight else None,
        stop=stop if stop.active else None,
        stopped=control.stopped,
    )
    # After the verdict exists and before it is written, so the outcomes are part of the
    # saved result. A sink can never change the verdict; publish_all catches everything.
    result = result.model_copy(update={"sinks": await publish_all(sinks, result, log=log)})
    store.save_report(
        run_id,
        render_html(result, store.load_records(run_id), profile, run_dir=store.run_dir(run_id)),
    )
    store.save_junit(run_id, render_junit(result))
    store.save_result(result)  # written last: marks the run as complete
    # After the result is on disk, so a slow or dead collector cannot cost anything.
    await report_telemetry(
        result,
        settings,
        event=TelemetryEvent.RUN,
        transports=[options.transport],
        languages=[
            effective_language(s.persona, scripted=not s.conversation.llm_persona).tag
            or s.persona.language
            for s in catalogue.scenarios
        ],
        trace_source=trace_source.name if trace_source else None,
        demo_target=str(catalogue_target).startswith(str(demo_catalogue_dir())),
        log=log,
    )
    return result
