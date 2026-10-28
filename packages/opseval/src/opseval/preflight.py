# SPDX-License-Identifier: Apache-2.0
"""Preflight health checks (FR-CTL, ADR-0020).

Checks the things a suite depends on *before* any conversation happens, so an outage
becomes ``skipped_precondition`` instead of a wall of failures or "not judged" verdicts.

This is the core rule applied ahead of time. If the agent is unreachable, every scenario
would otherwise burn its repeats producing transport errors; if the judge endpoint is
down, judge-only scenarios would each report ``JUDGE_UNAVAILABLE``. Both are harness
problems, and saying so once, up front, is both cheaper and more honest.

What preflight will **not** do is guess. A dependency it cannot reach is reported as
unavailable; a dependency nothing in the suite needs is reported as not needed; and a
check it cannot perform (proving an API key without spending tokens) says so rather than
implying the key is good.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlparse

import httpx
import websockets
from websockets.exceptions import InvalidStatus, WebSocketException

from opseval.auth import CredentialContext, GuardedTokens, HS256Tokens
from opseval.config import Settings, check_url_allowed
from opseval.errors import CredentialsError
from opseval.models.preflight import CheckStatus, HealthCheck, PreflightReport
from opseval.models.profile import SutProfile
from opseval.models.scenario import Scenario
from opseval.scoring import REGISTRY, effective_specs

PROBE_TIMEOUT = 5.0


def _requirements(scenarios: Iterable[Scenario]) -> dict[str, int]:
    """How many scenarios would be left with nothing measurable without each dependency.

    A scenario with both deterministic and judge scorers still produces a real verdict
    without the judge, so it does not count: skipping it would throw away the checks that
    *can* run, which is its own kind of dishonesty.
    """
    counts = {"llm_scorers": 0, "traces": 0, "simulator": 0}
    for scenario in scenarios:
        if scenario.is_blocked:
            continue
        requires = [
            cls.requires
            for spec in effective_specs(scenario)
            if (cls := REGISTRY.get(spec.id)) is not None
        ]
        if requires and all(r == "llm" for r in requires):
            counts["llm_scorers"] += 1
        if requires and all(r == "trace" for r in requires):
            counts["traces"] += 1
        if scenario.conversation.llm_persona:
            counts["simulator"] += 1
    return counts


def llm_roles_needed(scenarios: Iterable[Scenario]) -> set[str]:
    """The LLM roles (``simulator``, ``judge``) at least one runnable scenario uses.

    Only these are built: a suite of scripted, deterministic scenarios sends nothing to an
    LLM, so an endpoint it never uses must not stop it.
    """
    roles: set[str] = set()
    for scenario in scenarios:
        if scenario.is_blocked:
            continue
        if scenario.conversation.llm_persona:
            roles.add("simulator")
        if any(
            (cls := REGISTRY.get(spec.id)) is not None and cls.requires == "llm"
            for spec in effective_specs(scenario)
        ):
            roles.add("judge")
    return roles


async def _probe_http(url: str) -> tuple[bool, str]:
    """Any HTTP answer proves reachability, including 401 or 404."""
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, f"answered HTTP {response.status_code}"


async def _probe_websocket(url: str) -> tuple[bool, str]:
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            connection = await websockets.connect(url, open_timeout=PROBE_TIMEOUT)
        await connection.close()
    except InvalidStatus as exc:
        # The server spoke HTTP and refused the upgrade: it is up, and that is enough.
        return True, f"refused the upgrade with HTTP {exc.response.status_code} (host is up)"
    except (WebSocketException, OSError, TimeoutError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, "accepted a WebSocket connection"


async def _already_ok(name: str, detail: str) -> HealthCheck:
    return HealthCheck(name=name, status=CheckStatus.OK, detail=detail)


def _check_target_allowed(target: str, settings: Settings) -> None:
    """The agent's host is allowlisted like any other: the probe is the first thing sent."""
    scheme = (urlparse(target).scheme or "").lower()
    if scheme in ("ws", "wss"):
        check_url_allowed(target, settings.allowlist(), secure_schemes=("wss",))
    elif scheme in ("http", "https"):
        check_url_allowed(target, settings.allowlist(), secure_schemes=("https",))


async def probe_target(target: str, transport: str) -> HealthCheck:
    scheme = (urlparse(target).scheme or "").lower()
    if transport == "python" or scheme in ("", "python"):
        return HealthCheck(name="sut", status=CheckStatus.OK, detail="in-process callable")
    if scheme in ("ws", "wss"):
        ok, detail = await _probe_websocket(target)
    elif scheme in ("http", "https"):
        ok, detail = await _probe_http(target)
    else:
        return HealthCheck(
            name="sut",
            status=CheckStatus.UNAVAILABLE,
            detail=f"unsupported target scheme {scheme!r}",
        )
    return HealthCheck(
        name="sut",
        status=CheckStatus.OK if ok else CheckStatus.UNAVAILABLE,
        detail=detail,
    )


def check_token(profile: SutProfile, settings: Settings, *, target: str = "") -> HealthCheck:
    if settings.credentials.strip():
        return _check_provider(profile, settings, target)
    if profile.token is None:
        detail = (
            "static token from OPSEVAL_STATIC_TOKEN"
            if settings.static_token
            else "no credentials configured"
        )
        return HealthCheck(name="token", status=CheckStatus.NOT_NEEDED, detail=detail)
    key = settings.token_signing_key
    if key is None:
        return HealthCheck(
            name="token",
            status=CheckStatus.NOT_CONFIGURED,
            detail=f"profile {profile.name!r} mints HS256 tokens: set OPSEVAL_TOKEN_SIGNING_KEY",
        )
    try:
        HS256Tokens(
            key.get_secret_value(),
            ttl_seconds=profile.token.ttl_seconds,
            refresh_before_seconds=profile.token.refresh_before_seconds,
            claims=profile.token.claims,
        ).current()
    except Exception as exc:  # noqa: BLE001 - any minting failure is a setup problem
        return HealthCheck(
            name="token",
            status=CheckStatus.NOT_CONFIGURED,
            detail=f"cannot mint a token: {type(exc).__name__}: {exc}",
        )
    return HealthCheck(
        name="token",
        status=CheckStatus.OK,
        detail=f"minted an HS256 token (ttl {profile.token.ttl_seconds}s)",
    )


async def check_llm(
    role: str, settings: Settings, needed_by: int, *, verify: bool = False
) -> HealthCheck:
    base = getattr(settings, f"{role}_base_url")
    model = getattr(settings, f"{role}_model")
    if needed_by == 0:
        return HealthCheck(
            name=role,
            status=CheckStatus.NOT_NEEDED,
            detail="no scenario in this suite depends on it",
        )
    if not base or not model:
        return HealthCheck(
            name=role,
            status=CheckStatus.NOT_CONFIGURED,
            detail=f"set OPSEVAL_{role.upper()}_BASE_URL and OPSEVAL_{role.upper()}_MODEL",
            needed_by=needed_by,
        )
    # Not even a health probe goes to a host the operator did not allowlist. That is a
    # configuration error (exit 3), not an outage that would only skip scenarios.
    check_url_allowed(base, settings.allowlist(), secure_schemes=("https",))
    ok, detail = await _probe_http(base)
    if not ok:
        return HealthCheck(
            name=role, status=CheckStatus.UNAVAILABLE, detail=detail, needed_by=needed_by
        )
    if not verify:
        return HealthCheck(
            name=role,
            status=CheckStatus.UNVERIFIED,
            detail=(
                f"{base} is reachable and {model!r} is configured, but the key was not "
                "tested (pass --verify-llm to spend one token proving it)"
            ),
            needed_by=needed_by,
        )
    return await _verify_llm(role, settings, needed_by)


async def _verify_llm(role: str, settings: Settings, needed_by: int) -> HealthCheck:
    """Prove the credentials work with the smallest possible completion."""
    from opseval.llm import UsageMeter, client_for_role

    client = client_for_role(settings, role, UsageMeter())
    if client is None:  # pragma: no cover - guarded by the caller
        return HealthCheck(
            name=role,
            status=CheckStatus.NOT_CONFIGURED,
            detail="not configured",
            needed_by=needed_by,
        )
    try:
        await client.complete([{"role": "user", "content": "ping"}], max_tokens=1)
    except Exception as exc:  # noqa: BLE001 - any failure here is a setup problem
        return HealthCheck(
            name=role,
            status=CheckStatus.UNAVAILABLE,
            detail=f"{type(exc).__name__}: {exc}",
            needed_by=needed_by,
        )
    return HealthCheck(
        name=role,
        status=CheckStatus.OK,
        detail=f"{client.model!r} answered a test request",
        needed_by=needed_by,
    )


async def check_traces(
    profile: SutProfile, settings: Settings, needed_by: int, *, enabled: bool
) -> HealthCheck:
    if profile.traces is None or not enabled:
        return HealthCheck(
            name="traces",
            status=CheckStatus.NOT_NEEDED,
            detail="the profile declares no trace source" if profile.traces is None else "disabled",
        )
    if profile.traces.source == "otlp-file":
        # Read from disk, never over the network: there is no host to allowlist or probe.
        path = settings.otlp_spans_path
        if not path:
            return HealthCheck(
                name="traces",
                status=CheckStatus.NOT_CONFIGURED,
                detail="set OPSEVAL_OTLP_SPANS_PATH, or run with --no-traces",
                needed_by=needed_by,
            )
        found = Path(path).exists()
        return HealthCheck(
            name="traces",
            status=CheckStatus.OK if found else CheckStatus.UNAVAILABLE,
            detail=f"OTLP spans at {path}" if found else f"{path} does not exist",
            needed_by=needed_by,
        )
    host = settings.langfuse_host
    if not (host and settings.langfuse_public_key and settings.langfuse_secret_key):
        return HealthCheck(
            name="traces",
            status=CheckStatus.NOT_CONFIGURED,
            detail=(
                "set OPSEVAL_LANGFUSE_HOST, OPSEVAL_LANGFUSE_PUBLIC_KEY and "
                "OPSEVAL_LANGFUSE_SECRET_KEY, or run with --no-traces"
            ),
            needed_by=needed_by,
        )
    check_url_allowed(host, settings.allowlist(), secure_schemes=("https",))
    ok, detail = await _probe_http(host)
    return HealthCheck(
        name="traces",
        status=CheckStatus.OK if ok else CheckStatus.UNAVAILABLE,
        detail=detail,
        needed_by=needed_by,
    )


def _check_provider(profile: SutProfile, settings: Settings, target: str) -> HealthCheck:
    """Ask the credential provider plugin for one token, as an attempt would (ADR-0046).

    A plugin that is missing or broken raises :class:`ConfigError`, as it would at launch.
    """
    from opseval.plugins import load_credential_provider

    name = settings.credentials.strip()
    if profile.token is not None:
        return HealthCheck(
            name="token",
            status=CheckStatus.NOT_CONFIGURED,
            detail=f"profile {profile.name!r} mints HS256 tokens and OPSEVAL_CREDENTIALS "
            f"names {name!r}: choose one",
        )
    provider = load_credential_provider(name)
    context = CredentialContext(
        profile=profile.name, target=target, tenant=None, value=settings.secret_named
    )
    try:
        GuardedTokens(provider, context).current()
    except CredentialsError as exc:
        return HealthCheck(name="token", status=CheckStatus.NOT_CONFIGURED, detail=str(exc))
    return HealthCheck(
        name="token", status=CheckStatus.OK, detail=f"credential provider {name!r} gave a token"
    )


async def preflight(
    scenarios: Iterable[Scenario],
    profile: SutProfile,
    settings: Settings,
    *,
    target: str,
    transport: str,
    traces_enabled: bool = True,
    verify_llm: bool = False,
    probe_target_host: bool = True,
) -> PreflightReport:
    """Check everything this suite depends on, concurrently.

    ``probe_target_host`` is off when a transport factory was injected: there is no host
    to reach, and probing one would invent a failure the run would never have hit.
    """
    listed = list(scenarios)
    needs = _requirements(listed)
    # Before anything is probed, so a refused host never leaves other probes running. Every
    # LLM role the suite uses is checked, including one whose outage would skip nothing.
    if probe_target_host and transport != "python":
        _check_target_allowed(target, settings)
    for role in sorted(llm_roles_needed(listed)):
        base = getattr(settings, f"{role}_base_url")
        if base:
            check_url_allowed(base, settings.allowlist(), secure_schemes=("https",))
    probe = (
        probe_target(target, transport)
        if probe_target_host
        else _already_ok("sut", "transport supplied by the caller")
    )
    sut, simulator, judge, traces = await asyncio.gather(
        probe,
        check_llm("simulator", settings, needs["simulator"], verify=verify_llm),
        check_llm("judge", settings, needs["llm_scorers"], verify=verify_llm),
        check_traces(profile, settings, needs["traces"], enabled=traces_enabled),
    )
    return PreflightReport(
        checks=[sut, check_token(profile, settings, target=target), simulator, judge, traces]
    )


__all__ = [
    "check_llm",
    "check_token",
    "check_traces",
    "llm_roles_needed",
    "preflight",
    "probe_target",
]
