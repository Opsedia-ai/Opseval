# SPDX-License-Identifier: Apache-2.0
"""API response shapes. camelCase on the wire; the console mirrors these in types.ts.

An unmeasured number is ``None`` (JSON null), never 0.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class Api(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class RunSummary(Api):
    id: str
    suite: str
    started_at: str | None
    state: str  # running | finished
    gate: str  # judged | not_judged | blocked
    passed: int
    failed: int
    unjudged: int
    flaky: int


class ScoreOut(Api):
    scorer_id: str
    axis: str
    status: str
    score: float | None
    threshold: float
    reason: str | None
    degraded: bool


class AttemptOut(Api):
    index: int
    passed: bool | None
    terminal_state: str
    invalid_reason: str | None
    scores: list[ScoreOut]


class TestOut(Api):
    __test__ = False
    id: str
    name: str
    criticality: str
    status: str
    reason: str | None
    pass_rate: float | None
    flaky: bool
    attempts: list[AttemptOut]


class UsageOut(Api):
    role: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None


class RunDetail(Api):
    summary: RunSummary
    target: str
    profile: str
    gate_status: str
    gate_summary: str
    blocking: list[str]
    warnings: list[str]
    exit_code: int
    tests: list[TestOut]
    usage: list[UsageOut]
    load_errors: list[str]


class ScenarioOut(Api):
    id: str
    title: str
    difficulty: str
    criticality: str
    stop_at: str
    repeats: int
    tags: list[str]
    allow_commit: bool
    language: str
    persona_role: str


class SuiteOut(Api):
    file: str
    display_name: str
    description: str
    include: list[str]
    scenario_count: int
    repeats: int | None = None  # the suite's override; None = scenarios decide
    stop_at: str | None = None


class CheckOut(Api):
    id: str
    question: str
    area: str
    kind: str  # deterministic | judge
    reads: str


class RunUsage(Api):
    run: str
    simulator_tokens: int | None
    judge_tokens: int | None
    cost_usd: float | None


class TrendPoint(Api):
    run: str
    label: str
    pass_rate: float | None


class SettingRow(Api):
    key: str
    value: str | None
    redacted: bool


class Health(Api):
    status: str
    version: str
    reports_dir: str
    catalogue: str | None
    profile: str | None


class Change(Api):
    test_id: str
    name: str
    before: str
    after: str


class AreaDelta(Api):
    area: str
    before: float | None
    after: float | None


class Comparison(Api):
    baseline: str
    candidate: str
    regressed: list[Change]
    improved: list[Change]
    unjudged: list[Change]
    areas: list[AreaDelta]


class Problem(Api):
    path: str
    field: str
    message: str


class CatalogueOut(Api):
    scenarios: list[ScenarioOut]
    problems: list[Problem]


JsonDict = dict[str, Any]


class Turn(Api):
    who: str  # customer | agent
    text: str


class RehearseIn(Api):
    scenario_id: str
    transcript: list[Turn] = []


class RehearseOut(Api):
    turn: Turn
    goal_reached: bool
    model: str | None  # None = scripted, no model involved


class PersonaOut(Api):
    scenario_id: str
    role: str
    goal: str
    flow: str
    language: str
    dialect: str | None
    timezone: str | None
    typing_noise: float
    scripted: bool
    opening: str | None
    simulator_model: str | None
    judge_model: str | None


class LaunchIn(Api):
    suite: str | None = None
    scenarios: list[str] = []
    target: str | None = None
    repeats: int | None = None
    allow_commit: bool = False
    fail_fast: bool = False


class LaunchOut(Api):
    accepted: bool
    token: str
    message: str


class SuiteIn(Api):
    file: str
    display_name: str
    description: str = ""
    include: list[str]
    repeats: int | None = None
    stop_at: str | None = None


class ImportIn(Api):
    family: str = "uncategorised"
    files: list[dict[str, str]]  # {name, text}


class ImportOut(Api):
    imported: list[str]
    problems: list[Problem]


class ReadinessItem(Api):
    id: str
    label: str
    ready: bool
    detail: str  # what a person should do, or what is limited, when not ready
