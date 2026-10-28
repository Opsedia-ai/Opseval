# SPDX-License-Identifier: Apache-2.0
"""Scenario and suite schema (``schema: opseval/v1``).

Every model forbids unknown fields so that authoring mistakes fail at load time
instead of surfacing later as fake agent failures.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class Scenario(Strict):
    schema_: str = Field(default=SCHEMA_VERSION, alias="schema")
    id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")]
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

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

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
