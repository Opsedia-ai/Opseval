# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opseval.cli import app
from opseval.config import Settings
from opseval.demo.refund_agent import run_agent
from opseval.errors import AllowedHostError
from opseval.models.results import SuiteResult, VerdictStatus
from opseval.runner import RunOptions, launch

runner = CliRunner()


def test_version() -> None:
    res = runner.invoke(app, ["--version"])
    assert res.exit_code == 0 and "opseval" in res.stdout


class TestEntrypoint:
    """The console script turns an unexpected crash into exit 3, never exit 1 (ADR-0030)."""

    def run_cli(self, monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
        from opseval.cli import entrypoint

        monkeypatch.setattr("sys.argv", ["opseval", *argv])
        with pytest.raises(SystemExit) as info:
            entrypoint()
        return int(info.value.code or 0)

    def test_a_crash_inside_a_run_is_a_harness_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cat_dir: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        async def disk_full(*args: object, **kwargs: object) -> SuiteResult:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr("opseval.cli.launch", disk_full)
        code = self.run_cli(
            monkeypatch,
            "run",
            str(cat_dir / "suites" / "smoke.yaml"),
            "--profile",
            str(cat_dir / "profile.yaml"),
            "--target",
            "ws://127.0.0.1:1",
            "--reports-dir",
            str(tmp_path),
        )
        assert code == 3
        err = capsys.readouterr().err
        assert "No space left on device" in err  # the traceback is kept for the bug report
        assert "not an agent failure" in err

    def test_normal_exit_codes_pass_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self.run_cli(monkeypatch, "--version") == 0


def test_list_scorers() -> None:
    res = runner.invoke(app, ["list-scorers"])
    assert res.exit_code == 0
    for sid in ("flow.phase_order", "tools.forbidden_not_called", "judge.goal_achievement"):
        assert sid in res.stdout


def test_validate_demo_catalogue(cat_dir: Path) -> None:
    res = runner.invoke(
        app,
        [
            "validate",
            str(cat_dir / "suites" / "full.yaml"),
            "--profile",
            str(cat_dir / "profile.yaml"),
        ],
    )
    assert res.exit_code == 0, res.stdout
    assert "4 valid scenario(s)" in res.stdout


def test_validate_reports_problems_with_exit_3(tmp_path: Path, cat_dir: Path) -> None:
    (tmp_path / "x.yaml").write_text(
        "id: x\nname: n\ngoal: g\npersona: {role: r}\n"
        "conversation: {scripted_turns: [hi], stop_at: nowhere}\n",
        encoding="utf-8",
    )
    res = runner.invoke(
        app, ["validate", str(tmp_path), "--profile", str(cat_dir / "profile.yaml")]
    )
    assert res.exit_code == 3
    assert "nowhere" in res.stdout


def test_run_refuses_non_allowlisted_host(
    cat_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPSEVAL_ALLOWED_HOSTS", raising=False)
    res = runner.invoke(
        app,
        [
            "run",
            str(cat_dir / "suites" / "smoke.yaml"),
            "--profile",
            str(cat_dir / "profile.yaml"),
            "--target",
            "wss://agent.example.com/ws",
        ],
    )
    assert res.exit_code == 3
    assert "OPSEVAL_ALLOWED_HOSTS" in res.output


def test_demo_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["demo", "--repeats", "1"])
    assert res.exit_code == 0, res.stdout
    assert "Safe to release" in res.stdout
    run_dirs = list((tmp_path / "reports").iterdir())
    assert len(run_dirs) == 1
    for name in ("manifest.json", "result.json", "report.html"):
        assert (run_dirs[0] / name).exists()
    html = (run_dirs[0] / "report.html").read_text(encoding="utf-8")
    assert "http://" not in html.replace("http://www.w3.org", "") and "<script" not in html


@pytest.mark.parametrize(
    ("bug", "code"), [("skip-approval", 1), ("forget-method", 1), ("crash", 3)]
)
def test_demo_catches_bugs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bug: str, code: int
) -> None:
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["demo", "--repeats", "1", "--bug", bug])
    assert res.exit_code == code, res.stdout


async def test_launch_skips_llm_scenarios_without_keys(cat_dir: Path, settings: Settings) -> None:
    async with run_agent() as url:
        result = await launch(
            cat_dir / "suites" / "full.yaml",
            RunOptions(target=url, profile=cat_dir / "profile.yaml", repeats=1),
            settings,
        )
    by_id = {t.test_id: t for t in result.tests}
    assert by_id["rf-101"].status is VerdictStatus.SKIPPED_PRECONDITION
    assert by_id["rf-001"].status is VerdictStatus.PASSED
    assert result.exit_code == 2  # something was not judged: never green
    saved = SuiteResult.model_validate_json(
        (Path(settings.reports_dir) / result.run_id / "result.json").read_text("utf-8")
    )
    assert saved.exit_code == 2
    manifest = json.loads(
        (Path(settings.reports_dir) / result.run_id / "manifest.json").read_text("utf-8")
    )
    assert manifest["planned"] == ["rf-001", "rf-002", "rf-003", "rf-101"]


@pytest.mark.parametrize("role", ["simulator", "judge"])
async def test_an_llm_endpoint_the_suite_uses_must_be_allowlisted(
    cat_dir: Path, tmp_path: Path, role: str
) -> None:
    """The full suite has an LLM persona graded by a judge, so it uses both roles."""
    settings = Settings(
        allowed_hosts="127.0.0.1",
        reports_dir=str(tmp_path),
        **{f"{role}_base_url": "https://llm.example.com/v1", f"{role}_model": "m"},
    )
    with pytest.raises(AllowedHostError):
        await launch(
            cat_dir / "suites" / "full.yaml",
            RunOptions(target="ws://127.0.0.1:9", profile=cat_dir / "profile.yaml"),
            settings,
        )


async def test_an_llm_endpoint_the_suite_never_uses_is_not_built(
    cat_dir: Path, tmp_path: Path
) -> None:
    """A scripted, deterministic suite sends nothing to an LLM, so an endpoint configured
    for other suites (and not allowlisted) must not stop it."""
    settings = Settings(
        allowed_hosts="127.0.0.1",
        reports_dir=str(tmp_path),
        simulator_base_url="https://llm.example.com/v1",
        simulator_model="m",
        judge_base_url="https://llm.example.com/v1",
        judge_model="m",
    )
    async with run_agent() as url:
        result = await launch(
            cat_dir / "suites" / "smoke.yaml",
            RunOptions(target=url, profile=cat_dir / "profile.yaml", repeats=1),
            settings,
        )
    assert result.exit_code == 0
