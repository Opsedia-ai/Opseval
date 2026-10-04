# SPDX-License-Identifier: Apache-2.0
"""Sinks: where a finished run is published (FR-INT-04, FR-INT-07, ADR-0027).

A sink takes a :class:`~opseval.models.results.SuiteResult` and sends it somewhere — a
webhook, Langfuse scores, later an issue tracker. The architecture fixes the contract as
``async publish(run_result)`` over the ``opseval.sinks`` entry-point group.

Two rules that are not negotiable, because publishing happens *after* the verdict:

1. **A sink can never change the verdict or the exit code.** It runs once the result is
   written; it has nothing to say about the agent. Every failure is caught.
2. **A failure is still reported.** Telemetry can fail silently because nobody asked for
   it. A sink was asked for, so a webhook that did not fire has to say so, in the console
   and in the result. Swallowing it would leave an operator believing a notification went
   out.

Every sink sends to a host the operator configured, so each one goes through
``check_url_allowed``: allowlisted, and TLS off loopback, like everything else that
receives data. Note the difference from telemetry (ADR-0021): a sink publishes to *your*
endpoint, so it carries the full result rather than bucketed counts — you already have
this data. The redaction that matters is still enforced, because ``config_digest``
redacts secrets before they reach a ``SuiteResult`` at all.
"""

from __future__ import annotations

from opseval.sinks.base import Sink, SinkOutcome, build_sinks, publish_all
from opseval.sinks.langfuse import LangfuseScoreSink
from opseval.sinks.webhook import WebhookSink

__all__ = [
    "LangfuseScoreSink",
    "Sink",
    "SinkOutcome",
    "WebhookSink",
    "build_sinks",
    "publish_all",
]
