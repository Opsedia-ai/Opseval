# SPDX-License-Identifier: Apache-2.0
"""API request and response shapes. camelCase on the wire.

These models are the one source of the wire format: the console's TypeScript types are
generated from them (scripts/console_types.py, checked by the test suite). Enum fields use
the core enums themselves, so a new member reaches the console instead of breaking it.

An unmeasured number is ``None`` (JSON null), never 0.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from opseval.models.gate import Preset
from opseval.models.results import Axis, ScoreStatus, VerdictStatus
from opseval.models.scenario import Criticality, Difficulty


class Api(BaseModel):
    """A response shape."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ApiIn(BaseModel):
    """A request body. Unknown fields are an error: a mistyped ``allowCommitt`` must not
    be silently dropped (the project rule: authoring mistakes fail loudly)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


RunState = Literal["running", "finished", "harness_error"]
GateState = Literal["safe", "gaps", "blocked", "not_judged", "harness_error"]
"""What a run means for the release, decided here once (ADR-0029/0030):

- ``safe``: the gate passed and everything passed, was measured and loaded.
- ``gaps``: the gate passed, but something failed (normal), was skipped or unmeasured.
- ``blocked``: the gate blocked on an agent failure (exit 1).
- ``not_judged``: the gate blocked because too little could be judged (exit 2).
- ``harness_error``: Opseval could not do the job (exit 3, or the run never finished).
"""


class ScenarioProgressOut(Api):
    id: str
    state: Literal["waiting", "running", "done", "skipped"]
    attempts: int  # attempts recorded so far
    repeats: int | None  # attempts planned; None until the scenario has started


class RunProgress(Api):
    """How far a run in flight has got, as the runner itself recorded it."""

    stage: Literal["running", "finishing"]  # finishing: gate, reports and sinks
    scenarios_planned: int
    scenarios_done: int  # done or skipped
    attempts_recorded: int
    current: str | None  # the scenario being run now
    scenarios: list[ScenarioProgressOut]


class RunSummary(Api):
    id: str
    suite: str
    started_at: str | None
    state: RunState
    gate: GateState | None  # None while running: nothing is decided yet
    passed: int | None  # None = not measured (running, or the run could not be done)
    failed: int | None
    unjudged: int | None
    flaky: int | None
    error: str | None = None  # why a harness_error run could not be carried out
    progress: RunProgress | None = None  # only while running


class ScoreOut(Api):
    scorer_id: str
    axis: Axis
    status: ScoreStatus
    score: float | None
    threshold: float
    reason: str | None
    degraded: bool
    vacuous: bool = False


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
    criticality: Criticality
    status: VerdictStatus
    reason: str | None
    pass_rate: float | None
    flaky: bool
    attempts: list[AttemptOut]
    unmeasured_scorers: list[str] = []


class TranscriptEntry(Api):
    """One thing that happened in a conversation, in order."""

    seq: int
    kind: Literal["message", "checkpoint", "decision", "result", "error", "notification", "unknown"]
    who: Literal["customer", "agent"] | None  # only for a message
    text: str | None
    phase: str | None
    latency_ms: float | None
    detail: str | None  # the checkpoint, the decision, or an error or result in short


class ToolCallOut(Api):
    name: str
    ok: bool
    arguments: str | None  # JSON, as the trace recorded it (credential values redacted)


class AttemptRecordOut(Api):
    """The record behind one attempt: what was said and done, not how it was scored."""

    test_id: str
    index: int
    terminal_state: str
    stop_reason: str | None
    started_at: str
    ended_at: str
    language: str | None
    dialect: str | None
    user_turns: int
    trace_coverage: Literal["full", "frames_only"]
    entries: list[TranscriptEntry]
    tool_calls: list[ToolCallOut]
    infra_errors: list[str]
    connection: list[str]  # transport events: dropped, resumed, token refreshed
    probes: list[str]  # protocol probes: "name: passed" or "name: failed (why)"
    planted_injections: list[str]
    redaction_checked: bool  # False: written before value redaction; never checked
    redacted_kinds: list[str]  # the credential kinds taken out; never the values


class UsageOut(Api):
    role: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None


class SinkOut(Api):
    sink: str
    published: bool
    detail: str


class RunDetail(Api):
    summary: RunSummary
    target: str
    profile: str
    gate_status: Literal["pass", "block"]
    gate_summary: str
    blocking: list[str]
    warnings: list[str]
    exit_code: int | None  # None: the run never finished
    safe_to_release: bool
    gate_policy: str | None = None  # "standard", or "strict + flaky=warn"; None before policies
    looser_than_standard: list[str] = []
    ignored: list[str] = []
    tests: list[TestOut]
    usage: list[UsageOut]
    load_errors: list[str]
    degraded_scorers: list[str] = []
    vacuous_scorers: list[str] = []
    unmeasured_scorers: list[str] = []
    sinks: list[SinkOut] = []


class ScenarioOut(Api):
    id: str
    title: str
    difficulty: Difficulty
    criticality: Criticality
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
    problem: str | None = None  # the file could not be read or validated
    gate: GateOut | None = None  # the release gate this suite runs with (ADR-0032)


class GateSettingOut(Api):
    key: str  # "failed.normal", "flaky", ...
    label: str
    help: str
    choices: list[str]  # only what the loader accepts: the console cannot offer more


class GateCatalogOut(Api):
    settings: list[GateSettingOut]
    presets: dict[Preset, dict[str, str]]  # preset -> every setting's value


class GateOut(Api):
    """A suite's gate: what its YAML says, and what that resolves to."""

    preset: Preset
    overrides: dict[str, str]  # only the settings the suite changes from its preset
    effective: dict[str, str]  # every setting, as a run would use it
    describe: str  # "strict + flaky=warn"
    looser_than_standard: list[str]
    yaml: str  # the exact `gate:` block the suite file gets ("" for the plain standard gate)


class GateIn(ApiIn):
    preset: Preset = "standard"
    overrides: dict[str, str] = {}


class CheckOut(Api):
    id: str
    question: str
    area: Axis
    kind: Literal["deterministic", "judge"]
    reads: Literal["chat", "trace", "frames"]


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
    status: Literal["ok"]
    version: str
    reports_dir: str
    catalogue: str | None
    profile: str | None
    read_only: bool = False  # the catalogue cannot be edited (the built-in demo)
    allow_commit: bool = False  # started with `opseval serve --allow-commit`


class Change(Api):
    test_id: str
    name: str
    before: VerdictStatus
    after: VerdictStatus


class AreaDelta(Api):
    area: Axis
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
    who: Literal["customer", "agent"]
    text: str


class TurnIn(ApiIn):
    who: Literal["customer", "agent"]
    text: Annotated[str, Field(max_length=20_000)]


class RehearseIn(ApiIn):
    scenario_id: str
    transcript: Annotated[list[TurnIn], Field(max_length=200)] = []


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


Repeats = Annotated[int, Field(ge=1, le=20)]  # the same bounds as a scenario's execution


class LaunchIn(ApiIn):
    suite: str | None = None
    scenarios: Annotated[list[str], Field(max_length=500)] = []
    target: str | None = None
    repeats: Repeats | None = None
    allow_commit: bool = False
    fail_fast: bool = False


class LaunchOut(Api):
    accepted: bool
    run_id: str
    message: str


class SuiteIn(ApiIn):
    file: str
    display_name: Annotated[str, Field(min_length=1, max_length=200)]
    description: Annotated[str, Field(max_length=2000)] = ""
    include: Annotated[list[str], Field(min_length=1, max_length=500)]
    repeats: Repeats | None = None
    stop_at: str | None = None
    gate: GateIn | None = None  # omitted: keep the file's gate; null: remove it (standard)
    overwrite: bool = False  # replacing an existing suite must be asked for


class ImportFile(ApiIn):
    name: str
    text: Annotated[str, Field(max_length=200_000)]


class ImportIn(ApiIn):
    family: str = "uncategorised"
    files: Annotated[list[ImportFile], Field(min_length=1, max_length=200)]


class ScenarioSourceOut(Api):
    """One scenario's YAML as it is on disk, for the editor."""

    id: str
    file: str  # relative to the catalogue
    text: str
    etag: str  # changes when the file does: a save must name the version it edited


class ScenarioTextIn(ApiIn):
    text: Annotated[str, Field(max_length=200_000)]
    editing: str | None = None  # the scenario being edited, so its own id is no clash


class ScenarioSaveIn(ApiIn):
    text: Annotated[str, Field(max_length=200_000)]
    etag: str


class ScenarioCheckOut(Api):
    """What the loader would say about this text: the same checks as `opseval validate`."""

    ok: bool
    id: str | None
    title: str | None
    problems: list[Problem]


class ImportOut(Api):
    imported: list[str]
    problems: list[Problem]


class ReadinessItem(Api):
    id: str
    label: str
    ready: bool
    detail: str  # what a person should do, or what is limited, when not ready
