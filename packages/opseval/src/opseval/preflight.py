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
from urllib.parse import urlparse

import httpx
import websockets
from websockets.exceptions import InvalidStatus, WebSocketException

from opseval.auth import HS256Tokens
from opseval.config import Settings, check_url_allowed
from opseval.errors import ConfigError
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
        if not scenario.conversation.scripted_turns:
            counts["simulator"] += 1
    return counts


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


def check_token(profile: SutProfile, settings: Settings) -> HealthCheck:
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
    try:
        # Not even a health probe goes to a host the operator did not allowlist.
        check_url_allowed(base, settings.allowlist(), secure_schemes=("https",))
    except ConfigError as exc:
        return HealthCheck(
            name=role, status=CheckStatus.UNAVAILABLE, detail=str(exc), needed_by=needed_by
        )
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
    ok, detail = await _probe_http(host)
    return HealthCheck(
        name="traces",
        status=CheckStatus.OK if ok else CheckStatus.UNAVAILABLE,
        detail=detail,
        needed_by=needed_by,
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
    return PreflightReport(checks=[sut, check_token(profile, settings), simulator, judge, traces])


__all__ = [
    "check_llm",
    "check_token",
    "check_traces",
    "preflight",
    "probe_target",
]
