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


class LLMRequestError(HarnessError):
    """The LLM provider rejected the request (4xx other than 429)."""
