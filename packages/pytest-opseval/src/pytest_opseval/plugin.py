# SPDX-License-Identifier: Apache-2.0
"""Run Opseval scenarios as pytest tests.

    pytest --opseval-profile opseval/profile.yaml --opseval-target wss://agent.example.com \\
        opseval/suites/smoke.yaml

Each scenario becomes one item. A file (suite or scenario) is run once, lazily, by the
first of its items; every item then reports its own verdict, with the same mapping as
Opseval's JUnit output, so the core promise holds inside pytest too:

- ``passed``                     → passed
- ``failed``                     → FAILED (the agent's fault)
- ``inconclusive`` (not judged)  → ERROR (a harness problem; never a pass)
- ``skipped_precondition`` / ``blocked`` → skipped, with the reason
- a configuration or harness error for the whole file → ERROR on every item

The plugin is inert unless ``--opseval-profile`` (or the ``opseval_profile`` ini value)
is set. Scenario files are collected from any path; suite files only when passed
explicitly, so a directory never runs the same scenario twice.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from opseval.catalogue import load_catalogue, load_profile
from opseval.config import Settings
from opseval.errors import ConfigError
from opseval.models.results import SuiteResult, TestVerdict, VerdictStatus
from opseval.runner import RunOptions, launch
from opseval.scoring import INVARIANTS, REGISTRY


class NotJudged(Exception):
    """The scenario could not be judged (harness or configuration problem)."""


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("opseval", "Opseval scenarios")
    group.addoption("--opseval-profile", help="SUT profile YAML (or builtin:<name>).")
    group.addoption("--opseval-target", help="Agent URL (ws://, wss://, http(s)://).")
    group.addoption(
        "--opseval-transport", help="websocket | http-json | openai-chat (default websocket)."
    )
    group.addoption("--opseval-repeats", type=int, help="Override the scenarios' repeats.")
    group.addoption(
        "--opseval-allow-commit",
        action="store_true",
        default=None,
        help="Allow scenarios to perform the irreversible outcome.",
    )
    group.addoption("--opseval-reports-dir", help="Where Opseval writes run folders.")
    for name, help_ in (
        ("opseval_profile", "SUT profile for Opseval scenarios"),
        ("opseval_target", "agent URL for Opseval scenarios"),
        ("opseval_transport", "Opseval transport"),
        ("opseval_reports_dir", "where Opseval writes run folders"),
    ):
        parser.addini(name, help_)


def _opt(config: pytest.Config, name: str) -> Any:
    value = config.getoption(f"--{name.replace('_', '-')}")
    if value is None and name != "opseval_repeats" and name != "opseval_allow_commit":
        value = config.getini(name) or None
    return value


def _is_explicit(config: pytest.Config, path: Path) -> bool:
    for arg in config.args:
        candidate = Path(arg.split("::", 1)[0])
        if not candidate.is_absolute():
            candidate = Path(config.invocation_params.dir) / candidate
        if candidate.resolve() == path.resolve():
            return True
    return False


def _kind(path: Path) -> str | None:
    try:
        data = yaml.safe_load(path.read_text("utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    if "include" in data and "id" not in data:
        return "suite"
    if "id" in data and "persona" in data:
        return "scenario"
    return None


def pytest_collect_file(parent: pytest.Collector, file_path: Path) -> pytest.Collector | None:
    config = parent.config
    if file_path.suffix not in (".yaml", ".yml") or not _opt(config, "opseval_profile"):
        return None
    kind = _kind(file_path)
    if kind == "scenario" or (kind == "suite" and _is_explicit(config, file_path)):
        return OpsevalFile.from_parent(parent, path=file_path)
    return None


class OpsevalFile(pytest.File):
    """A scenario or suite file; runs once for all of its scenarios."""

    _error: str | None = None
    _ran = False
    _scenario_ids: list[str]

    def collect(self) -> Iterator[pytest.Item]:
        profile = load_profile(_opt(self.config, "opseval_profile"))
        catalogue, _ = load_catalogue(
            self.path,
            profile,
            known_scorers=REGISTRY.keys(),
            known_invariants=set(INVARIANTS),
        )
        for problem in catalogue.problems:
            yield LoadProblemItem.from_parent(
                self, name=f"load error: {Path(problem.path).name}", message=str(problem)
            )
        self._scenario_ids = [scenario.id for scenario in catalogue.scenarios]
        for scenario in catalogue.scenarios:
            yield ScenarioItem.from_parent(self, name=scenario.id, scenario_name=scenario.name)

    def verdicts(self) -> dict[str, TestVerdict]:
        if not self._ran:
            self._ran = True
            self._run_selected()
        if self._error is not None:
            raise NotJudged(self._error)
        return self._verdicts

    def _run_selected(self) -> None:
        """Run the whole file, or only the selected scenarios after ``-k``/deselection."""
        self._verdicts: dict[str, TestVerdict] = {}
        mine = [i for i in self.session.items if i.parent is self and isinstance(i, ScenarioItem)]
        everything = len(mine) >= len(self._scenario_ids)
        filters: list[list[str]] = [[]] if everything else [[f"id={i.name}"] for i in mine]
        for flt in filters:
            result = self._run(flt)
            if result is None:
                return
            self._verdicts.update({t.test_id: t for t in result.tests})

    def _run(self, filters: list[str]) -> SuiteResult | None:
        config = self.config
        target = _opt(config, "opseval_target")
        if not target:
            self._error = "configuration error: set --opseval-target (or opseval_target)"
            return None
        settings = Settings()
        reports = _opt(config, "opseval_reports_dir")
        if reports:
            settings.reports_dir = str(reports)
        options = RunOptions(
            target=target,
            transport=_opt(config, "opseval_transport") or "websocket",
            profile=_opt(config, "opseval_profile"),
            repeats=config.getoption("--opseval-repeats"),
            allow_commit=bool(config.getoption("--opseval-allow-commit")),
            filters=filters,
        )
        try:
            return asyncio.run(launch(self.path, options, settings))
        except ConfigError as exc:
            self._error = f"configuration error: {exc}"
            return None


class ScenarioItem(pytest.Item):
    def __init__(self, *, scenario_name: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.scenario_name = scenario_name
        self.verdict: TestVerdict | None = None

    def setup(self) -> None:
        verdict = cast(OpsevalFile, self.parent).verdicts().get(self.name)
        if verdict is None:
            raise NotJudged(f"{self.name} was not run (filtered out or stopped early)")
        self.verdict = verdict
        if verdict.status is VerdictStatus.INCONCLUSIVE:
            raise NotJudged(f"not judged: {verdict.reason}")
        if verdict.status in (VerdictStatus.SKIPPED_PRECONDITION, VerdictStatus.BLOCKED):
            pytest.skip(f"{verdict.status.value}: {verdict.reason}")

    def runtest(self) -> None:
        verdict = self.verdict
        if verdict is not None and verdict.status is VerdictStatus.FAILED:
            raise ScenarioFailed(verdict)

    def repr_failure(
        self,
        excinfo: pytest.ExceptionInfo[BaseException],
        style: Any = None,
    ) -> str:
        if isinstance(excinfo.value, ScenarioFailed | NotJudged):
            return str(excinfo.value)
        return str(super().repr_failure(excinfo, style=style))

    def reportinfo(self) -> tuple[Path, int | None, str]:
        return self.path, None, f"opseval scenario {self.name}: {self.scenario_name}"


class ScenarioFailed(AssertionError):
    def __init__(self, verdict: TestVerdict) -> None:
        rate = "not measured" if verdict.pass_rate is None else f"{verdict.pass_rate:.0%}"
        lines = [
            f"{verdict.test_id} [{verdict.criticality.value}] failed "
            f"(pass rate {rate}): {verdict.reason}"
        ]
        for attempt in verdict.attempts:
            for score in attempt.scores:
                if score.status.value == "failed":
                    lines.append(
                        f"  attempt {attempt.attempt_index}: {score.scorer_id} = {score.score}"
                    )
        super().__init__("\n".join(lines))


class LoadProblemItem(pytest.Item):
    def __init__(self, *, message: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.message = message

    def setup(self) -> None:
        raise NotJudged(f"scenario could not be loaded: {self.message}")

    def runtest(self) -> None:  # pragma: no cover - setup always raises
        pass

    def repr_failure(
        self,
        excinfo: pytest.ExceptionInfo[BaseException],
        style: Any = None,
    ) -> str:
        return str(excinfo.value)

    def reportinfo(self) -> tuple[Path, int | None, str]:
        return self.path, None, self.name
