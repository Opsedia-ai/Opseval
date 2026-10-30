# SPDX-License-Identifier: Apache-2.0
"""Scorer registry and scoring engine.

The engine converts every problem it cannot attribute to the agent (unknown scorer,
exception, missing evidence, contract violation) into ``infra_error``.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError

from opseval.errors import ConfigError
from opseval.langpacks import Spoken, effective_language, graded_language
from opseval.llm import ChatClient, model_family
from opseval.models.profile import SutProfile
from opseval.models.record import RunRecord
from opseval.models.results import (
    UNREAD_REPLIES,
    VOICE_INTELLIGIBLE,
    Axis,
    ScoreResult,
    ScoreStatus,
)
from opseval.models.scenario import Scenario, ScorerSpec


@dataclass
class ScoringContext:
    scenario: Scenario
    profile: SutProfile
    judge: ChatClient | None = None
    simulator_model: str | None = None
    """The persona model, for the independence guard on HARNESS-axis judges (ADR-0016)."""
    judge_independence: str = "error"
    """``error`` | ``warn`` | ``off`` (see ``OPSEVAL_JUDGE_INDEPENDENCE``)."""
    ballot_slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))
    """Caps judge requests in flight. A multi-vote judge fans its ballots out
    concurrently, so without a shared bound peak concurrency would be
    ``judge_concurrency x votes`` and a 3-vote judge could triple the request rate."""
    language: Spoken | None = None
    """The language the attempt is graded against, from the pack recorded at run time
    (:func:`opseval.langpacks.graded_language`). Unset: the packs as they are now."""

    def spoken(self) -> Spoken:
        """What the customer was told to write: :attr:`language`, else resolved now."""
        if self.language is not None:
            return self.language
        conv = self.scenario.conversation
        return effective_language(self.scenario.persona, scripted=not conv.llm_persona)

    def graded_model(self, axis: Axis) -> str | None:
        """The model a judge on ``axis`` grades: the simulator for HARNESS, else the agent."""
        if axis is Axis.HARNESS:
            return self.simulator_model
        return self.profile.declared_model()


class NoParams(BaseModel):
    """A scorer that takes no params: any key given is an authoring mistake."""

    model_config = ConfigDict(extra="forbid", strict=True, use_attribute_docstrings=True)


class Scorer(ABC):
    id: ClassVar[str]
    axis: ClassVar[Axis]
    requires: ClassVar[str] = "frames"  # frames | trace | llm
    description: ClassVar[str] = ""
    version: ClassVar[str] = "1"
    """Bump when the scoring logic changes (``opseval rescore`` reports the difference)."""
    reads_text: ClassVar[bool] = True
    """Whether the score depends on what the agent said (``FrameRecord.text``). On a voice
    attempt whose STT was proven broken (ADR-0053) such a score is not measured
    (``ASR_NO_TRANSCRIPT``), so it is ``True`` unless a scorer says otherwise: a plugin that
    does not say is treated as reading text, the direction that never blames the agent."""
    Params: ClassVar[type[BaseModel] | None] = None
    """The ``params`` this scorer takes, checked when a scenario loads (ADR-0043), so a typo
    fails ``opseval validate`` instead of becoming *not measured* at run time. Every built-in
    declares one (:class:`NoParams` when it takes none); a plugin that does not is unchecked,
    and ``validate`` says so."""

    @classmethod
    def axis_for(cls, spec: ScorerSpec) -> Axis:
        """The axis this spec's scores are on. Default: the class's. A scorer whose params
        choose its axis (``judge.rubric``, ADR-0061) says so here, so a score the engine makes
        for it (a crash, a contract violation, speech it could not hear) lands on the same
        axis as the scorer's own would."""
        return cls.axis

    @classmethod
    def check_params(cls, params: BaseModel, profile: SutProfile) -> list[str]:
        """Problems with valid ``params`` against the profile, found when a scenario loads
        (``field: message``, the field relative to ``params``). Default: none."""
        return []

    @abstractmethod
    async def score(
        self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext
    ) -> ScoreResult: ...

    # helpers ------------------------------------------------------------------------
    def result(
        self, spec: ScorerSpec, score: float, *, degraded: bool = False, **details: Any
    ) -> ScoreResult:
        status = ScoreStatus.PASSED if score >= spec.threshold else ScoreStatus.FAILED
        return ScoreResult(
            scorer_id=self.id,
            axis=self.axis,
            status=status,
            score=round(score, 4),
            threshold=spec.threshold,
            weight=spec.weight,
            degraded=degraded,
            details=details,
            scorer_version=self.version,
        )

    def infra(self, spec: ScorerSpec, reason: str, **details: Any) -> ScoreResult:
        return infra_error(self.id, self.axis, spec, reason, version=self.version, **details)


def infra_error(
    scorer_id: str,
    axis: Axis,
    spec: ScorerSpec,
    reason: str,
    *,
    version: str | None = None,
    **details: Any,
) -> ScoreResult:
    return ScoreResult(
        scorer_id=scorer_id,
        axis=axis,
        status=ScoreStatus.INFRA_ERROR,
        score=None,
        threshold=spec.threshold,
        weight=spec.weight,
        reason=reason,
        details=details,
        scorer_version=version,
    )


def describe_params(cls: type[Scorer]) -> str | None:
    """The params a scorer takes, ``name*`` when required; ``""`` for none; ``None`` when it
    does not say (a plugin without a ``Params`` model, whose params go unchecked)."""
    model = cls.Params
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        return None
    fields = model.model_fields.items()
    return ", ".join(f"{name}{'*' if info.is_required() else ''}" for name, info in fields)


REGISTRY: dict[str, type[Scorer]] = {}


def register(cls: type[Scorer]) -> type[Scorer]:
    if cls.id in REGISTRY:
        raise ValueError(f"duplicate scorer id {cls.id!r}")
    REGISTRY[cls.id] = cls
    return cls


INVARIANTS: dict[str, list[str]] = {
    "phase_order": ["flow.phase_order"],
    "no_errors": ["conversation.no_errors"],
    # forbidden_not_called is the broad check (must_not_call, forbidden_before and the commit
    # gate). Both invariants keep it, so no scenario loses a check it had; effective_specs
    # removes the duplicate, so one defect is never scored twice.
    "no_autonomous_commit": ["tools.forbidden_not_called"],
    "checkpoint_gating": ["tools.forbidden_not_called", "flow.checkpoint_gating"],
}

DEFAULT_PACK = [
    "conversation.no_errors",
    "flow.phase_order",
    "output.terminal_state",
    "tools.forbidden_not_called",
]
"""The pack before profiles shaped it, still used when no profile is known (ADR-0043)."""


def default_pack(scenario: Scenario, profile: SutProfile) -> list[str]:
    """``scorers: default`` for this scenario against this profile (ADR-0043).

    Only checks that can measure something here. A chatbot with no phases and no tools no
    longer gets ``flow.phase_order`` (always *not measured*) or a tool check that passes on
    nothing, so it can reach "Safe to release"; and an expectation the scenario declares is
    always checked, where before ``must_call`` or ``ordering`` under ``scorers: default`` was
    silently never looked at.
    """
    tools = scenario.expectations.expected_tools
    pack = ["conversation.no_errors", "conversation.agent_replied", "output.terminal_state"]
    if profile.phases:
        pack.append("flow.phase_order")
    committing = profile.commit_gate is not None and any(t.commits for t in profile.tools.values())
    if tools.must_not_call or tools.forbidden_before or committing:
        pack.append("tools.forbidden_not_called")
    if tools.must_call:
        pack.append("tools.expected_called")
    if tools.ordering:
        pack.append("tools.ordering")
    return pack + judged_expectations(scenario)


def judged_expectations(scenario: Scenario) -> list[str]:
    """The judges a declared expectation needs to be graded at all (ADR-0048).

    A refusal scenario under ``scorers: default`` was never graded as one, so an agent that
    did what it must refuse passed; policy rules were written down and never read. With no
    judge configured these are *not measured* and named as such, never a silent pass. They
    do not depend on the profile, so callers without one (preflight's roles) count them too.
    """
    out = []
    if scenario.expectations.kind == "refusal":
        out.append("judge.goal_achievement")
    if scenario.expectations.policy:
        out.append("security.policy_adherence")
    return out


ALWAYS_ON = [UNREAD_REPLIES]
"""Harness checks the engine runs on every attempt, whatever the scenario lists (ADR-0042).
A profile that maps nothing of what the agent says would otherwise let an attempt pass on
checks that read nothing, so this cannot be left to each author to remember. They are not
in :func:`effective_specs`, which stays what the scenario asked for: a harness check
measures nothing about the agent, so it must not change which dependencies a scenario
needs."""


VOICE_ALWAYS_ON = [VOICE_INTELLIGIBLE]
"""Added to :data:`ALWAYS_ON` for voice attempts (a record with a voice timeline, ADR-0053)."""


def _voice_degraded(score: ScoreResult, voice: Any) -> ScoreResult:
    """Mark a score degraded when the voice evidence behind it is weak (ADR-0053, ADR-0017):
    an unheard turn (graded on the heard ones), a turn heard only by the second STT or in
    another script degrades every text-reading score, a low transcript confidence every
    judge score. The reason goes in ``details.degraded_by``."""
    if voice is None or score.status is ScoreStatus.INFRA_ERROR:
        return score
    cls = REGISTRY.get(score.scorer_id)
    if cls is None or not cls.reads_text:
        return score
    reasons = [r for r in (voice.unheard, voice.text_degraded) if r]
    if voice.judge_degraded and cls.requires == "llm":
        reasons.append(voice.judge_degraded)
    if not reasons:
        return score
    prior = score.details.get("degraded_by")
    if isinstance(prior, str) and prior:
        reasons.insert(0, prior)  # a scorer's own reason is kept, never overwritten
    details = {**score.details, "degraded_by": "; ".join(reasons)}
    return score.model_copy(update={"degraded": True, "details": details})


def effective_specs(scenario: Scenario, profile: SutProfile | None = None) -> list[ScorerSpec]:
    """What the scenario asked to be scored with. ``scorers: default`` follows the profile
    (:func:`default_pack`); every caller that has a profile passes it, so the runner, the
    engine, preflight and advice agree on one set."""
    if scenario.scorers == "default":
        ids = (
            [*DEFAULT_PACK, *judged_expectations(scenario)]
            if profile is None
            else default_pack(scenario, profile)
        )
        specs = [ScorerSpec(id=i) for i in ids]
    else:
        specs = list(scenario.scorers)
    for inv in scenario.expectations.invariants:
        specs += [ScorerSpec(id=i) for i in INVARIANTS.get(inv, [])]
    seen: set[str] = set()
    out = []
    for spec in specs:
        if spec.id not in seen:
            seen.add(spec.id)
            out.append(spec)
    return out


def independence_conflicts(
    scenarios: Iterable[Scenario],
    profile: SutProfile,
    *,
    judge_model: str | None,
    simulator_model: str | None,
) -> list[str]:
    """Judges that would grade a model from their own vendor family (ADR-0016).

    This is pure configuration, knowable before the first frame is sent, which is why it
    is enforced at launch rather than per score: an ``infra_error`` judge is dropped from
    the measured set by :func:`opseval.verdicts.attempt_verdict`, so refusing a judge
    *during* scoring would quietly lose coverage instead of reporting it.
    """
    judge_family = model_family(judge_model)
    if judge_family is None:
        return []
    agent, simulator = profile.declared_model(), simulator_model
    graded_by_axis = {
        True: (simulator, model_family(simulator)),
        False: (agent, model_family(agent)),
    }
    conflicts: dict[str, str] = {}
    for scenario in scenarios:
        if scenario.is_blocked:
            continue  # skipped before scoring, so its judges never run
        for spec in effective_specs(scenario, profile):
            cls = REGISTRY.get(spec.id)
            if cls is None or cls.requires != "llm":
                continue
            harness = cls.axis is Axis.HARNESS
            graded, graded_family = graded_by_axis[harness]
            if graded_family is None or graded_family != judge_family:
                continue
            subject = "the simulated customer" if harness else "the agent"
            conflicts[spec.id] = (
                f"{spec.id}: the judge ({judge_model}) and {subject} ({graded}) are both "
                f"{judge_family} models, so a pass would grade that family against itself"
            )
    return [conflicts[key] for key in sorted(conflicts)]


def check_judge_independence(
    scenarios: Iterable[Scenario],
    profile: SutProfile,
    *,
    judge_model: str | None,
    simulator_model: str | None,
    mode: str,
    log: Callable[[str], None] = lambda _msg: None,
) -> list[str]:
    """Refuse, or warn about, judges that would grade their own vendor family (ADR-0016).

    Checked before any conversation happens, so a misconfigured run costs nothing. An
    unrecognised ``mode`` is treated as ``error``: the strict reading is the safe one.
    """
    if mode == "off":
        return []
    conflicts = independence_conflicts(
        scenarios, profile, judge_model=judge_model, simulator_model=simulator_model
    )
    if not conflicts:
        return []
    listed = "\n".join(f"  - {c}" for c in conflicts)
    if mode == "warn":
        log(f"warning: judge independence (scores will be marked degraded):\n{listed}")
        return conflicts
    raise ConfigError(
        "judge independence (ADR-0016):\n"
        f"{listed}\n"
        "Use a judge from another vendor, or set OPSEVAL_JUDGE_INDEPENDENCE=warn to score "
        "anyway and mark the results degraded (off disables the check)."
    )


class ScoringEngine:
    def __init__(
        self,
        judge: ChatClient | None = None,
        judge_concurrency: int = 4,
        *,
        simulator_model: str | None = None,
        judge_independence: str = "error",
    ) -> None:
        self.judge = judge
        self.simulator_model = simulator_model
        self.judge_independence = judge_independence
        self._judge_slots = asyncio.Semaphore(max(1, judge_concurrency))
        # Shared by every ballot of every judge, so the fan-out inside a multi-vote
        # judge cannot push more requests at the provider than judge_concurrency.
        self._ballot_slots = asyncio.Semaphore(max(1, judge_concurrency))

    async def score(
        self, record: RunRecord, scenario: Scenario, profile: SutProfile
    ) -> list[ScoreResult]:
        ctx = ScoringContext(
            scenario=scenario,
            profile=profile,
            judge=self.judge,
            simulator_model=self.simulator_model,
            judge_independence=self.judge_independence,
            ballot_slots=self._ballot_slots,
            # The pack resolved when the attempt ran, so a later change to the packs never
            # re-grades a saved run (ADR-0059).
            language=graded_language(
                scenario.persona,
                scripted=not scenario.conversation.llm_persona,
                recorded=record.persona,
            ),
        )
        # An always-on check runs with its own spec, even if the scenario lists it: a listed
        # threshold of 0 would otherwise switch off the check that catches a wrong mapping.
        always = [*ALWAYS_ON, *(VOICE_ALWAYS_ON if record.voice is not None else [])]
        specs = [s for s in effective_specs(scenario, profile) if s.id not in always]
        specs += [ScorerSpec(id=i) for i in always]
        from opseval.scoring.voice import heard_only, voice_findings

        voice = voice_findings(record)
        # Some turns heard, some not: the text checks grade the heard ones (ADR-0053).
        heard = heard_only(record, voice) if voice is not None and voice.unheard else record
        scores = await asyncio.gather(
            *(self._one(record, spec, ctx, voice, heard) for spec in specs)
        )
        return [_voice_degraded(score, voice) for score in scores]

    async def _one(
        self,
        record: RunRecord,
        spec: ScorerSpec,
        ctx: ScoringContext,
        voice: Any = None,
        heard: RunRecord | None = None,
    ) -> ScoreResult:
        cls = REGISTRY.get(spec.id)
        if cls is None:
            return infra_error(spec.id, Axis.OUTPUT, spec, f"UNKNOWN_SCORER: {spec.id}")
        if voice is not None and voice.asr_reason and cls.reads_text:
            # Opseval's own listening was proven broken and no agent turn was heard
            # (ADR-0053): what the agent said is not measured, and the reason names the
            # canary. Never run, so a judge is not paid to grade a transcript that is not
            # there.
            return infra_error(
                cls.id, cls.axis_for(spec), spec, voice.asr_reason, version=cls.version
            )
        if cls.reads_text and heard is not None:
            record = heard
        scorer = cls()
        try:
            if cls.requires == "llm":
                async with self._judge_slots:
                    return await scorer.score(record, spec, ctx)
            return await scorer.score(record, spec, ctx)
        except ValidationError as exc:
            return infra_error(
                cls.id,
                cls.axis_for(spec),
                spec,
                f"CONTRACT_VIOLATION: {exc.errors()[0]['msg']}",
                version=cls.version,
            )
        except Exception as exc:  # noqa: BLE001 - a scorer bug is never the agent's fault
            return infra_error(
                cls.id,
                cls.axis_for(spec),
                spec,
                f"SCORER_ERROR: {type(exc).__name__}: {exc}",
                version=cls.version,
            )
