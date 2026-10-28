# SPDX-License-Identifier: Apache-2.0
"""Exception hierarchy.

The split matters: anything under :class:`HarnessError` is a problem with Opseval,
its configuration or its dependencies, and must never be reported as an agent failure.
"""

from __future__ import annotations


class OpsevalError(Exception):
    """Base class for all Opseval errors."""


class ConfigError(OpsevalError):
    """Invalid configuration, catalogue or profile. Maps to exit code 3."""


class AllowedHostError(ConfigError):
    """A credential would be sent to a host that is not allowlisted, or without TLS."""


class HarnessError(OpsevalError):
    """A failure of the harness itself (never the agent's fault)."""


class TransportError(HarnessError):
    """Could not open or keep a session with the system under test."""


class TransportTimeout(OpsevalError):
    """The system under test did not answer in time. This *does* count against the agent."""


class TransportClosed(TransportError):
    """The connection closed unexpectedly."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class SimulatorError(HarnessError):
    """The customer simulator could not produce a valid turn or decision."""


class LLMInfraError(HarnessError):
    """The LLM provider failed (outage, rate limit exhausted, malformed response)."""


class RateLimited(Exception):
    """Marks an error caused by a rate limit that outlasted its retries.

    Mixed in rather than a branch of its own, so each error keeps its place in the core
    rule (a harness failure either way); it lets ``stop_on_rate_limit`` see the cause
    without reading error text (ADR-0035).
    """


class TransportRateLimited(TransportError, RateLimited):
    """The agent's endpoint kept answering HTTP 429."""


class LLMRateLimited(LLMInfraError, RateLimited):
    """An LLM role kept answering HTTP 429."""


class CredentialsError(HarnessError):
    """A credential provider could not supply a token. The harness's problem, never the
    agent's: no request reached the agent without credentials."""


class TraceUnavailable(HarnessError):
    """The agent's trace could not be read (source down, not ingested yet, no session id)."""


class LLMRequestError(HarnessError):
    """The LLM provider rejected the request (4xx other than 429)."""


class TtsError(HarnessError):
    """Text-to-speech failed: unreachable, 5xx, over its own timeout, or audio Opseval cannot
    decode (ADR-0054). Opseval could not speak, so the attempt is not judged."""


class SttError(HarnessError):
    """Speech-to-text failed: unreachable, 5xx, over its own timeout, or a body Opseval cannot
    read (ADR-0054). Opseval could not listen, so the attempt is not judged. An *empty*
    transcript is not this error: it is evidence the STT canary decides (ADR-0053)."""


class TtsRateLimited(TtsError, RateLimited):
    """The TTS provider kept answering HTTP 429."""


class SttRateLimited(SttError, RateLimited):
    """The STT provider kept answering HTTP 429."""


class TtsRequestError(TtsError):
    """The TTS provider rejected the request (4xx other than 429): Opseval's configuration
    (model, voice, key) is wrong, never the agent."""


class SttRequestError(SttError):
    """The STT provider rejected the request (4xx other than 429): Opseval's configuration
    (model, language, key) is wrong, never the agent."""
