# SPDX-License-Identifier: Apache-2.0
"""Webhook sink: POST the finished run to a URL (FR-INT-07, ADR-0027)."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from opseval.models.results import SuiteResult
from opseval.sinks.base import Sink


class WebhookSink(Sink):
    """POST the whole ``SuiteResult`` as JSON to an operator-configured URL.

    The full result, not a summary: this is the operator's own endpoint and they already
    have this data, so trimming it would only make the hook less useful. Secrets are not
    a concern here because ``config_digest`` redacts them before they ever reach a
    ``SuiteResult`` — not because the destination is trusted.
    """

    name = "webhook"

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 10.0,
        headers: dict[str, str] | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._url = url
        self._timeout = timeout
        self._headers = headers or {}
        self._client_factory = client_factory

    async def publish(self, result: SuiteResult) -> str:
        client = self._client_factory() if self._client_factory else httpx.AsyncClient()
        try:
            response = await client.post(
                self._url,
                json=result.model_dump(mode="json"),
                timeout=self._timeout,
                headers={"content-type": "application/json", **self._headers},
            )
        finally:
            await client.aclose()
        if response.status_code >= 400:
            raise RuntimeError(f"{self._url} answered HTTP {response.status_code}")
        return f"posted run {result.run_id} to {self._url} (HTTP {response.status_code})"


__all__ = ["WebhookSink"]
