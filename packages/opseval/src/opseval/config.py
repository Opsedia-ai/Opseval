# SPDX-License-Identifier: Apache-2.0
"""Settings, secrets and the host allowlist.

No other module reads environment variables directly.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from dotenv import dotenv_values
from pydantic import AliasChoices, Field, PrivateAttr, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from opseval.errors import AllowedHostError

REDACTED = "***redacted***"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class Settings(BaseSettings):
    """All ``OPSEVAL_*`` settings. Importing needs no environment."""

    _env_file_used: str | None = PrivateAttr(default=None)
    _dotenv: dict[str, str | None] | None = PrivateAttr(default=None)

    model_config = SettingsConfigDict(
        env_prefix="OPSEVAL_", env_file=".env", extra="ignore", use_attribute_docstrings=True
    )

    allowed_hosts: str = ""
    """Comma-separated hostnames that may receive credentials or conversations."""
    dotenv: bool = True
    """Read ``.env`` from the working directory. Set ``OPSEVAL_DOTENV=false`` in the real
    environment (a ``.env`` cannot switch itself off) to ignore it, as the GitHub action does
    unless asked, so a checked-out repository cannot configure the run (ADR-0058)."""

    # System under test
    static_token: SecretStr | None = None
    """A bearer token sent to the agent as is, when the profile does not mint its own."""
    token_signing_key: SecretStr | None = None
    """The HS256 key for profiles that mint short-lived tokens per attempt (ADR-0012)."""
    credentials: str = ""
    """A credential provider plugin to supply the agent's tokens, by entry-point name
    (``opseval.credentials``, ADR-0046). Installing one changes nothing until it is named
    here. It replaces ``OPSEVAL_STATIC_TOKEN``; a tenant's own ``token_env`` still wins."""

    # LLM roles (OpenAI-compatible endpoints)
    simulator_base_url: str | None = None
    """OpenAI-compatible endpoint of the LLM that plays the customer (host allowlisted)."""
    simulator_api_key: SecretStr | None = None
    """API key for the simulator endpoint, if it needs one."""
    simulator_model: str | None = None
    """Model the simulator uses. Without it, only scripted scenarios run."""
    judge_base_url: str | None = None
    """OpenAI-compatible endpoint of the LLM judges. Its host must be allowlisted."""
    judge_api_key: SecretStr | None = None
    """API key for the judge endpoint, if it needs one."""
    judge_model: str | None = None
    """Model the judges use. Without it, judge scores are not measured."""
    simulator_timeout_seconds: float = Field(default=300.0, gt=0)
    """The most one simulator call (an opening, a reply, a checkpoint decision) may take.
    Simulator time is off the agent's clock; overrunning this is a harness error."""
    judge_concurrency: int = 4
    """How many judge ballots may be in flight at once."""
    judge_independence: Literal["error", "warn", "off"] = "error"
    """What to do when a judge and the model it grades share a vendor family (ADR-0016).
    ``error`` refuses the run at launch (exit 3), ``warn`` scores but marks the results
    degraded, ``off`` skips the check."""

    # Trace source: OTLP-JSON spans on disk (ADR-0028)
    otlp_spans_path: str | None = None
    """OTLP-JSON spans (a file or folder) for `traces.source: otlp-file` (ADR-0028)."""

    # Trace source (Langfuse public API)
    langfuse_host: str | None = None
    """Langfuse base URL, for trace-backed tool checks and score pushing. Must be allowlisted."""
    langfuse_public_key: str | None = None
    """Langfuse public key."""
    langfuse_secret_key: SecretStr | None = None
    """Langfuse secret key."""

    # Telemetry (ADR-0010). No default endpoint: the code ships dark, so nothing is sent
    # until an operator sets one.
    telemetry: Literal["auto", "true", "false"] = "auto"
    """``auto`` means on unless CI or DO_NOT_TRACK says otherwise, and only ever to an
    allowlisted endpoint. ``false`` disables it outright."""
    telemetry_endpoint: str | None = None
    """Where telemetry counts go. None ships: nothing is sent until it is set and allowlisted."""

    # Not OPSEVAL_* but read here anyway, because no other module reads the environment.
    ci: bool = Field(default=False, validation_alias=AliasChoices("CI", "OPSEVAL_CI"))
    """Telemetry is off in CI unless OPSEVAL_TELEMETRY=true is set explicitly."""
    do_not_track: bool = Field(
        default=False, validation_alias=AliasChoices("DO_NOT_TRACK", "OPSEVAL_DO_NOT_TRACK")
    )
    """The community-standard opt-out. Honoured the same as OPSEVAL_TELEMETRY=false."""

    # Sinks: where a finished run is published (ADR-0027). Each host is allowlisted
    # like every other host that receives data.
    webhook_url: SecretStr | None = None
    """Slack, Teams and Discord incoming-webhook URLs are credentials: redacted everywhere."""
    github_issues_repo: str | None = None
    """``owner/name``: open an issue for each failed critical or blocker scenario (ADR-0040)."""
    github_token: SecretStr | None = None
    """Token for the issues sink; needs permission to write issues in that repository only."""
    github_api_url: str = "https://api.github.com"
    """GitHub API base, for GitHub Enterprise. Its host must be allowlisted."""
    github_issues_max: int = Field(default=10, ge=1, le=100)
    """Most issues opened or commented on per run, so a broken agent cannot flood a tracker."""
    jira_url: str | None = None
    """Jira base URL (``https://acme.atlassian.net`` or a Data Center host): open an issue for
    each failed critical or blocker scenario (ADR-0045). Its host must be allowlisted."""
    jira_project: str | None = None
    """Project key the Jira sink writes to, such as ``AGENT``."""
    jira_email: str | None = None
    """Account email for Jira Cloud (basic auth with an API token). Unset for Data Center,
    where the token is a personal access token sent as a bearer."""
    jira_token: SecretStr | None = None
    """Jira API token (Cloud) or personal access token (Data Center)."""
    jira_issue_type: str = "Bug"
    """Issue type the Jira sink creates."""
    jira_issues_max: int = Field(default=10, ge=1, le=100)
    """Most Jira issues opened or commented on per run."""
    sinks: str = ""
    """Plugin sinks to enable, by name (comma-separated). Installing a package never starts
    publishing on its own: a plugin sink runs only when named here."""
    langfuse_push_scores: bool = False
    """Publish each finished run's verdicts to Langfuse as scores (ADR-0027)."""
    langfuse_score_name: str = "opseval"
    """Name prefix for scores pushed to Langfuse."""

    # Gate
    gate_fail_on_flaky: bool = False
    """Make a flaky pass block the gate, whatever the suite's policy says."""

    # Test-tenant pool (FR-RUN-08, ADR-0041). Tokens live in the environment the pool file
    # names, never in the file itself.
    tenants_file: str | None = None
    """A test-tenant pool YAML: each scenario leases one tenant exclusively (ADR-0041)."""
    lanes: int | None = None
    """Requested lanes. Actual = min(tenants, requested, lane_cap)."""
    lane_cap: int = Field(default=8, ge=1, le=64)
    """The most lanes any run may use, however many tenants or lanes are asked for."""
    tenant_lock_dir: str | None = None
    """Where tenant leases live. Default: a machine-wide directory keyed by the pool's tenant
    ids, because a path relative to the working directory would let two checkouts of the same
    pool lease the same account."""
    tenant_stale_after_seconds: float = Field(default=300.0, ge=90, le=86_400)
    """A lease whose heartbeat is older than this may be reclaimed. Generous on purpose, and
    at least three heartbeats (30 s each), or every live lease would look dead."""
    tenant_wait_seconds: float = Field(default=30.0, ge=0, le=3_600)
    """How long to wait for a free tenant before skipping the scenario."""
    reports_dir: str = "reports"
    """Where run folders are written."""
    retention_days: int = Field(default=90, ge=1)
    """``opseval prune`` deletes finished runs older than this many days (FR-SAF-06)."""

    # `opseval serve` (ADR-0031). Every API request must carry this bearer token; when it
    # is unset, the server makes a random one per start and prints the console link.
    serve_token: SecretStr | None = None
    """The console API token. Random on every `opseval serve` start when unset (ADR-0031)."""
    console_dir: str | None = None
    """The built web console (``packages/console/dist``) for ``opseval serve`` to serve.
    When unset, a source checkout's build is found on its own (ADR-0034)."""

    # Server mode on Postgres, the Langfuse model (ADR-0047).
    database_url: SecretStr | None = None
    """Postgres for server mode: ``postgresql://user:password@host:5432/opseval``. It carries
    a password, so it is a secret. Its host must be allowlisted, and a host other than
    localhost needs TLS (``?ssl=require``, ``verify-ca`` or ``verify-full``)."""

    def __init__(self, **values: Any) -> None:
        if "_env_file" not in values and _dotenv_off():
            values["_env_file"] = None
        super().__init__(**values)
        # The .env this instance was actually built from: an explicit ``_env_file`` (None
        # included) wins over the class default, so secret_named reads the same file.
        chosen = values.get("_env_file", self.model_config.get("env_file"))
        self._env_file_used = str(chosen) if isinstance(chosen, str | Path) else None
        self._dotenv: dict[str, str | None] | None = None

    def secret_named(self, name: str) -> str | None:
        """A secret named by a *file* rather than by a field: a tenant's ``token_env``.

        This lives here because ``config`` is the only module that reads the environment,
        and because of a trap: ``.env`` is read into this model but is **never exported to
        ``os.environ``**. A tenant token set in ``.env``, exactly as ``opseval init`` says,
        would look unset to anything calling ``os.environ.get``. So both are consulted, with
        the real environment winning. The ``.env`` read is the one this instance was built
        from, parsed once.
        """
        from_env = os.environ.get(name)
        if from_env:
            return from_env
        if self._dotenv is None:
            path = Path(self._env_file_used) if self._env_file_used else None
            self._dotenv = dotenv_values(path) if path is not None and path.is_file() else {}
        return self._dotenv.get(name) or None

    def allowlist(self) -> set[str]:
        return {h.strip().lower() for h in self.allowed_hosts.split(",") if h.strip()}


def _dotenv_off() -> bool:
    """``OPSEVAL_DOTENV`` set false in the real environment: no ``.env`` is read at all."""
    flag = os.environ.get("OPSEVAL_DOTENV", "").strip().lower()
    return flag in {"0", "false", "no", "off"}


SECRET_FIELDS = (
    "static_token",
    "token_signing_key",
    "simulator_api_key",
    "judge_api_key",
    "langfuse_secret_key",
    "serve_token",
    "webhook_url",
    "github_token",
    "jira_token",
    "database_url",
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
    # Only the scheme and host are ever repeated: a webhook URL's path is its secret, and a
    # URL can carry credentials in its user-info or query string.
    if not host:
        raise AllowedHostError(
            f"URL has no host (scheme {parsed.scheme or 'missing'!r}); expected https://host/..."
        )
    if host not in allowlist:
        raise AllowedHostError(
            f"host {host!r} is not in OPSEVAL_ALLOWED_HOSTS; add it explicitly to allow it"
        )
    if parsed.scheme not in secure_schemes and not _is_local(host):
        raise AllowedHostError(
            f"{parsed.scheme}://{host} must use one of {secure_schemes} "
            "(plain schemes are allowed only on localhost)"
        )


TLS_VERIFIED = ("verify-ca", "verify-full")
"""``ssl`` modes that check the server's certificate, as HTTPS does for every other destination.
``require`` encrypts but accepts any certificate, so anyone on the path can answer in the
database's place; ``prefer`` and ``allow`` can fall back to plain text."""


def check_database_host(host: str, ssl_modes: list[str], allowlist: set[str]) -> None:
    """Raise :class:`AllowedHostError` unless a database at ``host`` may receive data.

    The rule every destination follows (``check_url_allowed``): the host is allowlisted, and
    anything but loopback is encrypted *and authenticated*. Postgres has no TLS-only URL scheme,
    so TLS is read from the ``ssl`` modes the connection will use. The URL itself, which
    carries a password, is never repeated. ``opseval.db.engine`` decides the host and modes from
    the URL exactly as the driver will, so this cannot be shown one host and connect to another.
    """
    host = host.lower()
    if not host:
        raise AllowedHostError("database URL has no host; use postgresql://…@host:5432/db")
    if host not in allowlist:
        raise AllowedHostError(
            f"database host {host!r} is not in OPSEVAL_ALLOWED_HOSTS; add it explicitly to allow it"
        )
    if _is_local(host):
        return
    if not ssl_modes or any(m not in TLS_VERIFIED for m in ssl_modes):
        raise AllowedHostError(
            f"postgresql://{host} must use verified TLS: add ?ssl=verify-full (and "
            "&sslrootcert=/path/ca.pem for a private CA, such as a cloud provider's); "
            "plain or unverified connections are allowed only on localhost"
        )
