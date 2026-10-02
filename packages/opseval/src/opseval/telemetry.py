# SPDX-License-Identifier: Apache-2.0
"""Opt-out, content-free, documented telemetry (ADR-0010).

**This ships dark.** There is no default endpoint, so nothing is sent until an operator
sets ``OPSEVAL_TELEMETRY_ENDPOINT``, and that host must be in ``OPSEVAL_ALLOWED_HOSTS``
like every other host that receives data. ADR-0010 leaves the operator and the public
launch timing open; this module implements the design without making either choice.

Three rules shape the code:

1. **Only the documented fields leave the machine.** The payload is a model with
   ``extra="forbid"``, built from a :class:`SuiteResult` and never from records, so a
   transcript cannot reach it even by accident. Counts are bucketed.
2. **It cannot affect the run.** One POST, a short timeout, no retries, no disk queue,
   every exception swallowed. It is sent after the result is written and can never change
   an exit code or raise.
3. **It is easy to see and to stop.** ``opseval telemetry show`` prints the exact JSON.
   ``OPSEVAL_TELEMETRY=false``, ``DO_NOT_TRACK=1``, ``CI=true`` or
   ``opseval telemetry disable`` each turn it off, and the first run that would send
   prints a notice and sends nothing.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from opseval import __version__
from opseval.config import Settings, check_url_allowed
from opseval.errors import AllowedHostError
from opseval.models.results import ScoreStatus, SuiteResult
from opseval.models.telemetry import TelemetryEvent, TelemetryPayload, bucket

POST_TIMEOUT = 2.0

BUILTIN_TRANSPORTS = frozenset({"websocket", "http-json", "sse", "openai-chat", "python"})
BUILTIN_TRACE_SOURCES = frozenset({"langfuse"})
CUSTOM = "custom"
"""Stands in for any name that is not ours: a third-party transport or scorer id could
identify a company, so only built-in names are ever sent."""


@dataclass(frozen=True)
class Decision:
    """Whether to send, and the reason either way (which ``telemetry show`` prints)."""

    send: bool
    reason: str
    endpoint: str | None = None
    first_run: bool = False


def config_path() -> Path:
    """Where the install id and the local opt-out live."""
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "opseval" / "config.json"


def _read_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_config(path: Path, config: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    except OSError:
        # A read-only home must not break a run. The cost is a new install id next time.
        pass


def install_id(path: Path | None = None) -> str:
    """A random id for this install, created on first use. Not derived from anything.

    Deriving it from a hostname, a MAC address or a path would make it a fingerprint, so
    it is a plain UUID4 with nothing of the machine in it.
    """
    file = path or config_path()
    config = _read_config(file)
    existing = config.get("install_id")
    if isinstance(existing, str) and existing:
        return existing
    fresh = str(uuid.uuid4())
    _write_config(file, {**config, "install_id": fresh, "notice_shown": False})
    return fresh


def set_enabled(enabled: bool, path: Path | None = None) -> Path:
    """Record the local opt-out (``opseval telemetry enable`` / ``disable``)."""
    file = path or config_path()
    config = _read_config(file)
    _write_config(file, {**config, "telemetry": enabled})
    return file


def decide(settings: Settings, path: Path | None = None) -> Decision:
    """Whether this run may send, and why. Checked before a payload is even built."""
    if settings.telemetry == "false":
        return Decision(False, "disabled by OPSEVAL_TELEMETRY=false")
    if settings.do_not_track and settings.telemetry != "true":
        return Decision(False, "disabled by DO_NOT_TRACK")
    file = path or config_path()
    config = _read_config(file)
    if config.get("telemetry") is False and settings.telemetry != "true":
        return Decision(False, f"disabled locally in {file}")
    if settings.ci and settings.telemetry != "true":
        return Decision(False, "off in CI (set OPSEVAL_TELEMETRY=true to send from CI)")
    endpoint = settings.telemetry_endpoint
    if not endpoint:
        return Decision(False, "no endpoint configured, so nothing is sent")
    try:
        check_url_allowed(endpoint, settings.allowlist(), secure_schemes=("https",))
    except AllowedHostError as exc:
        # ADR-0010 decision 1, option A: the telemetry host is allowlisted like any
        # other, which makes telemetry opt-in in practice. Deliberate while the project
        # is private.
        return Decision(False, f"endpoint not allowlisted: {exc}")
    if not config.get("notice_shown"):
        return Decision(False, "first run: printing the notice instead", endpoint, first_run=True)
    return Decision(True, "enabled", endpoint)


def mark_notice_shown(path: Path | None = None) -> None:
    file = path or config_path()
    _write_config(file, {**_read_config(file), "notice_shown": True})


def notice(endpoint: str) -> str:
    return (
        "Opseval sends anonymous, content-free usage counts (no conversations, scenarios, "
        f"prompts, hosts or keys) to {endpoint}.\n"
        "Nothing was sent this time. See exactly what it would send with "
        "`opseval telemetry show`.\n"
        "Turn it off with `opseval telemetry disable`, OPSEVAL_TELEMETRY=false or "
        "DO_NOT_TRACK=1."
    )


def _known(names: list[str], allowed: frozenset[str]) -> list[str]:
    return sorted({n if n in allowed else CUSTOM for n in names})


def build_payload(
    result: SuiteResult,
    *,
    event: TelemetryEvent,
    install: str,
    transports: list[str],
    languages: list[str],
    trace_source: str | None = None,
    demo_target: bool = False,
    ci: bool = False,
    scorer_registry: frozenset[str] | None = None,
) -> TelemetryPayload:
    """Build the payload from the result only. Records are never read."""
    from opseval.scoring import REGISTRY

    known_scorers = scorer_registry if scorer_registry is not None else frozenset(REGISTRY)
    attempts = [a for t in result.tests for a in t.attempts]
    judged = [
        a
        for a in attempts
        if any(s.axis.agent_facing and s.status is not ScoreStatus.INFRA_ERROR for s in a.scores)
    ]
    scorers = [s.scorer_id for a in attempts for s in a.scores]
    return TelemetryPayload(
        install_id=install,
        event=event,
        opseval_version=__version__,
        python=f"{sys.version_info.major}.{sys.version_info.minor}",
        os=platform.system(),
        arch=platform.machine(),
        ci=ci,
        scenarios=bucket(len(result.tests)),
        attempts=bucket(len(attempts)),
        judged_attempts=bucket(len(judged)),
        exit_code=result.exit_code,
        gate=result.gate.status,
        transports=_known(transports, BUILTIN_TRANSPORTS),
        scorers=_known(scorers, frozenset(known_scorers)),
        trace_source=(
            "none"
            if not trace_source
            else (trace_source if trace_source in BUILTIN_TRACE_SOURCES else CUSTOM)
        ),
        languages=sorted(set(languages)),
        demo_target=demo_target,
    )


async def send(payload: TelemetryPayload, endpoint: str) -> bool:
    """Fire and forget. Returns whether it went, and never raises."""
    try:
        async with httpx.AsyncClient(timeout=POST_TIMEOUT) as client:
            response = await client.post(
                endpoint, json=payload.model_dump(by_alias=True, mode="json")
            )
        return response.status_code < 400
    except Exception:  # noqa: BLE001 - telemetry must never affect a run
        return False


async def report(
    result: SuiteResult,
    settings: Settings,
    *,
    event: TelemetryEvent,
    transports: list[str],
    languages: list[str],
    trace_source: str | None = None,
    demo_target: bool = False,
    log: Any = None,
    path: Path | None = None,
) -> Decision:
    """Send one event if allowed. Swallows everything; never changes the exit code."""
    try:
        decision = decide(settings, path)
        if decision.first_run and decision.endpoint:
            if log is not None:
                log(notice(decision.endpoint))
            mark_notice_shown(path)
            return decision
        if not decision.send or decision.endpoint is None:
            return decision
        payload = build_payload(
            result,
            event=event,
            install=install_id(path),
            transports=transports,
            languages=languages,
            trace_source=trace_source,
            demo_target=demo_target,
            ci=settings.ci,
        )
        await send(payload, decision.endpoint)
        return decision
    except Exception as exc:  # noqa: BLE001 - never let telemetry break a run
        return Decision(False, f"telemetry skipped after an internal error: {exc}")


__all__ = [
    "BUILTIN_TRACE_SOURCES",
    "BUILTIN_TRANSPORTS",
    "CUSTOM",
    "Decision",
    "build_payload",
    "config_path",
    "decide",
    "install_id",
    "mark_notice_shown",
    "notice",
    "report",
    "send",
    "set_enabled",
]
