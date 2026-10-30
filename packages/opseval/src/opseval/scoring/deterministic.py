# SPDX-License-Identifier: Apache-2.0
"""Deterministic scorers over conversation frames (tool scorers live in ``tools.py``)."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from opseval.models.profile import FrameKind, SutProfile
from opseval.models.record import (
    SCRIPT_EXHAUSTED,
    VOICE_KEY,
    FrameRecord,
    InterruptRecord,
    RunRecord,
    TerminalState,
)
from opseval.models.results import UNREAD_REPLIES, Axis, ScoreResult
from opseval.models.scenario import RESERVED_STOP_AT, ScorerSpec
from opseval.paths import dig
from opseval.redaction import strip_markers
from opseval.scoring.base import NoParams, Scorer, ScoringContext, register


# The params each scorer takes, checked when a scenario loads (ADR-0043). Strict, so YAML's
# "false" is refused rather than read as a truthy string at run time.
class ConnectionStableParams(NoParams):
    ignore_codes: list[int] = Field(default_factory=list)
    """Close codes to tolerate."""


class TurnBudgetParams(NoParams):
    budget: int | None = Field(default=None, ge=1)
    """Customer turns allowed; the scenario's max_turns when unset."""


class PhaseOrderParams(NoParams):
    allow_gaps: bool = False
    """Allow phases to be skipped, as long as the order holds."""


class PayloadEqualsParams(NoParams):
    checkpoint: str = Field(min_length=1)
    path: str = Field(min_length=1)
    """Dotted path into the checkpoint payload (a number indexes a list)."""
    equals: Any
    """The value it must have (any JSON value, null included)."""


class RequiredFieldsParams(NoParams):
    checkpoint: str = Field(min_length=1)
    fields: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)
    """Dotted paths that must be present and not empty."""


class LanguageScriptParams(NoParams):
    script: Literal["arabic", "latin"] = "arabic"
    min_fraction: float | None = Field(default=None, ge=0, le=1)
    """Share of agent messages that must be in the script; the threshold when unset."""


def _spoken_unheard(frame: FrameRecord) -> bool:
    """A spoken agent turn Opseval could not hear (its own STT proven broken, ADR-0053, or a
    fragment the caller cut short, ADR-0055): the agent did answer there, so calling it
    silence would blame it for Opseval's ears or the caller's interruption."""
    payload = frame.payload
    evidence = payload.get(VOICE_KEY) if isinstance(payload, dict) else None
    return isinstance(evidence, dict) and evidence.get("heard") in ("asr_failed", "cut_short")


@register
class NoErrors(Scorer):
    id = "conversation.no_errors"
    reads_text = False
    Params = NoParams
    axis = Axis.FLOW
    description = "Fails if the agent sent any error frame."

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        errors = [f.seq for f in record.received() if f.kind is FrameKind.ERROR]
        return self.result(spec, 0.0 if errors else 1.0, error_frames=errors)


@register
class AgentReplied(Scorer):
    """ADR-0043: the agent answered every customer turn.

    Agent-facing, unlike ``protocol.replies_read``: the profile read the agent's replies, and
    a turn that got nothing back is the agent saying nothing. Without this, a silent agent
    passed every frame check a scripted smoke suite had (ATTENTION I10).

    What answers a turn is ``SutProfile.answers``, the rule the driver waits on: words, a
    checkpoint, a result, an error, or tool calls the agent asked the client to run. A
    notification ("typing…") is not an answer, even with text. A redaction marker is: it
    stands for words the agent did say, and calling them silence would blame the agent for the
    harness's own edit.
    """

    id = "conversation.agent_replied"
    Params = NoParams
    axis = Axis.FLOW
    description = (
        "Every customer turn got an answer: words, a checkpoint, a result, an error or a tool "
        "call. A turn answered with nothing, blank text or only a notification fails."
    )

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        turns: list[int] = []
        answered: list[bool] = []
        for frame in record.frames:
            if frame.direction == "sent" and frame.kind in (
                FrameKind.USER_MESSAGE,
                FrameKind.RESUME,
            ):
                turns.append(frame.seq)
                answered.append(False)
            elif frame.direction == "received" and turns and not answered[-1]:
                answered[-1] = ctx.profile.answers(
                    frame.kind, frame.text, frame.payload
                ) or _spoken_unheard(frame)
        if not turns:
            return self.result(spec, 1.0, vacuous=True, note="no customer turn was sent")
        silent = [n for n, ok in enumerate(answered, start=1) if not ok]
        return self.result(
            spec, (len(turns) - len(silent)) / len(turns), turns=len(turns), unanswered=silent
        )


@register
class ConnectionStable(Scorer):
    id = "conversation.connection_stable"
    reads_text = False
    Params = ConnectionStableParams
    axis = Axis.FLOW
    description = (
        "Fails if the agent dropped the connection, even when the session was resumed "
        "(opt-in; params.ignore_codes lists close codes to tolerate)."
    )

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        ignore = spec.params.get("ignore_codes", [])
        if not isinstance(ignore, list) or not all(isinstance(c, int) for c in ignore):
            return self.infra(spec, "BAD_PARAMS: ignore_codes must be a list of close codes")
        drops = [
            {"at": e.at.isoformat(), "close_code": e.close_code, "detail": e.detail}
            for e in record.transport_events
            if e.kind == "dropped" and e.close_code not in ignore
        ]
        resumed = sum(1 for e in record.transport_events if e.kind == "resumed")
        return self.result(spec, 0.0 if drops else 1.0, drops=drops, resumed=resumed)


@register
class TurnBudget(Scorer):
    id = "conversation.turn_budget"
    reads_text = False
    Params = TurnBudgetParams
    axis = Axis.FLOW
    description = "Customer turns used versus a budget (params.budget, default max_turns)."

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        budget = spec.params.get("budget")
        if budget is None:  # left out or null: the scenario's max_turns, as Params documents
            budget = ctx.scenario.conversation.max_turns
        if not isinstance(budget, int) or budget < 1:
            return self.infra(spec, "BAD_PARAMS: budget must be a positive integer")
        used = max(record.user_turns, 1)
        return self.result(
            spec, min(1.0, budget / used), user_turns=record.user_turns, budget=budget
        )


@register
class PhaseOrder(Scorer):
    id = "flow.phase_order"
    reads_text = False
    Params = PhaseOrderParams
    axis = Axis.FLOW
    description = "Phases first appear in profile order without skipping (params.allow_gaps)."

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        order = ctx.profile.phases
        if not order:
            return self.infra(spec, "NOT_MEASURABLE: the SUT profile declares no phases")
        first: list[str] = []
        for frame in record.received():
            if frame.phase in order and frame.phase not in first:
                first.append(frame.phase)
        if not first:
            return self.infra(spec, "NOT_MEASURABLE: no phase markers were observed")
        idx = [order.index(p) for p in first]
        allow_gaps = bool(spec.params.get("allow_gaps", False))
        problems = []
        for a, b in zip(idx, idx[1:], strict=False):
            if b <= a:
                problems.append(f"{order[b]!r} first appeared after {order[a]!r}")
            elif not allow_gaps and b - a > 1:
                skipped = order[a + 1 : b]
                problems.append(f"skipped {skipped} between {order[a]!r} and {order[b]!r}")
        return self.result(spec, 0.0 if problems else 1.0, observed=first, problems=problems)


@register
class RejectionRouting(Scorer):
    id = "flow.rejection_routing"
    reads_text = False
    Params = NoParams
    axis = Axis.FLOW
    description = "After each rejection, the agent returns to the expected target phase."

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        checks = []
        for itr in record.interrupts:
            if itr.action != "reject" or not itr.expected_target_phase or itr.decision_seq is None:
                continue
            nxt = next(
                (f for f in record.received() if f.seq > itr.decision_seq and f.phase is not None),
                None,
            )
            ok = nxt is not None and nxt.phase == itr.expected_target_phase
            checks.append(
                {
                    "checkpoint": itr.checkpoint,
                    "occurrence": itr.occurrence,
                    "expected": itr.expected_target_phase,
                    "observed": nxt.phase if nxt else None,
                    "ok": ok,
                }
            )
        if not checks:
            return self.result(spec, 1.0, vacuous=True, note="no rejection with an expected phase")
        good = sum(1 for c in checks if c["ok"])
        return self.result(spec, good / len(checks), checks=checks)


@register
class TerminalStateScorer(Scorer):
    id = "output.terminal_state"
    reads_text = False
    Params = NoParams
    version = "2"  # 2: the default stop_at with no commit gate expects goal_reached (ADR-0043)
    axis = Axis.OUTPUT
    description = "The conversation ended where the scenario's stop_at says it should."

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        stop_at = ctx.scenario.conversation.stop_at
        if stop_at == "commit_gate":
            # Stop before the irreversible step, if there is one; with no commit gate the
            # conversation runs to its goal (ADR-0043).
            stop_at = ctx.profile.commit_gate or "goal_reached"
        observed = record.terminal_state
        last_checkpoint = record.interrupts[-1].checkpoint if record.interrupts else None
        if stop_at == "outcome_committed":
            ok = observed is TerminalState.OUTCOME_COMMITTED
        elif stop_at == "goal_reached":
            ok = observed is TerminalState.GOAL_REACHED
        elif stop_at not in RESERVED_STOP_AT:
            ok = observed is TerminalState.STOPPED_AT_CHECKPOINT and last_checkpoint == stop_at
        else:
            return self.infra(spec, f"NOT_MEASURABLE: cannot resolve stop_at {stop_at!r}")
        if not ok and "commit_guard" in record.notes:
            return self.infra(
                spec,
                "COMMIT_GUARD: Opseval refused to answer the commit gate, so the intended "
                "end state could not be reached",
            )
        result = self.result(
            spec,
            1.0 if ok else 0.0,
            expected=stop_at,
            observed=observed.value,
            last_checkpoint=last_checkpoint,
            stop_reason=record.stop_reason,
        )
        if SCRIPT_EXHAUSTED not in record.notes:
            return result
        note = (
            "the scripted turns ran out: this shows the conversation ran to its end without an "
            "error, a timeout or a stop, not that the customer's goal was met"
        )
        return result.model_copy(update={"details": {**result.details, "note": note}})


_dig = dig  # objects by key, lists by index (opseval.paths)


@register
class PayloadEquals(Scorer):
    id = "output.payload_equals"
    reads_text = False
    Params = PayloadEqualsParams
    axis = Axis.OUTPUT
    # 2: a checkpoint the profile does not declare is BAD_PARAMS, not a failure.
    # 3: a numeric path step indexes a list (opseval.paths.dig).
    version = "3"
    description = (
        "A checkpoint payload field has the expected value (params: checkpoint, path, equals; "
        "checks the approved occurrence, else the last one)."
    )

    @classmethod
    def check_params(cls, params: BaseModel, profile: SutProfile) -> list[str]:
        return _declared_checkpoint(params, profile)

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        checkpoint, path = spec.params.get("checkpoint"), spec.params.get("path")
        if (
            not isinstance(checkpoint, str)
            or not isinstance(path, str)
            or "equals" not in spec.params
        ):
            return self.infra(spec, "BAD_PARAMS: needs checkpoint, path and equals")
        expected = spec.params["equals"]
        unknown = _unknown_checkpoint(checkpoint, ctx)
        if unknown:
            return self.infra(spec, unknown)
        chosen = _occurrence(record, checkpoint)
        if chosen is None:
            return self.result(spec, 0.0, note=f"checkpoint {checkpoint!r} was never raised")
        try:
            observed = _dig(chosen.payload, path)
        except KeyError:
            return self.result(
                spec, 0.0, note=f"{path!r} missing from the payload", occurrence=chosen.occurrence
            )
        return self.result(
            spec,
            1.0 if observed == expected else 0.0,
            expected=expected,
            observed=observed,
            occurrence=chosen.occurrence,
        )


def _occurrence(record: RunRecord, checkpoint: str) -> InterruptRecord | None:
    """The approved occurrence of a checkpoint, else its last one, else None."""
    hits = [i for i in record.interrupts if i.checkpoint == checkpoint]
    approved = [i for i in hits if i.action == "approve"]
    return approved[-1] if approved else (hits[-1] if hits else None)


def _declared_checkpoint(params: BaseModel, profile: SutProfile) -> list[str]:
    """``params.checkpoint`` must be one the profile declares: checked at load (ADR-0043), so a
    misspelt name fails ``validate`` instead of being *not measured* on every run."""
    checkpoint = getattr(params, "checkpoint", None)
    if profile.checkpoints and checkpoint not in profile.checkpoints:
        known = ", ".join(sorted(profile.checkpoints))
        return [f"checkpoint: the profile declares no checkpoint {checkpoint!r} (known: {known})"]
    return []


def _unknown_checkpoint(checkpoint: str, ctx: ScoringContext) -> str | None:
    """A checkpoint name the profile does not declare is an authoring mistake: not measured,
    never the agent's failure for not raising it."""
    if ctx.profile.checkpoints and checkpoint not in ctx.profile.checkpoints:
        known = ", ".join(sorted(ctx.profile.checkpoints))
        return f"BAD_PARAMS: the profile declares no checkpoint {checkpoint!r} (known: {known})"
    return None


@register
class RequiredFields(Scorer):
    id = "output.required_fields"
    reads_text = False
    Params = RequiredFieldsParams
    axis = Axis.OUTPUT
    version = "2"  # 2: a numeric path step indexes a list (opseval.paths.dig)
    description = (
        "A checkpoint payload carries every listed field, present and not empty (params: "
        "checkpoint, fields; checks the approved occurrence, else the last one)."
    )

    @classmethod
    def check_params(cls, params: BaseModel, profile: SutProfile) -> list[str]:
        return _declared_checkpoint(params, profile)

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        checkpoint, fields = spec.params.get("checkpoint"), spec.params.get("fields")
        if (
            not isinstance(checkpoint, str)
            or not isinstance(fields, list)
            or not fields
            or not all(isinstance(f, str) and f for f in fields)
        ):
            return self.infra(spec, "BAD_PARAMS: needs checkpoint and a list of field paths")
        unknown = _unknown_checkpoint(checkpoint, ctx)
        if unknown:
            return self.infra(spec, unknown)
        chosen = _occurrence(record, checkpoint)
        if chosen is None:
            # The agent never asked for this approval: its failure, like payload_equals.
            return self.result(spec, 0.0, note=f"checkpoint {checkpoint!r} was never raised")
        missing = []
        for path in fields:
            try:
                value = _dig(chosen.payload, path)
            except KeyError:
                missing.append(path)
                continue
            if value is None or value == "" or value == [] or value == {}:
                missing.append(path)
        return self.result(
            spec,
            (len(fields) - len(missing)) / len(fields),
            missing=missing,
            occurrence=chosen.occurrence,
        )


ARABIC = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
LATIN = re.compile(r"[A-Za-z]")


@register
class LanguageScript(Scorer):
    id = "output.language_script"
    Params = LanguageScriptParams
    axis = Axis.OUTPUT
    description = "Share of agent messages written mainly in the expected script (arabic|latin)."

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        script = spec.params.get("script", "arabic")
        minimum = spec.params.get("min_fraction")
        if minimum is None:  # left out or null: the threshold, as Params documents
            minimum = spec.threshold
        if script not in ("arabic", "latin") or not isinstance(minimum, int | float):
            return self.infra(spec, "BAD_PARAMS: script must be arabic|latin")
        # Markers are the harness's own edit (FR-REC-03), and they are Latin script. Left
        # in, a redaction inside an Arabic reply could push it under the threshold and
        # mark the agent down for something the harness did to the evidence.
        # Markers are stripped because they are the harness's own edit and they are Latin
        # script (FR-REC-03). Messages that become empty are *kept*: a reply that was
        # nothing but a credential is a real failure to answer in the expected script, and
        # dropping it would report that as "not measured" and excuse genuinely blank
        # replies into the bargain.
        texts = [strip_markers(f.text) for f in record.received() if f.text]
        if not texts:
            return self.infra(spec, "NOT_MEASURABLE: the agent sent no text")
        want, other = (ARABIC, LATIN) if script == "arabic" else (LATIN, ARABIC)
        ok = [len(want.findall(t)) > len(other.findall(t)) for t in texts]
        fraction = sum(ok) / len(ok)
        spec_eff = spec.model_copy(update={"threshold": float(minimum)})
        return self.result(spec_eff, fraction, messages=len(texts), in_script=sum(ok))


@register
class ProbesPassed(Scorer):
    """FR-RUN-07: the agent answered the raw protocol probes as the profile declares."""

    id = "protocol.probes_passed"
    reads_text = False
    Params = NoParams
    axis = Axis.OUTPUT
    description = "Every protocol probe the profile declares got the answer it expects."

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        declared = [p.name for p in ctx.profile.probes]
        if not declared:
            return self.result(spec, 1.0, vacuous=True, note="the profile declares no probes")
        if not record.probes:
            # Declared but never run: the attempt died before PROBES, so this is the
            # harness losing the session, not the agent answering wrongly.
            return self.infra(
                spec,
                "NOT_MEASURABLE: the attempt ended before the probes ran "
                f"({record.terminal_state.value})",
                declared=declared,
            )
        failed = [
            {"probe": p.name, "reason": p.reason, "answered": p.answered}
            for p in record.probes
            if not p.passed
        ]
        return self.result(
            spec,
            (len(record.probes) - len(failed)) / len(record.probes),
            probes=len(record.probes),
            failed=failed,
        )


READ_KINDS = frozenset(
    {FrameKind.NEEDS_HUMAN, FrameKind.RESULT, FrameKind.ERROR, FrameKind.NOTIFICATION}
)
"""Kinds that carry meaning without text: the profile recognised the frame for what it is."""


def _shape(payloads: list[object]) -> str:
    """What the unread replies look like, for the message that says how to fix the mapping."""
    objects = [p for p in payloads if isinstance(p, dict) and set(p) != {"_raw"}]
    keyed = [p for p in objects if p]
    if keyed:
        keys = sorted(str(k) for k in keyed[0])[:12]
        return f"the replies have keys {keys}"
    if objects:
        return "the replies are empty JSON objects"
    if any(isinstance(p, dict) for p in payloads):
        return "the replies are not JSON"  # decode_frame keeps undecodable input as {"_raw"}
    return "the replies are JSON, but not objects"


@register
class RepliesRead(Scorer):
    """ADR-0042: Opseval read what the agent sent, so the other checks had something to grade.

    A profile whose ``kind_field`` or ``text_field`` does not match the agent's replies
    leaves the transcript with only the customer in it: judges have nothing to quote, and
    the frame checks (no error frames, the terminal state, the turn budget) pass on nothing.
    That is a harness problem, the mapping is wrong, and left alone it reads as an agent that
    did nothing wrong. On the harness axis a failure invalidates the attempt, so it is *not
    judged*; when every attempt is, the run exits 3 (``verdicts.exit_code``).

    It fails when either holds, counting only the conversation (probe answers are their own
    check, ``protocol.probes_passed``):

    - the agent sent frames and the profile read nothing from any of them: no frame had the
      text field, and none was classified as a checkpoint, result, error or notification;
    - frames were classified as messages, and not one of them had the text field: the kind
      is mapped, the text is not.

    The text field being *present* is what counts, whatever its value: ``""`` or ``null``
    means the path matched and the agent said nothing, which is the agent's failure, and
    hiding it as "not judged" would break the other half of the core rule.
    """

    id = UNREAD_REPLIES
    reads_text = False
    Params = NoParams
    axis = Axis.HARNESS
    description = (
        "Always on. Invalidates the attempt when the agent sent frames but the profile read "
        "nothing from them (no text, checkpoint, result or error): a wrong mapping is not judged."
    )

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        profile = ctx.profile
        probe_replies = {p.received_seq for p in record.probes if p.received_seq is not None}
        frames = [
            f
            for f in record.received()
            if f.kind is not FrameKind.HANDSHAKE and f.seq not in probe_replies
        ]

        def has_text(frame: FrameRecord) -> bool:
            if frame.text is not None:
                return True
            return isinstance(frame.payload, dict) and profile.text_field in frame.payload

        unread = [f for f in frames if not has_text(f) and f.kind not in READ_KINDS]
        messages = [f for f in frames if f.kind is FrameKind.MESSAGE]
        textless = bool(messages) and not any(has_text(f) for f in messages)
        received, unread_count = len(frames), len(unread)
        if not frames or (unread_count < received and not textless):
            return self.result(spec, 1.0, received=received, unread=unread_count)
        if textless:
            shape = _shape([f.payload for f in messages])
            problem = (
                f"{len(messages)} frame(s) were classified as messages and none had text at "
                f"{profile.text_field!r} ({shape}). Fix text_field in the profile."
            )
        else:
            shape = _shape([f.payload for f in unread])
            problem = (
                f"{len(frames)} frame(s) arrived from the agent and the profile read nothing "
                f"from any of them: no text at {profile.text_field!r} and no kind it maps at "
                f"{profile.kind_field!r} ({shape}). Fix kind_field, kinds or text_field in "
                "the profile."
            )
        return self.result(
            spec, 0.0, received=received, unread=unread_count, shape=shape, reason=problem
        )
