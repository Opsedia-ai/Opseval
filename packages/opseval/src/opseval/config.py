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

    # Speech providers for voice (ADR-0054). Both receive conversation content: the
    # customer's script goes to TTS, the agent's voice to STT. Every URL is allowlisted.
    tts_provider: str = "openai"
    """The text-to-speech provider: ``openai`` (the built-in OpenAI-compatible
    ``/v1/audio/speech`` client) or the name of an ``opseval.tts`` plugin. Only the named one
    is imported; a name nothing provides is refused (exit 3), never replaced by another."""
    tts_url: str | None = None
    """Base URL of the TTS provider (``{url}/v1/audio/speech`` for the built-in). Required for
    every provider, plugin included; its host must be allowlisted, and use TLS unless it is
    loopback."""
    tts_key: SecretStr | None = None
    """API key for the TTS provider, if it needs one."""
    tts_model: str | None = None
    """TTS model, such as ``tts-1`` or ``gpt-4o-mini-tts``."""
    tts_voice_map: dict[str, str] = {}
    """JSON object from a persona's voice name, or a language tag, to the provider's voice:
    ``{"calm_female": "nova", "ar": "onyx", "default": "alloy"}``. A voice is looked up by
    persona voice, then language, then ``default``; an unmapped persona voice is sent as is."""
    tts_format: Literal["wav", "pcm"] = "wav"
    """Audio format asked of the built-in TTS: ``wav`` (the rate is in the file) or ``pcm``
    (raw PCM16 at ``OPSEVAL_TTS_PCM_RATE``)."""
    tts_pcm_rate: int = Field(default=24_000, gt=0)
    """The sample rate of raw ``pcm`` TTS audio (OpenAI sends 24 kHz)."""
    tts_timeout_seconds: float = Field(default=30.0, gt=0)
    """The most one TTS call may take, retries included. Off the agent's clock; overrunning it
    is a harness error (``TtsError``), never the agent's timeout."""
    stt_provider: str = "openai"
    """The speech-to-text provider: ``openai`` (the built-in OpenAI-compatible
    ``/v1/audio/transcriptions`` client) or the name of an ``opseval.stt`` plugin."""
    stt_url: str | None = None
    """Base URL of the STT provider (``{url}/v1/audio/transcriptions`` for the built-in).
    Required for every provider, plugin included. Allowlisted, TLS off loopback."""
    stt_key: SecretStr | None = None
    """API key for the STT provider, if it needs one."""
    stt_model: str | None = None
    """STT model, such as ``whisper-1``."""
    stt_timeout_seconds: float = Field(default=60.0, gt=0)
    """The most one STT call may take, retries included. Off the agent's clock; overrunning it
    is a harness error (``SttError``), never the agent's timeout (ADR-0053)."""
    stt_min_confidence: float = Field(default=0.5, ge=0, le=1)
    """A transcript below this confidence marks the attempt's judge scores degraded
    (ADR-0053). A provider that reports no confidence is not measured, never 1.0."""
    stt_canary_max_wer: float = Field(default=0.25, ge=0)
    """The STT health canary is healthy when its word error rate is at most this (ADR-0053).
    Calibrated per language pack later (ADR-0059)."""
    stt_canary_wav: str | None = None
    """A WAV of known speech for the STT canary when no TTS is configured to make one. Its
    words go in ``OPSEVAL_STT_CANARY_TEXT`` and its language in
    ``OPSEVAL_STT_CANARY_LANGUAGE``; one without the others is refused (exit 3). Without a
    TTS or this WAV, the canary is not run."""
    stt_canary_text: str | None = None
    """What ``OPSEVAL_STT_CANARY_WAV`` says, word for word."""
    stt_canary_language: str | None = None
    """The language ``OPSEVAL_STT_CANARY_WAV`` is spoken in, as a BCP-47 tag (``en``,
    ``ar-EG``). The STT is asked to hear the canary in this language, not the scenario's."""
    stt_second_provider: str | None = None
    """An optional second STT check (ADR-0053): ``openai`` or an ``opseval.stt`` plugin. It
    must differ from the first in provider or model, or the run is refused (exit 3): the same
    STT twice proves nothing. Setting a second URL or model alone implies ``openai``."""
    stt_second_url: str | None = None
    """Base URL of the second STT. Allowlisted and preflighted like the first."""
    stt_second_key: SecretStr | None = None
    """API key for the second STT, if it needs one."""
    stt_second_model: str | None = None
    """Model of the second STT."""

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


SECRET_FIELDS = (
    "static_token",
    "token_signing_key",
    "simulator_api_key",
    "judge_api_key",
    "tts_key",
    "stt_key",
    "stt_second_key",
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
