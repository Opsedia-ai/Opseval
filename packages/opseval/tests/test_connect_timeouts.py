# SPDX-License-Identifier: Apache-2.0
"""A timeout before the agent had the request is a harness error (ADR-0029).

``httpx.ConnectTimeout`` and ``httpx.PoolTimeout`` are subclasses of
``httpx.TimeoutException``, so catching the parent alone turned "the connection never
opened" and "Opseval's own pool was exhausted" into ``TransportTimeout``: the agent's
fault. Preflight already calls an unreachable agent a configuration error, so the two
paths disagreed. Both halves of the rule are pinned for every HTTP transport.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from opseval.catalogue import load_profile
from opseval.driver import Driver
from opseval.errors import HarnessError, TransportTimeout
from opseval.models.profile import FrameKind, SutProfile
from opseval.simulator import ScriptedSimulator
from opseval.transport import HttpJsonTransport, OpenAIChatTransport, SseTransport, Transport
from opseval.verdicts import attempt_verdict

from .conftest import make_scenario

Handler = Callable[[httpx.Request], httpx.Response]


def raises(exc: type[httpx.TimeoutException]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc("timed out", request=request)

    return handler


def client(handler: Handler) -> Callable[[], httpx.AsyncClient]:
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def sse_profile() -> SutProfile:
    return SutProfile.model_validate(
        {
            "name": "sse-agent",
            "kinds": {"message": FrameKind.MESSAGE, "error": FrameKind.ERROR},
            "session_id_field": "session_id",
            "phases": [],
        }
    )


def build(name: str, handler: Handler, profile: SutProfile) -> Transport:
    if name == "http-json":
        return HttpJsonTransport("http://agent.local/chat", profile, client_factory=client(handler))
    if name == "openai-chat":
        return OpenAIChatTransport(
            "https://agent.example.com/v1",
            load_profile("builtin:openai-chat"),
            token="tok",
            client_factory=client(handler),
        )
    return SseTransport(
        "https://agent.example.com/sse", sse_profile(), client_factory=client(handler)
    )


def first_frame(name: str) -> dict[str, Any]:
    if name == "openai-chat":
        return {load_profile("builtin:openai-chat").text_field: "hi"}
    return {"type": "user_message", "text": "hi"}


TRANSPORTS = ["http-json", "openai-chat", "sse"]


@pytest.mark.parametrize("name", TRANSPORTS)
@pytest.mark.parametrize("exc", [httpx.ConnectTimeout, httpx.PoolTimeout])
async def test_a_request_that_was_never_sent_is_not_blamed(
    name: str, exc: type[httpx.TimeoutException], profile: SutProfile
) -> None:
    session = await build(name, raises(exc), profile).connect()
    with pytest.raises(HarnessError, match="could not connect") as info:
        await session.send(first_frame(name))
    assert not isinstance(info.value, TransportTimeout)
    await session.close()


@pytest.mark.parametrize("name", TRANSPORTS)
async def test_an_agent_that_never_answers_is_still_a_timeout(
    name: str, profile: SutProfile
) -> None:
    """The converse: the request reached the agent and nothing came back."""
    session = await build(name, raises(httpx.ReadTimeout), profile).connect()
    with pytest.raises(TransportTimeout):
        await session.send(first_frame(name))
    await session.close()


async def test_connect_timeout_invalidates_the_attempt(profile: SutProfile) -> None:
    """End to end through the driver: not judged, rather than failed."""
    s = make_scenario()
    rec = await Driver(
        scenario=s,
        profile=profile,
        transport=build("http-json", raises(httpx.ConnectTimeout), profile),
        simulator=ScriptedSimulator(s, 1),
        run_id="r",
        attempt_index=0,
        seed=1,
    ).run()
    assert rec.terminal_state.is_infra, rec.terminal_state
    assert attempt_verdict(rec, [], "a0.json").passed is None
