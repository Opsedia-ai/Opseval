# SPDX-License-Identifier: Apache-2.0
"""Created artifacts on the run record (FR-REC-01): what the agent created in the system under
test, read from frames as the profile declares, so a test tenant's leftovers can be found."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from opseval.demo.refund_agent import run_agent
from opseval.driver import Driver
from opseval.models.profile import ArtifactSpec, FrameKind, SutProfile
from opseval.models.record import FrameRecord
from opseval.simulator import ScriptedSimulator
from opseval.transport import PythonTransport

from .conftest import make_scenario
from .test_driver_and_faults import drive

NOW = datetime(2026, 10, 19, tzinfo=UTC)


def committing_scenario() -> Any:
    return make_scenario(
        conversation={
            "stop_at": "outcome_committed",
            "allow_commit": True,
            "checkpoint_policy": {
                "proposal_approval": {"action": "approve"},
                "supervisor_review": {"action": "approve"},
            },
        }
    )


async def test_a_refund_the_agent_issued_is_on_the_record(profile: SutProfile) -> None:
    s = committing_scenario()
    async with run_agent() as url:
        rec = await drive(s, profile, url, allow_commit=True)
    assert rec.terminal_state.value == "outcome_committed"
    assert [(a.kind, a.id) for a in rec.created_artifacts] == [("refund", "rf-10482")]


async def test_nothing_declared_means_nothing_looked_for(profile: SutProfile) -> None:
    bare = profile.model_copy(update={"artifacts": []})
    s = committing_scenario()
    async with run_agent() as url:
        rec = await drive(s, bare, url, allow_commit=True)
    assert rec.created_artifacts == ()


def frame(
    seq: int, payload: Any, kind: FrameKind = FrameKind.RESULT, direction: str = "received"
) -> FrameRecord:
    return FrameRecord(seq=seq, at=NOW, direction=direction, kind=kind, payload=payload)  # type: ignore[arg-type]


def extract(
    profile: SutProfile, frames: list[FrameRecord], *specs: ArtifactSpec
) -> list[tuple[str, str, int]]:
    s = make_scenario()
    driver = Driver(
        scenario=s,
        profile=profile.model_copy(update={"artifacts": list(specs)}),
        transport=PythonTransport(lambda: None, profile),  # type: ignore[arg-type]
        simulator=ScriptedSimulator(s, 1),
        run_id="r",
        attempt_index=0,
        seed=1,
    )
    driver._frames = frames
    return [(a.kind, a.id, a.seq) for a in driver._created_artifacts()]


class TestExtraction:
    def test_ids_are_read_from_the_declared_path_and_frame_kinds(self, profile: SutProfile) -> None:
        frames = [
            frame(0, {"data": {"t": {"id": "T-1"}}}),
            frame(1, {"data": {"t": {"id": 42}}}),
            frame(2, {"data": {"t": {"id": "T-1"}}}),  # the same id again: listed once
            frame(3, {"data": {"t": {"id": "T-9"}}}, kind=FrameKind.MESSAGE),  # not a result
            frame(4, {"data": {"t": {"id": "T-8"}}}, direction="sent"),  # ours, not the agent's
        ]
        got = extract(profile, frames, ArtifactSpec(kind="ticket", path="data.t.id"))
        assert got == [("ticket", "T-1", 0), ("ticket", "42", 1)]

    @pytest.mark.parametrize("value", [None, True, {"x": 1}, ["a"]])
    def test_only_a_string_or_number_is_an_id(self, profile: SutProfile, value: Any) -> None:
        got = extract(profile, [frame(0, {"id": value})], ArtifactSpec(kind="x", path="id"))
        assert got == []

    def test_a_missing_path_is_simply_absent(self, profile: SutProfile) -> None:
        got = extract(profile, [frame(0, {"data": "flat"})], ArtifactSpec(kind="x", path="data.id"))
        assert got == []


def test_an_artifact_declaration_must_be_complete() -> None:
    with pytest.raises(ValidationError):
        ArtifactSpec(kind="", path="id")
    with pytest.raises(ValidationError):
        SutProfile.model_validate(
            {"name": "p", "artifacts": [{"kind": "x", "path": "id", "where": "y"}]}
        )


async def test_the_report_and_the_console_show_them(profile: SutProfile, tmp_path: Any) -> None:
    from opseval.models.results import Gate, SuiteResult
    from opseval.reporting import render_html
    from opseval.verdicts import attempt_verdict, fold_attempts

    s = committing_scenario()
    async with run_agent() as url:
        rec = await drive(s, profile, url, allow_commit=True)
    attempt = attempt_verdict(rec, [], "a.json")
    result = SuiteResult(
        opseval_version="0",
        run_id=rec.run_id,
        suite="s",
        target="x",
        profile="p",
        started_at=NOW,
        ended_at=NOW,
        tests=[
            fold_attempts(
                s.model_copy(update={"execution": s.execution.model_copy(update={"repeats": 1})}),
                [attempt],
            )
        ],
        gate=Gate(status="pass", summary="ok"),
        exit_code=0,
    )
    html = render_html(result, [rec], profile)
    assert "Created in the system under test" in html and "refund rf-10482" in html

    pytest.importorskip("fastapi")
    from opseval.server.views import attempt_record
    from opseval.storage import FileStore

    store = FileStore(tmp_path)
    store.write_manifest("r", {"run_id": "r"})
    store.save_record(rec)
    out = attempt_record(store, "r", rec.test_id, rec.attempt_index)
    assert out is not None and out.created_artifacts == ["refund rf-10482"]
