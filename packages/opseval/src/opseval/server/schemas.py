# SPDX-License-Identifier: Apache-2.0
"""API request and response shapes. camelCase on the wire.

These models are the one source of the wire format: the console's TypeScript types are
generated from them (scripts/console_types.py, checked by the test suite). Enum fields use
the core enums themselves, so a new member reaches the console instead of breaking it.

An unmeasured number is ``None`` (JSON null), never 0.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from opseval.models.gate import Preset
from opseval.models.results import Axis, ScoreStatus, SkipKind, StopCause, VerdictStatus
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
    cancel_requested: bool = False  # it stops before its next attempt (ADR-0035)


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
    skip_kind: SkipKind | None = None  # why a skipped scenario was not run
    cut_short_of: int | None = None  # a cancel interrupted it: the repeats it planned


class StopOut(Api):
    """A stop policy (ADR-0035), every field resolved: what a run would do."""

    fail_fast: bool
    max_consecutive_failures: int | None
    stop_on_critical_failure: bool
    stop_on_rate_limit: bool
    max_wall_clock_seconds: float | None


class StoppedOut(Api):
    """Why a run ended before every scenario ran."""

    by: StopCause
    after: str | None
    detail: str


AudioState = Literal[
    "available",
    "pruned",
    "hashes_only",
    "undecodable",
    "missing",
    "key_missing",
    "key_mismatch",
    "key_invalid",
    "extra_missing",
    "too_large",
]
"""What can be done with a turn's recorded audio, decided by the server (ADR-0053, V0d):
``available`` plays; every other state is said in words, never drawn as a broken player.
No key is three states: unset (``key_missing``), malformed (``key_invalid``) and set without
the voice extra (``extra_missing``). ``too_large`` is a whole call over the size a call is
built at (its turns still play one by one)."""


class AudioTurnOut(Api):
    """One spoken turn's recorded audio. Played from ``…/audio/{seq}``."""

    seq: int
    speaker: Literal["caller", "agent"]
    state: AudioState
    start_ms: float  # on the attempt's clock
    duration_ms: float
    encrypted: bool
    note: str | None  # why it cannot be played (None when it can)


class AttemptAudioOut(Api):
    """A voice attempt's audio: every turn, and whether the whole call can be played."""

    turns: list[AudioTurnOut]
    call_state: AudioState  # the whole call (``…/audio/call``): the first turn that stops it
    call_note: str | None
    skipped: list[int]  # undecodable agent turns, left as silence in the whole call


class TranscriptEntry(Api):
    """One thing that happened in a conversation, in order."""

    seq: int
    kind: Literal[
        "message",
        "checkpoint",
        "decision",
        "result",
        "error",
        "notification",
        "unknown",
        "undecodable_audio",
    ]
    who: Literal["customer", "agent"] | None  # a message, or undecodable audio (the agent)
    text: str | None
    phase: str | None
    latency_ms: float | None
    detail: str | None  # the checkpoint, the decision, or an error or result in short
    audio: AudioTurnOut | None = None  # the spoken turn behind it (message, undecodable_audio)


class ToolCallOut(Api):
    name: str
    ok: bool
    arguments: str | None  # JSON, as the trace recorded it (credential values redacted)


class ArtifactOut(Api):
    kind: str
    id: str  # empty when the id itself was redacted
    seq: int  # the frame that reported it
    id_redacted: bool = False
    id_truncated: bool = False


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
    created_artifacts: list[ArtifactOut] = []  # what the agent created (FR-REC-01)
    artifacts_checked: bool = False  # False: not looked for (never "nothing created")
    audio: AttemptAudioOut | None = None  # None: a text attempt, nothing was spoken


class UsageReportedOut(Api):
    """How many answered calls reported each count (ADR-0066)."""

    answered: int
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cached_input_tokens: int
    cost: int


class UsageOut(Api):
    role: str
    model: str
    calls: int
    input_tokens: int | None  # None: a speech role, or a call did not report it (never 0)
    output_tokens: int | None
    cost_usd: float | None
    audio_in_seconds: float | None = None  # STT audio heard; None = not measured (ADR-0054)
    audio_out_seconds: float | None = None  # TTS audio spoken
    characters: int | None = None  # TTS characters spoken
    reasoning_tokens: int | None = None  # part of the output; None = not reported (ADR-0066)
    cached_input_tokens: int | None = None  # part of the input served from cache
    failed_calls: int = 0  # calls with no usable answer: nothing to count
    reported: UsageReportedOut | None = None  # None: written before per-call usage


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
    stop_policy: StopOut | None = None  # the stop policy in force, when there was one
    stopped: StoppedOut | None = None  # set when a stop policy or a cancel ended it early


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
    file: str  # relative to the catalogue, for the editor


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
    stop: StopOut | None = None  # when its runs stop early; None: they never do (ADR-0035)


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
    # True only when every simulator (judge) model's calls reported their tokens; false with a
    # number: the number is a floor ("≥"), what the models that reported them used (ADR-0066)
    simulator_tokens_complete: bool = False
    judge_tokens_complete: bool = False
    cost_usd: float | None  # the known cost; None when no model in the run has a price
    cost_complete: bool = False  # True only when every model in the run has a price
    audio_in_seconds: float | None = None  # STT audio heard in the run; None = not measured
    audio_out_seconds: float | None = None  # TTS audio spoken
    characters: int | None = None  # TTS characters spoken
    per_call: bool = False  # false: written before per-call usage (totals as written)


class MeasuredOut(Api):
    """A sum and what it covers (ADR-0052). ``value`` null: nothing was measured, never 0.
    ``complete`` false with a value: a floor, drawn as partial."""

    value: float | None
    measured: int
    of: int
    complete: bool


class RatioOut(Api):
    """A sum over the measured units of a denominator. ``value`` null: not measured.
    ``complete`` false: the numerator was a floor, so the ratio is too (drawn with "≥")."""

    value: float | None
    measured: int  # units of the denominator measured (and divided by)
    of: int  # units there were
    complete: bool


class UsageBucket(Api):
    key: str  # an ISO date (a day, or a week's Monday) or a run id
    label: str
    start: str
    runs: int


class UsageSeries(Api):
    """One group's values, one per bucket, in the order of the answer's ``buckets``."""

    group: str
    points: list[MeasuredOut]
    total: MeasuredOut


class UsageScopeOut(Api):
    runs: int
    calls: int
    failed_calls: int
    attempts: int
    attempts_without_usage: int
    rescore_calls: int  # of calls, those a run's latest re-score made
    legacy_runs: list[str]  # written before per-call usage: per-model totals as written
    unreadable_runs: list[str]  # records that could not be read: left out entirely, and named
    rescored_runs: list[str]  # runs whose latest re-score's calls are counted
    unreadable_rescores: list[str]  # a re-score that could not be read: its calls left out
    in_flight_excluded: bool


class UsageFiltersOut(Api):
    suite: str | None
    model: str | None
    role: str | None
    since: str | None
    until: str | None
    bucket: Literal["day", "week", "run"]


class UsageFilterOptions(Api):
    suites: list[str]
    roles: list[str]
    models: list[str]
    server_only: list[str]  # filters only server mode applies: refused here, never ignored


class RunCostOut(Api):
    run: str
    suite: str
    started_at: str
    cost_usd: MeasuredOut
    passed_tests: int
    cost_per_passed_test: RatioOut
    per_call: bool  # false: written before per-call usage


class ScenarioCostOut(Api):
    scenario: str
    attempts: int
    attempts_measured: int
    cost_usd: MeasuredOut
    cost_per_attempt: RatioOut
    passed_runs: int  # runs it passed in with every attempt measured
    cost_per_passed_test: RatioOut


class CostDashboard(Api):
    """Evaluation spend (ADR-0066): never the agent's own cost, never $0 for "no price"."""

    filters: UsageFiltersOut
    options: UsageFilterOptions
    scope: UsageScopeOut
    buckets: list[UsageBucket]
    by_role: list[UsageSeries]
    by_model: list[UsageSeries]
    total_usd: MeasuredOut
    cost_per_passed_test: RatioOut
    passed_tests: int
    runs: list[RunCostOut]
    scenarios: list[ScenarioCostOut]
    sources: dict[str, int]  # priced calls by cost source: provider, price_table, unknown


class TokenTotalsOut(Api):
    input: MeasuredOut  # cached included
    cached_input: MeasuredOut
    output: MeasuredOut  # reasoning included
    reasoning: MeasuredOut
    total: MeasuredOut


class ScenarioTokensOut(Api):
    scenario: str
    attempts: int
    attempts_measured: int
    tokens: MeasuredOut
    tokens_per_attempt: RatioOut


class TokensDashboard(Api):
    filters: UsageFiltersOut
    options: UsageFilterOptions
    scope: UsageScopeOut
    buckets: list[UsageBucket]
    by_kind: list[UsageSeries]  # stack: input (not cached), cached_input, output, reasoning
    by_model: list[UsageSeries]
    totals: TokenTotalsOut
    reasoning_share: RatioOut
    tokens_per_attempt: RatioOut
    scenarios: list[ScenarioTokensOut]


class TrendPoint(Api):
    run: str
    label: str
    pass_rate: float | None


class TrendRun(Api):
    run: str
    label: str
    suite: str


class TrendCell(Api):
    """One scenario in one run. ``status`` None means the scenario was not in that run: an
    absence, never a result."""

    run: str
    status: VerdictStatus | None
    pass_rate: float | None
    flaky: bool = False


class ScenarioTrendRow(Api):
    id: str
    name: str
    cells: list[TrendCell]  # in the order of ScenarioTrend.runs
    flips: int  # changes between consecutive judged verdicts, counted within each suite


class ScenarioTrend(Api):
    """Each scenario's verdict across recent finished runs (FR-RPT-06), oldest run first."""

    suites: list[str]  # every suite with a finished run, for the filter
    runs: list[TrendRun]
    scenarios: list[ScenarioTrendRow]


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
    script_exhausted: bool = False
    """A scripted persona has no turns left: the rehearsal ends, which says nothing about
    whether the goal was met (ADR-0043). ``goal_reached`` is never true when this is."""
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


class StopIn(ApiIn):
    """Stop-policy fields (ADR-0035). For a run, a null field keeps the suite's own; for a
    suite, a null field is left out of its ``stop:`` block."""

    fail_fast: bool | None = None
    max_consecutive_failures: Annotated[int, Field(ge=1, le=10_000)] | None = None
    stop_on_critical_failure: bool | None = None
    stop_on_rate_limit: bool | None = None
    max_wall_clock_seconds: Annotated[float, Field(gt=0, le=604_800)] | None = None


class LaunchIn(ApiIn):
    suite: str | None = None
    scenarios: Annotated[list[str], Field(max_length=500)] = []
    target: str | None = None
    repeats: Repeats | None = None
    allow_commit: bool = False
    stop: StopIn | None = None  # per-run overrides of the suite's stop policy


class CancelOut(Api):
    accepted: bool  # False: a cancel was already requested
    run_id: str
    message: str


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
    stop: StopIn | None = None  # omitted: keep the file's stop policy; null: remove it
    overwrite: bool = False  # replacing an existing suite must be asked for


SCENARIO_TEXT_MAX = 200_000
"""The most text one scenario file may have, wherever it comes in."""


class ImportFile(ApiIn):
    name: str
    text: Annotated[str, Field(max_length=SCENARIO_TEXT_MAX)]


class ImportIn(ApiIn):
    family: str = "uncategorised"
    files: Annotated[list[ImportFile], Field(min_length=1, max_length=200)]


class ScenarioSourceOut(Api):
    """One scenario file as it is on disk, for the editor: valid or not."""

    file: str  # relative to the catalogue
    id: str | None  # None when the file does not validate
    text: str
    etag: str  # changes when the file does: a save must name the version it edited


class ScenarioTextIn(ApiIn):
    text: Annotated[str, Field(max_length=SCENARIO_TEXT_MAX)]
    file: str | None = None  # the file being edited: its own id is no clash
    family: str | None = None  # for a new file: the folder under scenarios/
    file_name: str | None = None  # for a new file: its name, checked before it is written


class ScenarioSaveIn(ApiIn):
    text: Annotated[str, Field(max_length=SCENARIO_TEXT_MAX)]
    etag: str


class ScenarioCheckOut(Api):
    """What the loader would say about this text: the same checks as `opseval validate`."""

    ok: bool
    id: str | None
    title: str | None
    problems: list[Problem]
    warnings: list[Problem] = []  # valid, but it will measure less than it looks; never blocks
    expanded: list[str] = []
    """The ids a ``dataset:`` or ``matrix:`` scenario expands to, from the loader (ADR-0060);
    empty for a plain scenario, or when a problem stops the expansion."""


class ImportOut(Api):
    imported: list[str]
    problems: list[Problem]


class ReadinessItem(Api):
    id: str
    label: str
    ready: bool
    detail: str  # what a person should do, or what is limited, when not ready


# --- the guided scenario form (ADR-0048) -------------------------------------------------
#
# The form is filled in the browser; everything it offers, and the YAML it becomes, comes from
# the server: the choices from the profile and the scorer registry, the YAML and its checks
# from the loader. Draft fields are declared once and shared by the request and the response,
# because a request may not nest a response model (scripts/console_types.py).

DraftMode = Literal["scripted", "simulated"]
"""``scripted``: fixed customer turns, no LLM. ``simulated``: an LLM plays the customer."""
DraftAction = Literal["approve", "reject", "script"]
"""``script``: the simulated customer decides from the checkpoint payload (simulated mode only)."""


class _AnswerFields(BaseModel):
    checkpoint: str = Field(min_length=1, max_length=100)
    action: DraftAction = "approve"
    feedback: str | None = Field(default=None, max_length=2_000)
    """What the customer says when rejecting."""


PARAMS_MAX_BYTES = 20_000
PARAMS_MAX_DEPTH = 8


class _CheckFields(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    threshold: float | None = Field(default=None, ge=0, le=1)
    """None keeps the scorer's default (1.0)."""
    params: dict[str, Any] = {}

    @field_validator("params")
    @classmethod
    def _bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        # Bounded before the YAML is written: a deep value would overflow the dumper's
        # recursion, and a large one the editor's 200 KB, as a bare 500 either way.
        stack: list[tuple[Any, int]] = [(value, 1)]
        while stack:
            item, depth = stack.pop()
            if depth > PARAMS_MAX_DEPTH:
                raise ValueError(f"nested more than {PARAMS_MAX_DEPTH} levels deep")
            if isinstance(item, dict):
                stack.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                stack.extend((child, depth + 1) for child in item)
        if len(json.dumps(value, ensure_ascii=False).encode()) > PARAMS_MAX_BYTES:
            raise ValueError(f"more than {PARAMS_MAX_BYTES:,} bytes")
        return value


class _DraftFields(BaseModel):
    id: str = Field(default="", max_length=100)
    name: str = Field(default="", max_length=300)
    goal: str = Field(default="", max_length=4_000)
    criticality: Criticality = Criticality.NORMAL
    tags: Annotated[list[Annotated[str, Field(max_length=60)]], Field(max_length=30)] = []
    role: str = Field(default="customer", max_length=300)
    language: str = Field(default="en", max_length=20)
    dialect: str | None = Field(default=None, max_length=40)
    typing_noise: float = Field(default=0.0, ge=0, le=1)
    mode: DraftMode = "scripted"
    scripted_turns: Annotated[
        list[Annotated[str, Field(max_length=4_000)]], Field(max_length=100)
    ] = []
    opening_message: str | None = Field(default=None, max_length=4_000)
    flow: str = Field(default="", max_length=8_000)
    """What the simulated customer is after and how they behave (simulated mode)."""
    max_turns: int = Field(default=30, ge=1, le=100)
    stop_at: str = Field(default="auto", max_length=100)
    """``auto`` (stop before the irreversible step, if the profile has one; else at the goal),
    ``goal_reached``, or a checkpoint of the profile."""
    refusal: bool = False
    """The agent is expected to decline (``expectations.kind: refusal``)."""
    expected_outcome: str | None = Field(default=None, max_length=2_000)
    must_call: Annotated[list[Annotated[str, Field(max_length=100)]], Field(max_length=50)] = []
    must_not_call: Annotated[list[Annotated[str, Field(max_length=100)]], Field(max_length=50)] = []
    policy: Annotated[list[Annotated[str, Field(max_length=500)]], Field(max_length=50)] = []
    injection: str | None = Field(default=None, max_length=4_000)
    """A prompt-injection payload planted in the first customer turn."""
    use_default_checks: bool = True
    """On: ``scorers: default`` for this scenario and profile, plus any ``checks`` added to it
    (then written out as one list). Off: exactly ``checks``."""
    repeats: int = Field(default=3, ge=1, le=20)
    family: str = Field(default="uncategorised", max_length=64)
    file_name: str | None = Field(default=None, max_length=70)
    """None: named after the id."""


class DraftAnswerIn(ApiIn, _AnswerFields):
    pass


class DraftCheckIn(ApiIn, _CheckFields):
    pass


class ScenarioDraftIn(ApiIn, _DraftFields):
    """A scenario as the form describes it; ``POST /scenarios/draft`` turns it into YAML."""

    answers: Annotated[list[DraftAnswerIn], Field(max_length=50)] = []
    checks: Annotated[list[DraftCheckIn], Field(max_length=50)] = []
    """The checks to run when ``use_default_checks`` is off."""


class DraftAnswerOut(Api, _AnswerFields):
    pass


class DraftCheckOut(Api, _CheckFields):
    pass


class ScenarioDraftOut(Api, _DraftFields):
    """A starting point the server offers: one of the form's templates."""

    answers: list[DraftAnswerOut] = []
    checks: list[DraftCheckOut] = []


class DraftOut(Api):
    """The YAML a draft becomes, and what the loader says about it. Nothing is written."""

    yaml: str
    family: str
    file_name: str
    check: ScenarioCheckOut
    default_checks: list[str]
    """What ``scorers: default`` runs for this scenario against this profile (ADR-0043)."""


class FormCheckpoint(Api):
    name: str
    phase: str | None
    commit_gate: bool
    """Approving it leads to the irreversible outcome: the run stops here unless the scenario
    allows commits, which the form never does."""


class FormTool(Api):
    name: str
    commits: bool


class FormProfile(Api):
    name: str
    phases: list[str]
    checkpoints: list[FormCheckpoint]
    tools: list[FormTool]
    traces: bool
    tool_calls_complete: bool


class FormLanguage(Api):
    code: str
    dialects: list[str]
    stable: bool


ParamKind = Literal[
    "text", "integer", "number", "boolean", "choice", "text_list", "integer_list", "json"
]


class FormParam(Api):
    """One field a check takes, described by the server from the check's own params model."""

    name: str
    kind: ParamKind
    required: bool
    description: str | None
    choices: list[str] | None
    minimum: float | None
    maximum: float | None
    default: Any = None


class FormCheck(Api):
    id: str
    area: Axis
    reads: Literal["chat", "trace", "frames"]
    question: str
    params: list[FormParam] | None
    """None: the check (a plugin) does not declare its params, so the form offers none."""


class FormTemplate(Api):
    id: str
    title: str
    description: str
    draft: ScenarioDraftOut


class FormChoice(Api):
    value: str
    label: str
    help: str | None = None


class FormAction(Api):
    """An answer the customer can give at a checkpoint, and the modes it works in."""

    value: DraftAction
    label: str
    modes: list[DraftMode]


class FormOut(Api):
    """Everything the guided form may offer, decided by the server."""

    profile: FormProfile
    languages: list[FormLanguage]
    checks: list[FormCheck]
    templates: list[FormTemplate]
    stop_at: list[FormChoice]
    """Where a run may stop, for this profile."""
    answer_actions: list[FormAction]
    default_checks: list[str]
    """``scorers: default`` for a scenario with no tool expectations."""
    read_only: bool


# --- accounts and access (ADR-0050) ----------------------------------------------------------

AccessMode = Literal["token", "accounts"]
"""``token``: one shared bearer token, loopback only (ADR-0031). ``accounts``: users,
sessions, roles and API keys (ADR-0050)."""
Capability = Literal[
    "runs:read", "runs:start", "catalogue:write", "catalogue:label", "label:protected", "manage"
]
"""What the caller may do here. The console shows or hides actions by these; it never works
them out from a role."""
RoleName = Literal["viewer", "member", "admin", "owner"]
ScopeName = Literal[
    "runs:read", "runs:start", "catalogue:write", "catalogue:label", "label:protected"
]


class SessionUser(Api):
    kind: Literal["operator", "user", "key"]
    name: str
    """An email, a key's name, or ``operator`` in token mode."""
    role: RoleName | None
    """The role on the served project; ``None`` for the operator or no role at all."""


class SessionOut(Api):
    """Who the console is talking as, and what they may do. Answered to anyone in accounts
    mode (``user`` is ``None`` until signed in)."""

    mode: AccessMode
    user: SessionUser | None
    capabilities: list[Capability]
    has_access: bool
    """Signed in but without a role in this project: the console says so, rather than show
    empty pages."""
    csrf_token: str | None
    """Send it as ``X-CSRF-Token`` on every write made with the session cookie."""
    project: str | None
    task: str | None
    oidc: bool
    """An OIDC sign-in is offered (``GET /auth/oidc/start``)."""


class LoginIn(ApiIn):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class KeyIn(ApiIn):
    name: str = Field(min_length=1, max_length=200)
    scopes: list[ScopeName] = Field(min_length=1, max_length=5)
    tasks: list[str] | None = Field(default=None, max_length=100)
    """Task slugs the key may touch; ``None`` means every task of the project."""
    system: bool = False
    """A key for Opseval's own automation: it can never hold ``label:protected``."""
    expires_days: int | None = Field(default=None, ge=1, le=3650)


class KeyOut(Api):
    public_key: str
    name: str
    secret_prefix: str
    scopes: list[ScopeName]
    tasks: list[str] | None
    system: bool
    created_by: str | None
    created_at: str
    expires_at: str | None
    revoked_at: str | None
    last_used_at: str | None


class KeyCreatedOut(Api):
    key: KeyOut
    secret: str
    """Shown once, never again: only its hash is stored."""
    authorization: str
    """The header value to send: ``Bearer <public>:<secret>``."""


class MemberIn(ApiIn):
    email: str = Field(min_length=3, max_length=320)
    role: RoleName | None
    """The organisation role; ``None`` removes the person from the organisation."""
    project_role: RoleName | None = None
    """A lower role on this project only; raising it is an Enterprise feature. Left out: an
    existing project role is kept. ``null`` sent explicitly: it is cleared."""
    password: str | None = Field(default=None, min_length=12, max_length=1024)
    """A first password, when the email is new. Without one the user signs in with OIDC."""
    oidc_subject: str | None = Field(default=None, min_length=1, max_length=255)
    """Link the person's identity at the configured OIDC issuer (the ID token's ``sub``)."""


class MemberOut(Api):
    email: str
    name: str | None
    org_role: RoleName
    project_role: RoleName | None
    role: RoleName
    disabled: bool


class LabelOut(Api):
    kind: str
    name: str
    label: str
    version: int
    protected: bool


class LabelMoveIn(ApiIn):
    kind: Literal["scenario", "suite", "profile", "dataset"]
    name: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1)
    label: str = Field(min_length=1, max_length=36)


class LabelMoveOut(Api):
    protected: bool


class LabelProtectIn(ApiIn):
    label: str = Field(min_length=1, max_length=36)
    protected: bool
