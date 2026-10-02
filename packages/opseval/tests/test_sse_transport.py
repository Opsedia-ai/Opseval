# SPDX-License-Identifier: Apache-2.0
"""Server-Sent Events transport.

Fault-injection view of the core rule:
- harness half: a connection that fails or dies mid-stream is ``TransportClosed`` (a
  ``HarnessError``), so the attempt is *not judged*; a non-SSE content type is a config
  error; a malformed event is recorded verbatim rather than raising;
- agent half: a stream that stalls past the timeout is ``TransportTimeout``, which does
  count against the agent, and an HTTP 5xx becomes an ``error`` frame that gets scored.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from opseval.errors import TransportClosed, TransportError, TransportTimeout
from opseval.models.profile import FrameKind, SutProfile
from opseval.scoring import ScoringEngine
from opseval.transport.base import SYNTHETIC_KIND
from opseval.transport.sse import (
    SSE_MEDIA_TYPE,
    SseEvent,
    SseTransport,
    frame_from_event,
    parse_sse_lines,
)

from .conftest import make_scenario

SSE_HEADERS = {"content-type": SSE_MEDIA_TYPE}


def sse_profile(**overrides: Any) -> SutProfile:
    base: dict[str, Any] = {
        "name": "sse-agent",
        "kinds": {"message": FrameKind.MESSAGE, "error": FrameKind.ERROR},
        "session_id_field": "session_id",
        "phases": [],
    }
    return SutProfile.model_validate({**base, **overrides})


def transport(handler: Any, profile: SutProfile | None = None, **kw: Any) -> SseTransport:
    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return SseTransport(
        "https://agent.example.com/sse",
        profile or sse_profile(),
        client_factory=factory,
        **kw,
    )


def stream(*chunks: str, status: int = 200, headers: dict[str, str] | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, headers=headers if headers is not None else SSE_HEADERS, text="".join(chunks)
        )

    return handler


# --- the wire format -----------------------------------------------------------------


class TestParsing:
    def test_a_simple_event(self) -> None:
        events = parse_sse_lines(['data: {"a": 1}', ""])
        assert len(events) == 1
        assert events[0].data == '{"a": 1}'

    def test_multiple_data_lines_join_with_newlines(self) -> None:
        events = parse_sse_lines(["data: one", "data: two", ""])
        assert events[0].data == "one\ntwo"

    def test_the_event_name_is_kept(self) -> None:
        events = parse_sse_lines(["event: message", "data: {}", ""])
        assert events[0].event == "message"

    def test_comments_and_heartbeats_are_not_events(self) -> None:
        assert parse_sse_lines([": keep-alive", "", ": another", ""]) == []

    def test_a_blank_line_without_data_dispatches_nothing(self) -> None:
        """An event: line with no data is not a frame."""
        assert parse_sse_lines(["event: ping", ""]) == []

    def test_a_stream_ending_without_a_blank_line_still_dispatches(self) -> None:
        events = parse_sse_lines(['data: {"a": 1}'])
        assert len(events) == 1

    def test_a_space_after_the_colon_is_stripped_once(self) -> None:
        assert parse_sse_lines(["data:  two spaces", ""])[0].data == " two spaces"

    def test_a_field_with_no_colon_is_ignored(self) -> None:
        assert parse_sse_lines(["data: ok", "", "bogusline", ""])[0].data == "ok"

    def test_carriage_returns_are_handled(self) -> None:
        assert parse_sse_lines(["data: ok\r", "\r"])[0].data == "ok"

    def test_the_id_field_is_carried(self) -> None:
        assert parse_sse_lines(["id: 7", "data: ok", ""])[0].last_event_id == "7"

    def test_an_id_containing_a_null_is_ignored(self) -> None:
        assert parse_sse_lines(["id: a\x00b", "data: ok", ""])[0].last_event_id is None

    def test_parsing_never_raises_on_rubbish(self) -> None:
        assert parse_sse_lines(["", "::::", "data", "retry: abc", "data: x", ""])


class TestEventToFrame:
    def test_the_event_name_supplies_a_missing_kind(self) -> None:
        frame = frame_from_event(SseEvent('{"text": "hi"}', "message", None), sse_profile())
        assert frame == {"text": "hi", "type": "message"}

    def test_a_payload_kind_always_wins(self) -> None:
        frame = frame_from_event(
            SseEvent('{"type": "error", "text": "hi"}', "message", None), sse_profile()
        )
        assert frame["type"] == "error"

    def test_a_custom_kind_field_is_respected(self) -> None:
        profile = sse_profile(kind_field="kind", kinds={"message": FrameKind.MESSAGE})
        frame = frame_from_event(SseEvent("{}", "message", None), profile)
        assert frame == {"kind": "message"}

    def test_unparseable_data_is_recorded_verbatim(self) -> None:
        """Bad bytes must never look like a clean turn."""
        frame = frame_from_event(SseEvent("not json", "message", None), sse_profile())
        assert frame == {"_raw": "not json"}

    def test_a_non_object_payload_is_left_alone(self) -> None:
        assert frame_from_event(SseEvent("[1, 2]", "message", None), sse_profile()) == [1, 2]


# --- the session ---------------------------------------------------------------------


class TestSession:
    async def test_frames_are_queued_then_handed_out_in_order(self) -> None:
        agent = transport(
            stream(
                'data: {"type": "message", "text": "one"}\n\n',
                'data: {"type": "message", "text": "two"}\n\n',
            )
        )
        session = await agent.connect()
        await session.send({"type": "user_message", "text": "hi"})
        assert (await session.receive(1))["text"] == "one"
        assert (await session.receive(1))["text"] == "two"
        with pytest.raises(TransportTimeout):
            await session.receive(1)
        await session.close()

    async def test_the_session_id_is_learned_from_the_stream(self) -> None:
        agent = transport(stream('data: {"type": "message", "session_id": "s-1"}\n\n'))
        session = await agent.connect()
        await session.send({"type": "user_message"})
        await session.receive(1)
        assert session.session_id == "s-1"
        await session.close()

    async def test_the_session_id_is_stamped_on_later_sends(self) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen.append(json.loads(request.content))
            return httpx.Response(
                200, headers=SSE_HEADERS, text='data: {"type": "message", "session_id": "s-1"}\n\n'
            )

        agent = transport(handler)
        session = await agent.connect()
        await session.send({"type": "user_message"})
        await session.receive(1)
        await session.send({"type": "user_message"})
        assert seen[1]["session_id"] == "s-1"
        await session.close()

    async def test_an_empty_stream_is_a_turn_with_nothing_in_it(self) -> None:
        """Not an error: the agent answered and said nothing."""
        session = await transport(stream("")).connect()
        await session.send({"type": "user_message"})
        with pytest.raises(TransportTimeout):
            await session.receive(1)
        await session.close()

    async def test_the_accept_header_asks_for_an_event_stream(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("accept", ""))
            return httpx.Response(200, headers=SSE_HEADERS, text="")

        session = await transport(handler).connect()
        await session.send({"type": "user_message"})
        assert SSE_MEDIA_TYPE in seen[0]
        await session.close()


# --- fault injection: both halves of the core rule -----------------------------------


class TestAgentFaults:
    """These count against the agent."""

    async def test_a_stalled_stream_is_a_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("stalled", request=request)

        session = await transport(handler, request_timeout=0.05).connect()
        with pytest.raises(TransportTimeout, match="did not finish its turn"):
            await session.send({"type": "user_message"})
        await session.close()

    async def test_a_server_error_becomes_a_scorable_error_frame(self) -> None:
        """The agent answered, with an error. That is behaviour, not harness noise."""
        session = await transport(stream("boom", status=503, headers={})).connect()
        await session.send({"type": "user_message"})
        frame = await session.receive(1)
        assert frame[SYNTHETIC_KIND] == "error"
        assert frame["status"] == 503
        await session.close()

    async def test_a_partial_event_at_the_end_is_still_delivered(self) -> None:
        session = await transport(stream('data: {"type": "message", "text": "cut"}')).connect()
        await session.send({"type": "user_message"})
        assert (await session.receive(1))["text"] == "cut"
        await session.close()


class TestHarnessFaults:
    """These are never the agent's fault, so the attempt is not judged."""

    async def test_a_dead_connection_is_transport_closed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        session = await transport(handler).connect()
        with pytest.raises(TransportClosed, match="event stream"):
            await session.send({"type": "user_message"})
        await session.close()

    async def test_transport_closed_is_a_harness_error(self) -> None:
        from opseval.errors import HarnessError

        assert issubclass(TransportClosed, HarnessError)
        assert not issubclass(TransportTimeout, HarnessError)

    async def test_a_stream_that_dies_midway_is_transport_closed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadError("connection reset", request=request)

        session = await transport(handler).connect()
        with pytest.raises(TransportClosed):
            await session.send({"type": "user_message"})
        await session.close()

    async def test_a_plain_json_endpoint_says_which_transport_to_use(self) -> None:
        """Pointing --transport sse at a JSON endpoint should say so, not report silence."""
        session = await transport(
            stream('{"type": "message"}', headers={"content-type": "application/json"})
        ).connect()
        with pytest.raises(TransportError, match="http-json"):
            await session.send({"type": "user_message"})
        await session.close()

    async def test_a_rejected_request_is_a_transport_error(self) -> None:
        session = await transport(stream("nope", status=401, headers={})).connect()
        with pytest.raises(TransportError, match="HTTP 401"):
            await session.send({"type": "user_message"})
        await session.close()

    async def test_an_unexpected_text_content_type_is_accepted(self) -> None:
        """A sloppy but working server must not be turned into a harness failure."""
        session = await transport(
            stream('data: {"type": "message", "text": "ok"}\n\n', headers={})
        ).connect()
        await session.send({"type": "user_message"})
        assert (await session.receive(1))["text"] == "ok"
        await session.close()


class TestWiring:
    def test_the_transport_reports_its_target(self) -> None:
        assert transport(stream("")).target == "https://agent.example.com/sse"

    def test_it_is_named_for_the_cli(self) -> None:
        assert SseTransport.name == "sse"

    def test_the_runner_builds_it(self) -> None:
        from opseval.config import Settings
        from opseval.runner import RunOptions, build_transport_factory

        settings = Settings(allowed_hosts="agent.example.com")
        factory = build_transport_factory(
            RunOptions(target="https://agent.example.com/sse", transport="sse"), settings
        )
        assert isinstance(factory(sse_profile()), SseTransport)

    def test_an_unknown_transport_names_sse(self) -> None:
        from opseval.config import Settings
        from opseval.errors import ConfigError
        from opseval.runner import RunOptions, build_transport_factory

        with pytest.raises(ConfigError, match="sse"):
            build_transport_factory(
                RunOptions(target="https://agent.example.com", transport="carrier-pigeon"),
                Settings(allowed_hosts="agent.example.com"),
            )

    def test_plain_http_is_refused_for_a_remote_host(self) -> None:
        """A credential must not cross the network in the clear."""
        from opseval.config import Settings
        from opseval.errors import AllowedHostError
        from opseval.runner import RunOptions, build_transport_factory

        with pytest.raises(AllowedHostError):
            build_transport_factory(
                RunOptions(target="http://agent.example.com/sse", transport="sse"),
                Settings(allowed_hosts="agent.example.com"),
            )

    def test_a_host_outside_the_allowlist_is_refused(self) -> None:
        from opseval.config import Settings
        from opseval.errors import AllowedHostError
        from opseval.runner import RunOptions, build_transport_factory

        with pytest.raises(AllowedHostError):
            build_transport_factory(
                RunOptions(target="https://elsewhere.example.com/sse", transport="sse"),
                Settings(allowed_hosts="agent.example.com"),
            )


class TestEndToEnd:
    async def test_a_streamed_turn_is_scored(self) -> None:
        """The driver should not be able to tell SSE from any other transport."""
        from opseval.driver import Driver
        from opseval.simulator import ScriptedSimulator

        profile = sse_profile(
            kinds={"message": FrameKind.MESSAGE, "result": FrameKind.RESULT},
            phases=["answer"],
        )
        agent = transport(
            stream(
                'event: message\ndata: {"text": "Your order shipped."}\n\n',
                'data: {"type": "result", "outcome": "answered"}\n\n',
            ),
            profile,
        )
        scenario = make_scenario(
            conversation={
                "scripted_turns": ["Where is order 10482?"],
                "stop_at": "goal_reached",
                "checkpoint_policy": {},
            },
            expectations={"expected_tools": {}},
            scorers=[{"id": "conversation.no_errors"}],
        )
        record = await Driver(
            scenario=scenario,
            profile=profile,
            transport=agent,
            simulator=ScriptedSimulator(scenario, 1),
            run_id="r",
            attempt_index=0,
            seed=1,
        ).run()
        assert any("order shipped" in (text or "") for _who, text in record.transcript())
        scores = await ScoringEngine().score(record, scenario, profile)
        assert [s.scorer_id for s in scores] == ["conversation.no_errors"]
