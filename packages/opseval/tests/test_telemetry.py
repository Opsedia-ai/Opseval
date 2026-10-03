# SPDX-License-Identifier: Apache-2.0
"""Opt-out, content-free telemetry (ADR-0010).

The tests the ADR asks for, plus the one that matters most: a run whose scenario text,
target host, profile name and run id are all sentinel strings, asserted absent from the
serialised payload. If telemetry ever starts carrying content, that test fails.

It also ships dark, so "nothing is sent" is the default and is tested as such.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from opseval.cli import app
from opseval.config import Settings
from opseval.models.record import TerminalState
from opseval.models.results import (
    AttemptVerdict,
    Axis,
    Gate,
    ScoreResult,
    ScoreStatus,
    SuiteResult,
    TestVerdict,
    VerdictStatus,
)
from opseval.models.scenario import Criticality
from opseval.models.telemetry import TELEMETRY_SCHEMA, TelemetryEvent, TelemetryPayload, bucket
from opseval.telemetry import (
    CUSTOM,
    build_payload,
    decide,
    install_id,
    mark_notice_shown,
    notice,
    report,
    send,
    set_enabled,
)

NOW = datetime(2026, 10, 2, tzinfo=UTC)
SENTINEL = "SECRETCUSTOMERTEXT"

PAYLOAD_FIELDS = {
    "schema",
    "install_id",
    "event",
    "opseval_version",
    "python",
    "os",
    "arch",
    "mode",
    "ci",
    "scenarios",
    "attempts",
    "judged_attempts",
    "exit_code",
    "gate",
    "transports",
    "scorers",
    "trace_source",
    "languages",
    "demo_target",
}
"""Every field that may ever be sent. Adding one here means moving ADR-0010 too."""


def score(scorer_id: str = "flow.phase_order", status: ScoreStatus = ScoreStatus.PASSED):
    return ScoreResult(
        scorer_id=scorer_id,
        axis=Axis.FLOW,
        status=status,
        score=None if status is ScoreStatus.INFRA_ERROR else 1.0,
        threshold=1.0,
        weight=1.0,
        reason=f"JUDGE_ERROR: {SENTINEL}" if status is ScoreStatus.INFRA_ERROR else None,
    )


def result(
    *,
    tests: int = 1,
    attempts: int = 1,
    scorer_id: str = "flow.phase_order",
    judged: bool = True,
    **kw: Any,
) -> SuiteResult:
    verdicts = [
        TestVerdict(
            test_id=f"{SENTINEL}-{i}",
            name=f"{SENTINEL} scenario name",
            criticality=Criticality.CRITICAL,
            status=VerdictStatus.PASSED,
            reason=f"{SENTINEL} reason",
            attempts=[
                AttemptVerdict(
                    attempt_index=j,
                    passed=True,
                    terminal_state=TerminalState.GOAL_REACHED,
                    scores=[
                        score(
                            scorer_id,
                            ScoreStatus.PASSED if judged else ScoreStatus.INFRA_ERROR,
                        )
                    ],
                    record_path=f"/home/{SENTINEL}/a{j}.json",
                )
                for j in range(attempts)
            ],
        )
        for i in range(tests)
    ]
    base: dict[str, Any] = {
        "opseval_version": "0.1.0",
        "run_id": f"{SENTINEL}-run",
        "suite": f"{SENTINEL}-suite",
        "target": f"wss://{SENTINEL}.example.com/ws",
        "profile": f"{SENTINEL}-profile",
        "started_at": NOW,
        "ended_at": NOW,
        "tests": verdicts,
        "gate": Gate(status="pass", summary=f"{SENTINEL} summary"),
        "exit_code": 0,
        "config_digest": {"allowed_hosts": f"{SENTINEL}.example.com"},
    }
    return SuiteResult(**{**base, **kw})


def payload(res: SuiteResult | None = None, **kw: Any) -> TelemetryPayload:
    kw.setdefault("event", TelemetryEvent.RUN)
    kw.setdefault("install", "iid")
    kw.setdefault("transports", ["websocket"])
    kw.setdefault("languages", ["en"])
    return build_payload(res or result(), **kw)


def settings(**kw: Any) -> Settings:
    """Settings with the ambient environment pinned.

    `Settings()` reads CI and DO_NOT_TRACK from the real environment, so without this a
    run under GitHub Actions takes the "off in CI" branch before reaching the one the
    test is about. Tests that are *about* those switches pass them explicitly.
    """
    kw.setdefault("allowed_hosts", "")
    kw.setdefault("ci", False)
    kw.setdefault("do_not_track", False)
    return Settings(**kw)


# --- the thing that must never happen -------------------------------------------------


class TestNoContentEscapes:
    def test_no_sentinel_appears_anywhere_in_the_payload(self) -> None:
        """Scenario ids and names, the reason, the target host, the profile, the run id
        and a record path all contain the sentinel. None may reach the wire."""
        blob = json.dumps(payload().model_dump(by_alias=True, mode="json"))
        assert SENTINEL not in blob

    def test_the_payload_has_exactly_the_documented_fields(self) -> None:
        assert set(payload().model_dump(by_alias=True).keys()) == PAYLOAD_FIELDS

    def test_an_extra_field_is_rejected(self) -> None:
        """So a field cannot be added without the schema and the docs moving with it."""
        with pytest.raises(ValueError, match="hostname"):
            TelemetryPayload(
                install_id="i",
                event=TelemetryEvent.RUN,
                opseval_version="0",
                python="3.12",
                os="Linux",
                arch="x86_64",
                scenarios="1",
                attempts="1",
                judged_attempts="1",
                exit_code=0,
                gate="pass",
                hostname="my-laptop",
            )

    def test_the_schema_is_versioned(self) -> None:
        assert payload().schema_ == TELEMETRY_SCHEMA

    def test_counts_are_bucketed_not_exact(self) -> None:
        out = payload(result(tests=7, attempts=4))
        assert out.scenarios == "6-20"
        assert out.attempts == "21-100"  # 7 scenarios x 4 attempts
        assert "28" not in json.dumps(out.model_dump(by_alias=True))

    @pytest.mark.parametrize(
        ("count", "label"),
        [(0, "0"), (1, "1"), (2, "2-5"), (5, "2-5"), (6, "6-20"), (100, "21-100"), (101, "100+")],
    )
    def test_bucket_boundaries(self, count: int, label: str) -> None:
        assert bucket(count) == label

    def test_a_third_party_transport_is_reported_as_custom(self) -> None:
        """A bespoke transport name could identify a company."""
        assert payload(transports=["acme-internal-bus"]).transports == [CUSTOM]

    def test_a_third_party_scorer_is_reported_as_custom(self) -> None:
        out = payload(result(scorer_id="acme.secret_check"))
        assert out.scorers == [CUSTOM]

    def test_builtin_names_are_kept(self) -> None:
        out = payload(result(scorer_id="flow.phase_order"), transports=["websocket"])
        assert out.scorers == ["flow.phase_order"]
        assert out.transports == ["websocket"]

    def test_a_custom_trace_source_is_reported_as_custom(self) -> None:
        assert payload(trace_source="acme-traces").trace_source == CUSTOM
        assert payload(trace_source="langfuse").trace_source == "langfuse"
        assert payload().trace_source == "none"

    def test_only_language_codes_are_sent(self) -> None:
        assert payload(languages=["ar", "en", "ar"]).languages == ["ar", "en"]


class TestJudgedAttempts:
    def test_an_unmeasured_attempt_is_not_counted_as_judged(self) -> None:
        out = payload(result(attempts=3, judged=False))
        assert out.attempts == "2-5"
        assert out.judged_attempts == "0"

    def test_a_measured_attempt_counts(self) -> None:
        assert payload(result(attempts=3, judged=True)).judged_attempts == "2-5"


# --- whether anything is sent at all --------------------------------------------------


class TestDecide:
    def test_nothing_is_sent_without_an_endpoint(self, tmp_path: Path) -> None:
        """The default. The code ships dark."""
        decision = decide(settings(), tmp_path / "c.json")
        assert decision.send is False
        assert "no endpoint" in decision.reason

    def test_opseval_telemetry_false_wins(self, tmp_path: Path) -> None:
        decision = decide(
            settings(telemetry="false", telemetry_endpoint="https://t.example.com"),
            tmp_path / "c.json",
        )
        assert decision.send is False
        assert "OPSEVAL_TELEMETRY=false" in decision.reason

    def test_do_not_track_is_honoured(self, tmp_path: Path) -> None:
        decision = decide(
            settings(
                do_not_track=True,
                telemetry_endpoint="https://t.example.com",
                allowed_hosts="t.example.com",
            ),
            tmp_path / "c.json",
        )
        assert decision.send is False
        assert "DO_NOT_TRACK" in decision.reason

    def test_ci_is_off_by_default(self, tmp_path: Path) -> None:
        decision = decide(
            settings(
                ci=True, telemetry_endpoint="https://t.example.com", allowed_hosts="t.example.com"
            ),
            tmp_path / "c.json",
        )
        assert decision.send is False
        assert "CI" in decision.reason

    def test_ci_can_be_overridden_explicitly(self, tmp_path: Path) -> None:
        mark_notice_shown(tmp_path / "c.json")
        decision = decide(
            settings(
                ci=True,
                telemetry="true",
                telemetry_endpoint="https://t.example.com",
                allowed_hosts="t.example.com",
            ),
            tmp_path / "c.json",
        )
        assert decision.send is True

    def test_an_unallowlisted_endpoint_is_refused(self, tmp_path: Path) -> None:
        """ADR-0010 decision 1 option A: the telemetry host is allowlisted like any other."""
        decision = decide(settings(telemetry_endpoint="https://t.example.com"), tmp_path / "c.json")
        assert decision.send is False
        assert "not allowlisted" in decision.reason

    def test_plain_http_is_refused(self, tmp_path: Path) -> None:
        decision = decide(
            settings(telemetry_endpoint="http://t.example.com", allowed_hosts="t.example.com"),
            tmp_path / "c.json",
        )
        assert decision.send is False

    def test_the_first_run_prints_a_notice_and_sends_nothing(self, tmp_path: Path) -> None:
        decision = decide(
            settings(telemetry_endpoint="https://t.example.com", allowed_hosts="t.example.com"),
            tmp_path / "c.json",
        )
        assert decision.send is False
        assert decision.first_run is True

    def test_the_second_run_sends(self, tmp_path: Path) -> None:
        config = tmp_path / "c.json"
        mark_notice_shown(config)
        decision = decide(
            settings(telemetry_endpoint="https://t.example.com", allowed_hosts="t.example.com"),
            config,
        )
        assert decision.send is True

    def test_a_local_disable_is_respected(self, tmp_path: Path) -> None:
        config = tmp_path / "c.json"
        mark_notice_shown(config)
        set_enabled(False, config)
        decision = decide(
            settings(telemetry_endpoint="https://t.example.com", allowed_hosts="t.example.com"),
            config,
        )
        assert decision.send is False
        assert "disabled locally" in decision.reason

    def test_the_notice_says_what_and_how_to_stop(self) -> None:
        text = notice("https://t.example.com")
        assert "no conversations" in text
        assert "Nothing was sent" in text
        assert "telemetry disable" in text
        assert "DO_NOT_TRACK" in text


class TestInstallId:
    def test_it_is_created_once_and_reused(self, tmp_path: Path) -> None:
        config = tmp_path / "c.json"
        first = install_id(config)
        assert install_id(config) == first

    def test_it_is_a_random_uuid_not_derived_from_the_machine(self, tmp_path: Path) -> None:
        """Deriving it from a hostname or MAC would make it a fingerprint."""
        import platform
        import uuid

        value = install_id(tmp_path / "c.json")
        uuid.UUID(value)  # parses
        assert platform.node() not in value
        assert install_id(tmp_path / "other.json") != value

    def test_an_unwritable_config_does_not_raise(self, tmp_path: Path) -> None:
        """A read-only home must not break a run."""
        assert install_id(tmp_path / "nope" / "deep" / "c.json")

    def test_a_corrupt_config_is_ignored(self, tmp_path: Path) -> None:
        config = tmp_path / "c.json"
        config.write_text("{not json", encoding="utf-8")
        assert install_id(config)


# --- it can never affect the run ------------------------------------------------------


class TestCannotAffectTheRun:
    async def test_a_dead_collector_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def boom(self: Any, url: str, **kw: Any) -> httpx.Response:
            raise httpx.ConnectError("no collector")

        monkeypatch.setattr(httpx.AsyncClient, "post", boom)
        assert await send(payload(), "https://t.example.com") is False

    async def test_a_5xx_is_not_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fail(self: Any, url: str, **kw: Any) -> httpx.Response:
            return httpx.Response(503)

        monkeypatch.setattr(httpx.AsyncClient, "post", fail)
        assert await send(payload(), "https://t.example.com") is False

    async def test_report_never_raises_even_on_an_internal_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "opseval.telemetry.build_payload",
            lambda *a, **k: (_ for _ in ()).throw(TypeError("bug")),
        )
        config = tmp_path / "c.json"
        mark_notice_shown(config)
        decision = await report(
            result(),
            settings(telemetry_endpoint="https://t.example.com", allowed_hosts="t.example.com"),
            event=TelemetryEvent.RUN,
            transports=["websocket"],
            languages=["en"],
            path=config,
        )
        assert decision.send is False
        assert "internal error" in decision.reason

    async def test_nothing_is_posted_when_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        posted: list[str] = []

        async def spy(self: Any, url: str, **kw: Any) -> httpx.Response:
            posted.append(url)
            return httpx.Response(200)

        monkeypatch.setattr(httpx.AsyncClient, "post", spy)
        await report(
            result(),
            settings(),  # no endpoint
            event=TelemetryEvent.RUN,
            transports=["websocket"],
            languages=["en"],
            path=tmp_path / "c.json",
        )
        assert posted == []

    async def test_a_real_send_posts_the_payload_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bodies: list[Any] = []

        async def spy(self: Any, url: str, **kw: Any) -> httpx.Response:
            bodies.append(kw.get("json"))
            return httpx.Response(200)

        monkeypatch.setattr(httpx.AsyncClient, "post", spy)
        config = tmp_path / "c.json"
        mark_notice_shown(config)
        await report(
            result(),
            settings(telemetry_endpoint="https://t.example.com", allowed_hosts="t.example.com"),
            event=TelemetryEvent.RUN,
            transports=["websocket"],
            languages=["en"],
            path=config,
        )
        assert len(bodies) == 1
        assert SENTINEL not in json.dumps(bodies[0])


# --- the CLI --------------------------------------------------------------------------


class TestPrecedence:
    """The order of the switches is load-bearing, so it is pinned."""

    def test_ci_is_checked_before_the_endpoint(self, tmp_path: Path) -> None:
        """Found by CI itself: under GitHub Actions this branch wins, which is correct —
        a CI run must not send even if an endpoint is configured and allowlisted."""
        decision = decide(
            settings(
                ci=True,
                telemetry_endpoint="https://t.example.com",
                allowed_hosts="t.example.com",
            ),
            tmp_path / "c.json",
        )
        assert decision.send is False
        assert "CI" in decision.reason

    def test_explicit_false_beats_everything(self, tmp_path: Path) -> None:
        decision = decide(
            settings(
                telemetry="false",
                telemetry_endpoint="https://t.example.com",
                allowed_hosts="t.example.com",
            ),
            tmp_path / "c.json",
        )
        assert decision.send is False
        assert "OPSEVAL_TELEMETRY=false" in decision.reason


class TestCli:
    runner = CliRunner()

    def test_show_sample_prints_the_payload_and_sends_nothing(self) -> None:
        res = self.runner.invoke(app, ["telemetry", "show", "--sample"])
        assert res.exit_code == 0, res.output
        assert "opseval.telemetry/v1" in res.output
        assert "everything that would be sent" in res.output

    def test_show_says_why_nothing_is_sent(self) -> None:
        res = self.runner.invoke(app, ["telemetry", "show", "--sample"])
        assert "not sending" in res.output

    def test_disable_and_enable_report_the_file(self, tmp_path: Path) -> None:
        import os

        os.environ["XDG_CONFIG_HOME"] = str(tmp_path)
        try:
            off = self.runner.invoke(app, ["telemetry", "disable"])
            assert off.exit_code == 0
            assert "disabled" in off.output
            on = self.runner.invoke(app, ["telemetry", "enable"])
            assert on.exit_code == 0
            assert "sends nothing unless" in on.output
        finally:
            os.environ.pop("XDG_CONFIG_HOME", None)
