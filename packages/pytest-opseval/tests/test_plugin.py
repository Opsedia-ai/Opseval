# SPDX-License-Identifier: Apache-2.0
"""pytest-opseval: scenarios as pytest items, with Opseval's verdict mapping.

Fault-injection view: an unreachable agent or a configuration problem is an ERROR (not
judged), never FAILED; an agent bug is FAILED, never hidden as skipped or passed.
"""

from __future__ import annotations

import asyncio
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from opseval.demo import catalogue_dir
from opseval.demo.refund_agent import run_agent

CATALOGUE = catalogue_dir()
PROFILE = str(CATALOGUE / "profile.yaml")
SMOKE = str(CATALOGUE / "suites" / "smoke.yaml")


@contextmanager
def agent(bug: str | None = None) -> Iterator[str]:
    """Run the demo agent on its own event loop in a background thread."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    state: dict[str, object] = {}

    async def main() -> None:
        stop = loop.create_future()
        state["stop"] = stop
        async with run_agent(bug=bug) as url:  # type: ignore[arg-type]
            state["url"] = url
            ready.set()
            await stop

    thread = threading.Thread(target=loop.run_until_complete, args=(main(),), daemon=True)
    thread.start()
    assert ready.wait(10)
    try:
        yield str(state["url"])
    finally:
        loop.call_soon_threadsafe(state["stop"].set_result, None)  # type: ignore[attr-defined]
        thread.join(10)
        loop.close()


@pytest.fixture(autouse=True)
def _allow_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSEVAL_ALLOWED_HOSTS", "127.0.0.1")


def run(pytester: pytest.Pytester, *args: str) -> pytest.RunResult:
    reports = pytester.path / "reports"
    return pytester.runpytest_inprocess(
        "-p",
        "no:cacheprovider",
        "-p",
        "no:opseval",  # the entry point, in case the ini does not disable it
        "-p",
        "pytest_opseval.plugin",
        "--opseval-reports-dir",
        str(reports),
        *args,
    )


def test_registered_as_a_pytest_plugin() -> None:
    from importlib.metadata import entry_points

    plugins = {ep.name: ep.value for ep in entry_points(group="pytest11")}
    assert plugins.get("opseval") == "pytest_opseval.plugin"


def test_inert_without_a_profile(pytester: pytest.Pytester) -> None:
    result = run(pytester, SMOKE)
    result.assert_outcomes()  # nothing collected


def test_healthy_agent_passes_every_scenario(pytester: pytest.Pytester) -> None:
    with agent() as url:
        result = run(
            pytester,
            "--opseval-profile",
            PROFILE,
            "--opseval-target",
            url,
            "--opseval-repeats",
            "1",
            SMOKE,
            "-v",
        )
    result.assert_outcomes(passed=3)
    result.stdout.fnmatch_lines(["*rf-001 PASSED*", "*rf-003 PASSED*"])


def test_agent_bug_is_failed_not_skipped(pytester: pytest.Pytester) -> None:
    with agent("skip-approval") as url:
        result = run(
            pytester,
            "--opseval-profile",
            PROFILE,
            "--opseval-target",
            url,
            "--opseval-repeats",
            "1",
            SMOKE,
        )
    outcomes = result.parseoutcomes()
    assert outcomes.get("failed", 0) >= 1 and "error" not in outcomes and "errors" not in outcomes
    result.stdout.fnmatch_lines(["*rf-001*failed (pass rate 0%)*"])


def test_unreachable_agent_is_an_error_never_a_failure(pytester: pytest.Pytester) -> None:
    result = run(
        pytester,
        "--opseval-profile",
        PROFILE,
        "--opseval-target",
        "ws://127.0.0.1:9",
        "--opseval-repeats",
        "1",
        SMOKE,
    )
    result.assert_outcomes(errors=3)
    result.stdout.fnmatch_lines(["*not judged*transport_failure*"])


def test_missing_target_is_an_error(pytester: pytest.Pytester) -> None:
    result = run(pytester, "--opseval-profile", PROFILE, SMOKE)
    result.assert_outcomes(errors=3)
    result.stdout.fnmatch_lines(["*set --opseval-target*"])


def test_directory_collects_scenarios_once_and_skips_llm_ones(pytester: pytest.Pytester) -> None:
    shutil.copytree(CATALOGUE, pytester.path / "cat")
    with agent() as url:
        result = run(
            pytester,
            "--opseval-profile",
            PROFILE,
            "--opseval-target",
            url,
            "--opseval-repeats",
            "1",
            str(pytester.path / "cat"),
        )
    outcomes = result.parseoutcomes()
    # 3 scripted scenarios pass; LLM-persona ones are skipped (no simulator configured).
    # Suite files are not collected from a directory, so nothing runs twice.
    assert outcomes["passed"] == 3 and outcomes["skipped"] >= 1
    assert "failed" not in outcomes


def test_selection_runs_only_the_selected_scenario(pytester: pytest.Pytester) -> None:
    reports = pytester.path / "reports"
    with agent() as url:
        result = run(
            pytester,
            "--opseval-profile",
            PROFILE,
            "--opseval-target",
            url,
            "--opseval-repeats",
            "1",
            SMOKE,
            "-k",
            "rf-002",
        )
    result.assert_outcomes(passed=1, deselected=2)
    runs = list(reports.iterdir())
    assert len(runs) == 1
    assert [p.stem for p in (runs[0] / "scenarios").glob("*.json")] == ["rf-002"] or (
        not (runs[0] / "scenarios").exists()
    )


def test_unloadable_scenario_is_an_error(pytester: pytest.Pytester) -> None:
    bad = pytester.path / "bad.yaml"
    bad.write_text("id: bad\nname: x\ngoal: y\npersona: {role: c}\n", encoding="utf-8")
    result = run(
        pytester, "--opseval-profile", PROFILE, "--opseval-target", "ws://127.0.0.1:9", str(bad)
    )
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*could not be loaded*conversation*"])


def test_ini_options(pytester: pytest.Pytester) -> None:
    with agent() as url:
        ini = pytester.makeini(
            f"[pytest]\nopseval_profile = {PROFILE}\nopseval_target = {url}\n"
            "asyncio_default_fixture_loop_scope = function\n"
        )
        result = run(pytester, "-c", str(ini), "--opseval-repeats", "1", SMOKE, "-k", "rf-001")
    result.assert_outcomes(passed=1, deselected=2)
