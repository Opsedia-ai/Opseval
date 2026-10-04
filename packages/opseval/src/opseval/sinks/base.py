# SPDX-License-Identifier: Apache-2.0
"""The sink contract and the publish loop (ADR-0027)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable

from opseval.config import Settings, check_url_allowed
from opseval.errors import ConfigError
from opseval.models.results import SinkOutcome, SuiteResult

SINK_GROUP = "opseval.sinks"


class Sink(ABC):
    """Somewhere a finished run gets published."""

    name: str = "sink"

    @abstractmethod
    async def publish(self, result: SuiteResult) -> str:
        """Publish ``result`` and return a one-line description of what was sent.

        Raise on failure. The caller catches it: a sink must never change a verdict.
        """


async def publish_all(
    sinks: Iterable[Sink],
    result: SuiteResult,
    *,
    log: Callable[[str], None] = lambda _msg: None,
) -> list[SinkOutcome]:
    """Publish to every sink. Never raises, and never changes the result.

    Each failure is caught *and reported*: a sink was asked for, so one that did not
    fire has to say so rather than leaving an operator believing a notification went out.
    """
    outcomes: list[SinkOutcome] = []
    for sink in sinks:
        try:
            detail = await sink.publish(result)
        except Exception as exc:  # noqa: BLE001 - a sink must not break a run
            outcomes.append(
                SinkOutcome(sink=sink.name, published=False, detail=f"{type(exc).__name__}: {exc}")
            )
            log(f"sink {sink.name}: FAILED to publish — {type(exc).__name__}: {exc}")
            continue
        outcomes.append(SinkOutcome(sink=sink.name, published=True, detail=detail))
        log(f"sink {sink.name}: {detail}")
    return outcomes


__all__ = ["SINK_GROUP", "Sink", "SinkOutcome", "build_sinks", "publish_all"]


def build_sinks(settings: Settings) -> list[Sink]:
    """The sinks the operator configured. Each host is checked before anything is sent.

    Raises :class:`~opseval.errors.ConfigError` for a host that is not allowlisted, or
    plain HTTP off loopback: a misconfigured destination is a configuration error, found
    before the run rather than after it has something to publish.
    """
    from opseval.plugins import load_sinks
    from opseval.sinks.langfuse import LangfuseScoreSink
    from opseval.sinks.webhook import WebhookSink

    built: list[Sink] = []
    # Discovered first, so a broken plugin is reported before any built-in publishes.
    for cls in load_sinks().values():
        built.append(cls())
    if settings.webhook_url:
        check_url_allowed(settings.webhook_url, settings.allowlist(), secure_schemes=("https",))
        built.append(WebhookSink(settings.webhook_url))
    if settings.langfuse_push_scores:
        host = settings.langfuse_host
        public, secret = settings.langfuse_public_key, settings.langfuse_secret_key
        if not (host and public and secret):
            raise ConfigError(
                "OPSEVAL_LANGFUSE_PUSH_SCORES needs OPSEVAL_LANGFUSE_HOST, "
                "OPSEVAL_LANGFUSE_PUBLIC_KEY and OPSEVAL_LANGFUSE_SECRET_KEY"
            )
        check_url_allowed(host, settings.allowlist(), secure_schemes=("https",))
        built.append(
            LangfuseScoreSink(
                host,
                public,
                secret.get_secret_value(),
                score_name=settings.langfuse_score_name,
            )
        )
    return built
