# SPDX-License-Identifier: Apache-2.0
"""WebSocket reconnect/resume and short-lived HS256 tokens.

Both halves of the core rule are exercised: a drop Opseval cannot resume is a harness
problem (not judged), while an agent that misbehaves after a successful resume still
fails, and the drop itself stays visible in the run record.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.server import ServerConnection, serve

from opseval.auth import BearerAuth, HS256Tokens, StaticToken, mint_hs256
from opseval.config import Settings
from opseval.demo.refund_agent import AgentOptions, run_agent
from opseval.driver import Driver
from opseval.errors import ConfigError, TransportError
from opseval.models.profile import SutProfile
from opseval.models.record import RunRecord, TerminalState
from opseval.models.results import ScoreStatus
from opseval.models.scenario import Scenario, ScorerSpec
from opseval.runner import RunOptions, build_transport_factory
from opseval.scoring import REGISTRY, ScoringContext, ScoringEngine
from opseval.simulator import ScriptedSimulator
from opseval.transport import HttpJsonTransport, WebSocketTransport
from opseval.verdicts import attempt_verdict

from .conftest import make_scenario

KEY = "test-signing-key"


async def no_sleep(_: float) -> None:
    return None


def resumable(profile: SutProfile, **reconnect: Any) -> SutProfile:
    data = profile.model_dump()
    data["kinds"]["session_resumed"] = "handshake"
    data["reconnect"] = {
        "resume_session_frame": {"type": "resume_session", "session_id": "{session_id}"},
        "backoff_seconds": 0,
        "confirm_timeout_seconds": 0.5,
        **reconnect,
    }
    return SutProfile.model_validate(data)


async def drive(
    scenario: Scenario, profile: SutProfile, transport: WebSocketTransport
) -> RunRecord:
    return await Driver(
        scenario=scenario,
        profile=profile,
        transport=transport,
        simulator=ScriptedSimulator(scenario, 1),
        run_id="r",
        attempt_index=0,
        seed=1,
    ).run()


async def verdict(record: RunRecord, scenario: Scenario, profile: SutProfile) -> bool | None:
    scores = await ScoringEngine().score(record, scenario, profile)
    return attempt_verdict(record, scores, "x").passed


def kinds(record: RunRecord) -> list[str]:
    return [e.kind for e in record.transport_events]


class TestResume:
    async def test_drop_is_resumed_and_the_attempt_is_judged(self, profile: SutProfile) -> None:
        prof, s = resumable(profile, resend_unanswered=True), make_scenario()
        async with run_agent(options=AgentOptions(drop_after_turns=1)) as url:
            rec = await drive(s, prof, WebSocketTransport(url, prof, sleep=no_sleep))
        assert rec.terminal_state is TerminalState.STOPPED_AT_CHECKPOINT
        assert [i.checkpoint for i in rec.interrupts] == ["proposal_approval", "supervisor_review"]
        assert kinds(rec) == ["dropped", "resumed"]
        assert rec.transport_events[0].close_code == 1011
        assert await verdict(rec, s, prof) is True

    async def test_a_resumed_drop_is_visible_to_the_opt_in_scorer(
        self, profile: SutProfile
    ) -> None:
        prof = resumable(profile, resend_unanswered=True)
        s = make_scenario(
            scorers=[{"id": "output.terminal_state"}, {"id": "conversation.connection_stable"}]
        )
        async with run_agent(options=AgentOptions(drop_after_turns=1)) as url:
            rec = await drive(s, prof, WebSocketTransport(url, prof, sleep=no_sleep))
        scores = {x.scorer_id: x for x in await ScoringEngine().score(rec, s, prof)}
        stable = scores["conversation.connection_stable"]
        assert stable.status is ScoreStatus.FAILED
        assert stable.details["drops"][0]["close_code"] == 1011
        assert stable.details["resumed"] == 1
        assert attempt_verdict(rec, list(scores.values()), "x").passed is False

    # --- harness half: a drop Opseval cannot resume is never the agent's failure ---
    async def test_drop_without_resume_config_is_not_judged(self, profile: SutProfile) -> None:
        s = make_scenario()
        async with run_agent(options=AgentOptions(drop_after_turns=1)) as url:
            rec = await drive(s, profile, WebSocketTransport(url, profile))
        assert rec.terminal_state is TerminalState.TRANSPORT_FAILURE
        assert "no reconnect.resume_session_frame" in (rec.stop_reason or "")
        assert kinds(rec) == ["dropped"]
        assert await verdict(rec, s, profile) is None

    async def test_resume_answered_with_a_new_session_is_not_judged(
        self, profile: SutProfile
    ) -> None:
        prof, s = resumable(profile), make_scenario()
        opts = AgentOptions(drop_after_turns=1, resumable=False)
        async with run_agent(options=opts) as url:
            rec = await drive(s, prof, WebSocketTransport(url, prof, sleep=no_sleep))
        assert rec.terminal_state is TerminalState.TRANSPORT_FAILURE
        assert "resume refused" in (rec.stop_reason or "")
        assert kinds(rec) == ["dropped", "resume_failed"]
        assert await verdict(rec, s, prof) is None

    async def test_resume_attempts_exhausted_is_not_judged(self, profile: SutProfile) -> None:
        prof, s = resumable(profile, max_attempts=2), make_scenario()

        class FailsAfterFirstConnect(WebSocketTransport):
            opened = 0

            async def open(self) -> ClientConnection:
                self.opened += 1
                if self.opened > 1:
                    raise TransportError("cannot connect: refused")
                return await super().open()

        async with run_agent(options=AgentOptions(drop_after_turns=1)) as url:
            rec = await drive(s, prof, FailsAfterFirstConnect(url, prof, sleep=no_sleep))
        assert rec.terminal_state is TerminalState.TRANSPORT_FAILURE
        assert "could not be resumed after 2 attempt(s)" in (rec.stop_reason or "")
        assert kinds(rec) == ["dropped", "resume_failed", "resume_failed"]
        assert await verdict(rec, s, prof) is None

    async def test_silent_agent_during_resume_is_not_judged(self, profile: SutProfile) -> None:
        """No confirmation at all (e.g. the agent ignores the resume frame)."""
        prof = resumable(profile, max_attempts=1)
        prof = prof.model_copy(
            update={
                "reconnect": prof.reconnect.model_copy(  # type: ignore[union-attr]
                    update={"resume_session_frame": {"type": "unknown_to_agent"}}
                ),
                "kinds": {k: v for k, v in prof.kinds.items() if k != "session_created"}
                | {"session_resumed": prof.kinds["session_resumed"]},
            }
        )
        s = make_scenario()
        async with run_agent(options=AgentOptions(drop_after_turns=1)) as url:
            rec = await drive(s, prof, WebSocketTransport(url, prof, sleep=no_sleep))
        assert rec.terminal_state is TerminalState.TRANSPORT_FAILURE
        assert await verdict(rec, s, prof) is None

    # --- agent half: after a successful resume, agent failures still fail --------------
    async def test_agent_that_loses_state_after_resume_fails(self, profile: SutProfile) -> None:
        prof, s = resumable(profile, resend_unanswered=True), make_scenario()
        opts = AgentOptions(drop_after_turns=1, forget_on_resume=True)
        async with run_agent(options=opts) as url:
            rec = await drive(s, prof, WebSocketTransport(url, prof, sleep=no_sleep))
        assert kinds(rec) == ["dropped", "resumed"]
        assert rec.terminal_state is TerminalState.ERROR_FRAME  # "no pending checkpoint"
        assert await verdict(rec, s, prof) is False

    async def test_agent_silent_after_resume_and_resend_is_a_timeout(
        self, profile: SutProfile
    ) -> None:
        prof = resumable(profile, resend_unanswered=True)
        s = make_scenario(execution={"turn_timeout_seconds": 0.5})
        dropped = False

        async def handler(conn: ServerConnection) -> None:
            nonlocal dropped
            await conn.send(json.dumps({"type": "session_created", "session_id": "s-fixed"}))
            async for raw in conn:
                frame = json.loads(raw)
                if frame["type"] == "resume_session":
                    await conn.send(
                        json.dumps({"type": "session_resumed", "session_id": "s-fixed"})
                    )
                elif not dropped:
                    dropped = True
                    await conn.close(code=1001)
                    return
                # after the resume the agent never answers again

        async with serve(handler, "127.0.0.1", 0) as server:
            port = next(iter(server.sockets)).getsockname()[1]
            rec = await drive(
                s, prof, WebSocketTransport(f"ws://127.0.0.1:{port}", prof, sleep=no_sleep)
            )
        assert kinds(rec) == ["dropped", "resumed"]
        assert rec.terminal_state is TerminalState.TIMEOUT
        assert await verdict(rec, s, prof) is False

    async def test_possibly_lost_message_without_resend_is_not_judged(
        self, profile: SutProfile
    ) -> None:
        """The drop may have swallowed the last message: silence is not the agent's fault."""
        prof = resumable(profile)
        s = make_scenario(execution={"turn_timeout_seconds": 0.5})
        state = {"dropped": False}

        async def handler(conn: ServerConnection) -> None:
            await conn.send(json.dumps({"type": "session_created", "session_id": "s-fixed"}))
            async for raw in conn:
                frame = json.loads(raw)
                if frame["type"] == "resume_session":
                    await conn.send(
                        json.dumps({"type": "session_resumed", "session_id": "s-fixed"})
                    )
                elif not state["dropped"]:
                    state["dropped"] = True
                    await conn.close(code=1001)
                    return

        async with serve(handler, "127.0.0.1", 0) as server:
            port = next(iter(server.sockets)).getsockname()[1]
            rec = await drive(
                s, prof, WebSocketTransport(f"ws://127.0.0.1:{port}", prof, sleep=no_sleep)
            )
        assert kinds(rec) == ["dropped", "resumed"]
        assert rec.terminal_state is TerminalState.TRANSPORT_FAILURE
        assert "may have been lost" in (rec.stop_reason or "")
        assert await verdict(rec, s, prof) is None

    async def test_resend_unanswered_after_resume(self, profile: SutProfile) -> None:
        prof = resumable(profile, resend_unanswered=True)
        s = make_scenario(execution={"turn_timeout_seconds": 2})
        received: list[str] = []
        state = {"dropped": False}

        async def handler(conn: ServerConnection) -> None:
            await conn.send(json.dumps({"type": "session_created", "session_id": "s-fixed"}))
            async for raw in conn:
                frame = json.loads(raw)
                if frame["type"] == "resume_session":
                    await conn.send(
                        json.dumps({"type": "session_resumed", "session_id": "s-fixed"})
                    )
                    continue
                received.append(frame["type"])
                if not state["dropped"]:
                    state["dropped"] = True  # drop before answering the first message
                    await conn.close(code=1001)
                    return
                await conn.send(json.dumps({"type": "error", "code": "done"}))

        async with serve(handler, "127.0.0.1", 0) as server:
            port = next(iter(server.sockets)).getsockname()[1]
            rec = await drive(
                s, prof, WebSocketTransport(f"ws://127.0.0.1:{port}", prof, sleep=no_sleep)
            )
        assert received == ["user_message", "user_message"]  # resent once after the resume
        assert rec.terminal_state is TerminalState.ERROR_FRAME

    def test_resume_needs_a_session_id_field(self, profile: SutProfile) -> None:
        data = resumable(profile).model_dump()
        data["session_id_field"] = None
        with pytest.raises(ValueError, match="needs session_id_field"):
            SutProfile.model_validate(data)


def decode(token: str) -> tuple[dict[str, Any], bool]:
    header, payload, signature = token.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    expected = hmac.new(KEY.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    ok = hmac.compare_digest(base64.urlsafe_b64decode(pad(signature)), expected)
    return json.loads(base64.urlsafe_b64decode(pad(payload))), ok


class TestTokens:
    def test_mint_hs256_is_a_verifiable_jwt(self) -> None:
        token = mint_hs256(KEY, {"sub": "x", "exp": 5})
        claims, ok = decode(token)
        assert ok and claims == {"exp": 5, "sub": "x"}
        header = json.loads(base64.urlsafe_b64decode(token.split(".")[0] + "=="))
        assert header == {"alg": "HS256", "typ": "JWT"}

    def test_tokens_are_short_lived_and_refreshed_before_expiry(self) -> None:
        now = [1000.0]
        tokens = HS256Tokens(
            KEY,
            ttl_seconds=60,
            refresh_before_seconds=10,
            claims={"aud": "sut"},
            clock=lambda: now[0],
        )
        assert tokens.seconds_until_refresh() == 0  # nothing minted yet
        first = tokens.current()
        claims, ok = decode(first)
        assert ok and claims["aud"] == "sut" and claims["exp"] - claims["iat"] == 60
        now[0] += 49
        assert tokens.current() == first and not tokens.refresh_due()
        now[0] += 1
        assert tokens.refresh_due()
        second = tokens.current()
        assert second != first and tokens.minted == 2
        assert decode(second)[0]["jti"] != claims["jti"]

    def test_refresh_margin_never_exceeds_the_lifetime(self) -> None:
        tokens = HS256Tokens(KEY, ttl_seconds=10, refresh_before_seconds=60, clock=lambda: 0.0)
        tokens.current()
        assert tokens.seconds_until_refresh() == 1  # refreshes 1s after minting, not before

    def test_empty_key_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            HS256Tokens("")

    def test_static_token_never_refreshes(self) -> None:
        assert StaticToken("t").current() == "t"
        assert StaticToken("t").refresh_due() is False

    async def test_http_transports_send_a_fresh_bearer_on_each_request(
        self, profile: SutProfile
    ) -> None:
        now = [0.0]
        tokens = HS256Tokens(KEY, ttl_seconds=10, refresh_before_seconds=2, clock=lambda: now[0])
        seen: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req.headers["authorization"])
            return httpx.Response(200, json=[])

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=BearerAuth(tokens))
        await client.get("https://sut.example.com/a")
        now[0] = 9
        await client.get("https://sut.example.com/a")
        assert len(set(seen)) == 2 and all(h.startswith("Bearer ey") for h in seen)

        transport = HttpJsonTransport("https://sut.example.com/chat", profile, token=tokens)
        session = await transport.connect()
        assert isinstance(session._client.auth, BearerAuth)  # type: ignore[attr-defined]
        await session.close()

    async def test_websocket_refreshes_the_token_while_waiting(self, profile: SutProfile) -> None:
        data = profile.model_dump()
        data["auth_frame"] = {"type": "auth", "token": "{token}"}
        data["token"] = {
            "ttl_seconds": 10,
            "refresh_before_seconds": 9,  # due one second after minting
            "refresh_frame": {"type": "auth_refresh", "token": "{token}"},
        }
        prof = SutProfile.model_validate(data)
        s = make_scenario(execution={"turn_timeout_seconds": 5})
        auth: list[tuple[str, dict[str, Any], bool]] = []

        async def handler(conn: ServerConnection) -> None:
            await conn.send(json.dumps({"type": "session_created", "session_id": "s-tok"}))
            async for raw in conn:
                frame = json.loads(raw)
                if frame["type"] in ("auth", "auth_refresh"):
                    claims, ok = decode(frame["token"])
                    auth.append((frame["type"], claims, ok))
                    continue
                await asyncio.sleep(1.4)  # slow reply: the token becomes due meanwhile
                await conn.send(json.dumps({"type": "error", "code": "done"}))

        async with serve(handler, "127.0.0.1", 0) as server:
            port = next(iter(server.sockets)).getsockname()[1]
            transport = WebSocketTransport(
                f"ws://127.0.0.1:{port}",
                prof,
                token=HS256Tokens(KEY, ttl_seconds=10, refresh_before_seconds=9),
            )
            rec = await drive(s, prof, transport)
        assert [a[0] for a in auth][:2] == ["auth", "auth_refresh"]
        assert all(ok for _, _, ok in auth)
        assert auth[0][1]["jti"] != auth[1][1]["jti"]
        assert "token_refreshed" in kinds(rec)
        assert "eyJ" not in rec.model_dump_json()  # token values never reach the record


class TestRunnerCredentials:
    def _profile(self, profile: SutProfile) -> SutProfile:
        data = profile.model_dump()
        data["token"] = {"ttl_seconds": 60, "claims": {"aud": "refund-agent"}}
        return SutProfile.model_validate(data)

    def test_profile_token_without_signing_key_is_a_config_error(
        self, profile: SutProfile, tmp_path: Path
    ) -> None:
        settings = Settings(allowed_hosts="127.0.0.1", reports_dir=str(tmp_path))
        factory = build_transport_factory(RunOptions(target="ws://127.0.0.1:1"), settings)
        with pytest.raises(ConfigError, match="OPSEVAL_TOKEN_SIGNING_KEY"):
            factory(self._profile(profile), None)

    def test_profile_token_mints_a_fresh_source_per_attempt(
        self, profile: SutProfile, tmp_path: Path
    ) -> None:
        settings = Settings(
            allowed_hosts="127.0.0.1", reports_dir=str(tmp_path), token_signing_key=KEY
        )
        factory = build_transport_factory(RunOptions(target="ws://127.0.0.1:1"), settings)
        a, b = (
            factory(self._profile(profile), None),
            factory(self._profile(profile), None),
        )
        assert isinstance(a, WebSocketTransport) and isinstance(b, WebSocketTransport)
        assert isinstance(a.tokens, HS256Tokens) and a.tokens is not b.tokens
        claims, ok = decode(a.tokens.current())
        assert ok and claims["aud"] == "refund-agent"

    async def test_auth_frame_without_credentials_is_not_judged(self, profile: SutProfile) -> None:
        data = profile.model_dump()
        data["auth_frame"] = {"type": "auth", "token": "{token}"}
        prof, s = SutProfile.model_validate(data), make_scenario()
        async with run_agent() as url:
            rec = await drive(s, prof, WebSocketTransport(url, prof))
        assert rec.terminal_state is TerminalState.TRANSPORT_FAILURE
        assert "OPSEVAL_TOKEN_SIGNING_KEY" in (rec.stop_reason or "")


class TestConnectionStableScorer:
    async def test_ignore_codes_and_bad_params(self, profile: SutProfile) -> None:
        prof, s = resumable(profile, resend_unanswered=True), make_scenario()
        async with run_agent(options=AgentOptions(drop_after_turns=1, drop_code=1001)) as url:
            rec = await drive(s, prof, WebSocketTransport(url, prof, sleep=no_sleep))
        scorer = REGISTRY["conversation.connection_stable"]()
        ctx = ScoringContext(scenario=s, profile=prof)
        ok = await scorer.score(rec, ScorerSpec(id=scorer.id, params={"ignore_codes": [1001]}), ctx)
        assert ok.status is ScoreStatus.PASSED
        bad = await scorer.score(
            rec, ScorerSpec(id=scorer.id, params={"ignore_codes": "1001"}), ctx
        )
        assert bad.status is ScoreStatus.INFRA_ERROR


class TestRunnerWithResume:
    async def test_resumed_run_passes_and_the_report_shows_the_drop(
        self, cat_dir: Path, profile: SutProfile, tmp_path: Path
    ) -> None:
        from opseval.models.results import VerdictStatus
        from opseval.runner import launch

        prof_path = tmp_path / "profile.json"
        prof_path.write_text(
            resumable(profile, resend_unanswered=True).model_dump_json(), encoding="utf-8"
        )
        settings = Settings(allowed_hosts="127.0.0.1", reports_dir=str(tmp_path / "r"))
        async with run_agent(options=AgentOptions(drop_after_turns=1)) as url:
            result = await launch(
                cat_dir / "scenarios" / "rf-001-happy-path.yaml",
                RunOptions(target=url, profile=prof_path, repeats=1),
                settings,
            )
        assert result.tests[0].status is VerdictStatus.PASSED, result.tests[0]
        assert result.exit_code == 0
        html = (Path(settings.reports_dir) / result.run_id / "report.html").read_text("utf-8")
        assert "dropped (code 1011) → resumed #1" in html
