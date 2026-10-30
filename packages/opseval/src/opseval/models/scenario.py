# SPDX-License-Identifier: Apache-2.0
"""Scenario and suite schema (``schema: opseval/v1``).

Every model forbids unknown fields so that authoring mistakes fail at load time
instead of surfacing later as fake agent failures.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError

from opseval.models.gate import GatePolicy
from opseval.models.stop import StopPolicy

SCHEMA_VERSION = "opseval/v1"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Criticality(StrEnum):
    BLOCKER = "blocker"
    CRITICAL = "critical"
    NORMAL = "normal"


class Difficulty(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Persona(Strict):
    role: str = Field(min_length=1)
    language: str = "en"
    dialect: str | None = None
    typing_noise: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    timezone: str | None = None


class DecisionAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class Decision(Strict):
    action: DecisionAction
    feedback: str | None = None


class PolicyAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    SCRIPT = "script"  # the simulator decides from the checkpoint payload
    SEQUENCE = "sequence"  # deterministic list of decisions, one per occurrence


class DecisionChannel(StrEnum):
    RESUME = "resume"
    USER_MESSAGE = "user_message"


class CheckpointPolicy(Strict):
    action: PolicyAction = PolicyAction.SCRIPT
    via: DecisionChannel = DecisionChannel.RESUME
    feedback: str | None = None
    sequence: list[Decision] = Field(default_factory=list)
    expected_target_phase: str | None = None
    max_rejections: Annotated[int, Field(ge=0, le=20)] = 3

    @model_validator(mode="after")
    def _check(self) -> CheckpointPolicy:
        if self.action is PolicyAction.SEQUENCE and not self.sequence:
            raise ValueError("action 'sequence' needs a non-empty 'sequence' list")
        if self.action is not PolicyAction.SEQUENCE and self.sequence:
            raise ValueError("'sequence' is only allowed with action 'sequence'")
        if self.action is PolicyAction.APPROVE and self.via is DecisionChannel.USER_MESSAGE:
            raise ValueError(
                "approvals must use via: resume; chat during a pending checkpoint is a rejection"
            )
        return self


class Injection(Strict):
    """A prompt-injection payload the harness plants, word for word, in one customer turn."""

    turn: Annotated[int, Field(ge=1, le=100)]
    """Customer turn to plant it in (1 = the opening message)."""
    payload: str = Field(min_length=1, max_length=4000)

    @field_validator("payload")
    @classmethod
    def _no_markers(cls, value: str) -> str:
        if "[INJECT]" in value or "[/INJECT]" in value:
            raise ValueError("write the payload without [INJECT] markers; Opseval adds them")
        return value


StopAt = str
"""``commit_gate`` (default), ``outcome_committed``, ``goal_reached`` or a checkpoint name."""

RESERVED_STOP_AT = ("commit_gate", "outcome_committed", "goal_reached")


class ConversationSpec(Strict):
    opening_message: str | None = None
    flow: str = ""
    scripted_turns: list[str] = Field(default_factory=list)
    max_turns: Annotated[int, Field(ge=1, le=100)] = 30
    stop_at: StopAt = "commit_gate"
    allow_commit: bool = False
    checkpoint_policy: dict[str, CheckpointPolicy] = Field(default_factory=dict)
    injections: list[Injection] = Field(default_factory=list)
    """Payloads planted verbatim (protected from typing noise) for security judges."""

    @property
    def llm_persona(self) -> bool:
        """An LLM plays the customer (no ``scripted_turns``): it needs a simulator and a
        language pack. Scripted turns are sent verbatim and need neither."""
        return not self.scripted_turns

    @model_validator(mode="after")
    def _check(self) -> ConversationSpec:
        if not self.flow.strip() and not self.scripted_turns:
            raise ValueError("a conversation needs a 'flow' (LLM persona) or 'scripted_turns'")
        for inj in self.injections:
            if inj.turn > self.max_turns:
                raise ValueError(
                    f"injection at turn {inj.turn} is beyond max_turns ({self.max_turns})"
                )
        if self.stop_at == "outcome_committed" and not self.allow_commit:
            raise ValueError(
                "stop_at: outcome_committed performs the irreversible outcome; "
                "set allow_commit: true explicitly (and run with --allow-commit)"
            )
        return self


class ForbiddenBefore(Strict):
    tool: str
    checkpoint: str


class OrderingRule(Strict):
    before: str
    after: str


class ExpectedTools(Strict):
    must_call: list[str] = Field(default_factory=list)
    must_not_call: list[str] = Field(default_factory=list)
    ordering: list[OrderingRule] = Field(default_factory=list)
    forbidden_before: list[ForbiddenBefore] = Field(default_factory=list)

    @model_validator(mode="after")
    def _not_both(self) -> ExpectedTools:
        # Required and forbidden at once fails whatever the agent does: an authoring mistake
        # that would read as an agent failure.
        both = sorted(set(self.must_call) & set(self.must_not_call))
        if both:
            raise ValueError(f"{', '.join(both)} cannot be both in must_call and in must_not_call")
        return self


class Expectations(Strict):
    kind: Literal["completion", "refusal"] = "completion"
    expected_tools: ExpectedTools = Field(default_factory=ExpectedTools)
    expected_outcome: str | None = None
    invariants: list[str] = Field(default_factory=list)
    policy: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(default_factory=list)
    """Rules the agent must not break, one per line, in plain language. Graded by
    ``security.policy_adherence``; without them that scorer is not measurable."""


class ScorerSpec(Strict):
    id: str
    threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0
    weight: Annotated[float, Field(gt=0.0)] = 1.0
    params: dict[str, object] = Field(default_factory=dict)


class ExecutionSpec(Strict):
    repeats: Annotated[int, Field(ge=1, le=20)] = 3
    consistency_threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0
    attempt_timeout_seconds: Annotated[float, Field(gt=0)] = 600.0
    turn_timeout_seconds: Annotated[float, Field(gt=0)] = 120.0


DATASET_FOLDER = "datasets"
DATASET_SUFFIXES = (".csv", ".jsonl")
VARIABLE_NAME = r"^[A-Za-z_][A-Za-z0-9_]*$"
"""A dataset column, and so a placeholder's name (ADR-0060)."""
MATRIX_AXES = ("language", "dialect", "persona")
"""The matrix axes, in the order cells are produced. Each is also a placeholder."""


class DatasetSpec(Strict):
    """Rows that one scenario stands for, one expanded scenario per row (ADR-0060)."""

    path: str
    """Relative to the catalogue root, under ``datasets/``: ``datasets/orders.csv``."""
    allow_blank: list[Annotated[str, Field(pattern=VARIABLE_NAME)]] = Field(default_factory=list)
    """Columns whose cells may be blank. A blank cell anywhere else is a load error."""

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        rel = PurePosixPath(value)
        if (
            "\\" in value
            or rel.is_absolute()
            or len(rel.parts) < 2
            or rel.parts[0] != DATASET_FOLDER
            or any(part in (".", "..") for part in rel.parts)
            or rel.suffix not in DATASET_SUFFIXES
        ):
            raise ValueError(
                f"{value!r}: a dataset is a .csv or .jsonl file under datasets/ at the catalogue "
                "root, written relative to that root (datasets/orders.csv)"
            )
        return value


class PersonaVariant(Strict):
    """One value of the matrix's ``persona`` axis: fields that replace the scenario's own."""

    name: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")] | None = None
    """How ``{{persona}}``, provenance and ``exclude`` name this variant. Without it, the
    first 8 hex digits of SHA-256 over the variant's content: never its index, which would
    change when the personas are reordered."""
    role: str | None = Field(default=None, min_length=1)
    typing_noise: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    timezone: str | None = None

    @model_validator(mode="after")
    def _something(self) -> PersonaVariant:
        if not self.model_fields_set - {"name"}:
            raise ValueError("a persona variant needs at least one of role, typing_noise, timezone")
        return self


class Matrix(Strict):
    """Axes a scenario runs across, one expanded scenario per cell (ADR-0060)."""

    language: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    """BCP-47 tags (``ar-EG``), resolved by the language packs (ADR-0059)."""
    dialect: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    """Dialect names, for the older ``language`` + ``dialect`` spelling."""
    persona: list[PersonaVariant] = Field(default_factory=list)
    exclude: list[dict[str, str | int]] = Field(default_factory=list)
    """Cells left out, each named by some of its axes (``persona`` by its index from 0, or
    by its ``name``)."""

    @model_validator(mode="after")
    def _check(self) -> Matrix:
        axes = {name: getattr(self, name) for name in MATRIX_AXES if getattr(self, name)}
        if not axes:
            raise ValueError("a matrix needs at least one axis: language, dialect or persona")
        for name, values in axes.items():
            seen = [v.model_dump(exclude_none=True) if name == "persona" else v for v in values]
            if any(seen.count(v) > 1 for v in seen):
                raise ValueError(f"{name}: a value is listed twice")
        names = [p.name for p in self.persona if p.name is not None]
        if len(set(names)) != len(names):
            raise ValueError("persona: a name is given to two variants")
        for index, rule in enumerate(self.exclude):
            if not rule:
                raise ValueError(f"exclude.{index}: empty; name the axes of the cells to leave out")
            for key, value in rule.items():
                if key not in axes:
                    raise ValueError(
                        f"exclude.{index}: {key!r} is not an axis of this matrix "
                        f"(axes: {', '.join(axes)})"
                    )
                if key == "persona":
                    if isinstance(value, str):
                        if value not in names:
                            raise ValueError(f"exclude.{index}.persona: no persona named {value!r}")
                        continue
                    if isinstance(value, bool) or not isinstance(value, int):
                        raise ValueError(
                            f"exclude.{index}.persona: name a persona by its index (0, 1, …) "
                            "or its name"
                        )
                    if not 0 <= value < len(self.persona):
                        raise ValueError(
                            f"exclude.{index}.persona: no persona {value} "
                            f"(there are {len(self.persona)})"
                        )
                elif value not in axes[key]:
                    raise ValueError(
                        f"exclude.{index}.{key}: {value!r} is not one of the matrix's {key} values"
                    )
        return self


class ExpandedFrom(Strict):
    """Where an expanded scenario came from. Set by the loader, never written by hand."""

    base_id: str
    hash: Annotated[str, Field(pattern=r"^[0-9a-f]{8}$")]
    """The first 8 hex digits of SHA-256 over the row's values and the matrix cell."""
    dataset: str | None = None
    dataset_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    """SHA-256 of the dataset file as it was read."""
    row: Annotated[int, Field(ge=1)] | None = None
    """The row's line in the dataset file (the CSV header is row 1)."""
    matrix: dict[str, str | int] = Field(default_factory=dict)
    """The matrix cell: ``language``/``dialect`` values, ``persona`` by its name (or content
    hash when it has none)."""


def _id_error(value: str, message: str) -> None:
    """Raise a problem at ``id`` from a model check (it needs ``expanded_from``, which is
    validated after ``id``), so the editor marks the field rather than the whole file."""
    raise ValidationError.from_exception_data(
        "Scenario",
        [
            InitErrorDetails(
                type=PydanticCustomError("value_error", message), loc=("id",), input=value
            )
        ],
    )


class Scenario(Strict):
    schema_: str = Field(default=SCHEMA_VERSION, alias="schema")
    id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}(@[0-9a-f]{8})?$")]
    """Authored ids never hold ``@``: ``<id>@<hash>`` is an expanded scenario's (ADR-0060)."""
    name: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    issue_refs: list[str] = Field(default_factory=list)
    difficulty: Difficulty = Difficulty.MEDIUM
    criticality: Criticality = Criticality.NORMAL
    persona: Persona
    conversation: ConversationSpec
    expectations: Expectations = Field(default_factory=Expectations)
    scorers: list[ScorerSpec] | Literal["default"] = "default"
    execution: ExecutionSpec = Field(default_factory=ExecutionSpec)
    dataset: DatasetSpec | None = None
    """A CSV or JSONL file under ``datasets/``: one scenario per row, ``{{column}}`` in text
    replaced by the row's value (ADR-0060). A plain string is the path."""
    matrix: Matrix | None = None
    """Language, dialect and persona axes: one scenario per cell, times every dataset row."""
    expanded_from: ExpandedFrom | None = None
    """Set by the loader on each expanded scenario; refused in an authored file."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("dataset", mode="before", json_schema_input_type=DatasetSpec | str | None)
    @classmethod
    def _dataset(cls, value: object) -> object:
        # Decided by the input's shape, so a mistake is one problem at its field
        # (``dataset.path``), never a union error from both forms.
        return {"path": value} if isinstance(value, str) else value

    @model_validator(mode="after")
    def _expansion(self) -> Scenario:
        if self.expanded_from is None:
            if "@" in self.id:
                _id_error(
                    self.id, "'@' is reserved for the scenarios a dataset or matrix expands to"
                )
            return self
        if self.dataset is not None or self.matrix is not None:
            raise ValueError("an expanded scenario has no dataset or matrix of its own")
        if self.id != f"{self.expanded_from.base_id}@{self.expanded_from.hash}":
            _id_error(self.id, "an expanded scenario's id is <base_id>@<hash>")
        return self

    @property
    def dataset_spec(self) -> DatasetSpec | None:
        return self.dataset

    @property
    def is_template(self) -> bool:
        """It stands for many scenarios: the loader expands it (ADR-0060)."""
        return self.dataset is not None or self.matrix is not None

    @property
    def base_id(self) -> str:
        """The authored id; an expanded scenario's ``<id>`` before the ``@``."""
        return self.expanded_from.base_id if self.expanded_from else self.id

    @field_validator("schema_")
    @classmethod
    def _schema(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema {value!r}; expected {SCHEMA_VERSION!r}")
        return value

    @field_validator("scorers")
    @classmethod
    def _scorers(cls, value: list[ScorerSpec] | str) -> list[ScorerSpec] | str:
        if isinstance(value, list) and not value:
            raise ValueError("at least one scorer is required (or use 'default')")
        return value

    @property
    def is_blocked(self) -> bool:
        return "blocked" in self.tags


class Suite(Strict):
    name: str
    description: str = ""
    include: list[str] = Field(default_factory=lambda: ["**/*.yaml"])
    exclude: list[str] = Field(default_factory=list)
    filters: list[str] = Field(default_factory=list)
    defaults: dict[str, object] = Field(default_factory=dict)
    overrides: dict[str, object] = Field(default_factory=dict)
    gate: GatePolicy | None = None
    """What blocks the release for this suite (ADR-0032). ``None`` is the standard preset."""
    stop: StopPolicy | None = None
    """When a run of this suite stops early (ADR-0035). ``None`` runs every scenario."""
