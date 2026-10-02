# SPDX-License-Identifier: Apache-2.0
"""Settings, secrets and the host allowlist.

No other module reads environment variables directly.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from opseval.errors import AllowedHostError

REDACTED = "***redacted***"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class Settings(BaseSettings):
    """All ``OPSEVAL_*`` settings. Importing needs no environment."""

    model_config = SettingsConfigDict(env_prefix="OPSEVAL_", env_file=".env", extra="ignore")

    allowed_hosts: str = ""
    """Comma-separated hostnames that may receive credentials or conversations."""

    # System under test
    static_token: SecretStr | None = None
    token_signing_key: SecretStr | None = None

    # LLM roles (OpenAI-compatible endpoints)
    simulator_base_url: str | None = None
    simulator_api_key: SecretStr | None = None
    simulator_model: str | None = None
    judge_base_url: str | None = None
    judge_api_key: SecretStr | None = None
    judge_model: str | None = None
    judge_concurrency: int = 4
    judge_independence: Literal["error", "warn", "off"] = "error"
    """What to do when a judge and the model it grades share a vendor family (ADR-0016).
    ``error`` refuses the run at launch (exit 3), ``warn`` scores but marks the results
    degraded, ``off`` skips the check."""

    # Trace source (Langfuse public API)
    langfuse_host: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None

    # Telemetry (ADR-0010). No default endpoint: the code ships dark, so nothing is sent
    # until an operator sets one.
    telemetry: Literal["auto", "true", "false"] = "auto"
    """``auto`` means on unless CI or DO_NOT_TRACK says otherwise, and only ever to an
    allowlisted endpoint. ``false`` disables it outright."""
    telemetry_endpoint: str | None = None

    # Not OPSEVAL_* but read here anyway, because no other module reads the environment.
    ci: bool = Field(default=False, validation_alias=AliasChoices("CI", "OPSEVAL_CI"))
    """Telemetry is off in CI unless OPSEVAL_TELEMETRY=true is set explicitly."""
    do_not_track: bool = Field(
        default=False, validation_alias=AliasChoices("DO_NOT_TRACK", "OPSEVAL_DO_NOT_TRACK")
    )
    """The community-standard opt-out. Honoured the same as OPSEVAL_TELEMETRY=false."""

    # Gate
    gate_fail_on_flaky: bool = False

    reports_dir: str = "reports"

    def allowlist(self) -> set[str]:
        return {h.strip().lower() for h in self.allowed_hosts.split(",") if h.strip()}


SECRET_FIELDS = (
    "static_token",
    "token_signing_key",
    "simulator_api_key",
    "judge_api_key",
    "langfuse_secret_key",
)


def config_digest(settings: Settings) -> dict[str, Any]:
    """The only sanctioned snapshot of configuration: every secret is redacted."""
    out: dict[str, Any] = {}
    for name, value in settings.model_dump().items():
        if isinstance(value, SecretStr) or name in SECRET_FIELDS:
            out[name] = None if value is None else REDACTED
        else:
            out[name] = value
    return out


def _is_local(host: str) -> bool:
    if host in LOCAL_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_url_allowed(url: str, allowlist: set[str], *, secure_schemes: tuple[str, ...]) -> None:
    """Raise :class:`AllowedHostError` unless ``url`` may receive credentials.

    The host must be allowlisted. Remote hosts must use a TLS scheme; plain schemes are
    accepted only for (allowlisted) loopback addresses.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        raise AllowedHostError(f"URL has no host: {url!r}")
    if host not in allowlist:
        raise AllowedHostError(
            f"host {host!r} is not in OPSEVAL_ALLOWED_HOSTS; add it explicitly to allow it"
        )
    if parsed.scheme not in secure_schemes and not _is_local(host):
        raise AllowedHostError(
            f"{url!r} must use one of {secure_schemes} (plain schemes are allowed only on "
            "localhost)"
        )
