# SPDX-License-Identifier: Apache-2.0
"""Markdown gate summary (PR comments, CI job summaries) and `opseval summary`."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from opseval.cli import app
from opseval.config import Settings
from opseval.demo.refund_agent import run_agent
from opseval.models.results import Gate, SuiteResult, TestVerdict, VerdictStatus
from opseval.models.scenario import Criticality
from opseval.reporting.markdown import MARKER, markdown_summary
from opseval.runner import RunOptions, launch

NOW = datetime(2026, 9, 24, tzinfo=UTC)


def result(*tests: TestVerdict, exit_code: int = 0, status: str = "pass") -> SuiteResult:
    return SuiteResult(
        opseval_version="0.1.0",
        run_id="20260924T000000Z-abc123",
        suite="smoke",
        target="ws://x",
        profile="p",
        started_at=NOW,
        ended_at=NOW,
        tests=list(tests),
        gate=Gate(status=status, summary="1/2 scenarios passed. Release is BLOCKED by: x."),
        exit_code=exit_code,
    )


def verdict(test_id: str, status: VerdictStatus, **kw: object) -> TestVerdict:
    return TestVerdict(
        test_id=test_id,
        name=kw.pop("name", "a scenario"),
        criticality=Criticality.CRITICAL,
        status=status,
        **kw,  # type: ignore[arg-type]
    )


def test_blocked_gate_lists_every_verdict_honestly() -> None:
    md = markdown_summary(
        result(
            verdict("rf-001", VerdictStatus.PASSED, pass_rate=1.0),
            verdict(
                "rf-002", VerdictStatus.FAILED, pass_rate=0.0, reason="tools.forbidden_not_called"
            ),
            verdict("rf-003", VerdictStatus.INCONCLUSIVE, reason="transport_failure: dropped"),
            verdict("rf-004", VerdictStatus.SKIPPED_PRECONDITION, reason="needs an LLM persona"),
            exit_code=1,
            status="block",
        ),
        report_url="https://github.com/o/r/actions/runs/1/artifacts/2",
    )
    assert md.startswith(MARKER)
    assert "🔴 **Release blocked** · exit code 1 (fail)" in md
    assert "| `rf-002` a scenario | critical | ❌ failed | 0% |" in md
    assert "| `rf-003` a scenario | critical | ⚠️ not judged | not measured |" in md  # never 0%
    assert "⏭️ skipped" in md and "<details><summary>Why</summary>" in md
    assert "[full report](https://github.com/o/r/actions/runs/1/artifacts/2)" in md


def test_scenario_text_cannot_inject_markdown_or_mentions() -> None:
    evil = "@org/team please [click](https://evil.example) `rm -rf` <img src=x>"
    md = markdown_summary(
        result(verdict("id`with`ticks", VerdictStatus.FAILED, name=evil, reason=evil), exit_code=1)
    )
    assert "[click](https://evil.example)" not in md and "<img" not in md
    assert "\\@org/team" in md
    assert "`id'with'ticks`" in md  # backticks cannot break out of the code span


def test_pass_and_load_errors() -> None:
    ok = result(verdict("a", VerdictStatus.PASSED, pass_rate=1.0, flaky=True))
    ok.load_errors.append("bad.yaml: x")
    md = markdown_summary(ok)
    assert "🟢 **Safe to release** · exit code 0 (pass)" in md
    assert "✅ passed · flaky" in md and "1 scenario file(s) could not be loaded" in md


def test_a_passing_gate_with_gaps_is_not_green() -> None:
    """Gate passed (only a normal scenario was skipped) but exit 2: never "Safe to release"."""
    from opseval.reporting import render_html

    res = result(
        verdict("a", VerdictStatus.PASSED, pass_rate=1.0),
        verdict("b", VerdictStatus.SKIPPED_PRECONDITION),
        exit_code=2,
    )
    md = markdown_summary(res)
    assert "🟡 **No blocking issues, not everything passed** · exit code 2" in md
    assert "Safe to release" not in md.split("\n")[3]
    html = render_html(res, [])
    assert "RELEASE: PASS WITH GAPS" in html and "RELEASE: PASS<" not in html


def test_summary_command(cat_dir: Path, tmp_path: Path) -> None:
    settings = Settings(allowed_hosts="127.0.0.1", reports_dir=str(tmp_path / "r"))

    async def go() -> SuiteResult:
        async with run_agent(bug="skip-approval") as url:
            return await launch(
                cat_dir / "suites" / "smoke.yaml",
                RunOptions(target=url, profile=cat_dir / "profile.yaml", repeats=1),
                settings,
            )

    res = asyncio.run(go())
    out = CliRunner().invoke(
        app, ["summary", str(tmp_path / "r" / res.run_id), "--report-url", "https://x/r"]
    )
    assert out.exit_code == 0
    assert out.output.startswith(MARKER) and "❌ failed" in out.output
    assert "[full report](https://x/r)" in out.output

    missing = CliRunner().invoke(app, ["summary", str(tmp_path / "nope")])
    assert missing.exit_code == 3
