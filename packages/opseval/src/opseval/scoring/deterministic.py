# SPDX-License-Identifier: Apache-2.0
"""Deterministic scorers over conversation frames (tool scorers live in ``tools.py``)."""

from __future__ import annotations

import re
from typing import Any

from opseval.models.profile import FrameKind
from opseval.models.record import RunRecord, TerminalState
from opseval.models.results import Axis, ScoreResult
from opseval.models.scenario import RESERVED_STOP_AT, ScorerSpec
from opseval.redaction import strip_markers
from opseval.scoring.base import Scorer, ScoringContext, register


@register
class NoErrors(Scorer):
    id = "conversation.no_errors"
    axis = Axis.FLOW
    description = "Fails if the agent sent any error frame."

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        errors = [f.seq for f in record.received() if f.kind is FrameKind.ERROR]
        return self.result(spec, 0.0 if errors else 1.0, error_frames=errors)


@register
class ConnectionStable(Scorer):
    id = "conversation.connection_stable"
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
    axis = Axis.FLOW
    description = "Customer turns used versus a budget (params.budget, default max_turns)."

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        budget = spec.params.get("budget", ctx.scenario.conversation.max_turns)
        if not isinstance(budget, int) or budget < 1:
            return self.infra(spec, "BAD_PARAMS: budget must be a positive integer")
        used = max(record.user_turns, 1)
        return self.result(
            spec, min(1.0, budget / used), user_turns=record.user_turns, budget=budget
        )


@register
class PhaseOrder(Scorer):
    id = "flow.phase_order"
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
    axis = Axis.OUTPUT
    description = "The conversation ended where the scenario's stop_at says it should."

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        stop_at = ctx.scenario.conversation.stop_at
        if stop_at == "commit_gate":
            stop_at = ctx.profile.commit_gate or stop_at
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
        return self.result(
            spec,
            1.0 if ok else 0.0,
            expected=stop_at,
            observed=observed.value,
            last_checkpoint=last_checkpoint,
            stop_reason=record.stop_reason,
        )


def _dig(value: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(path)
        value = value[part]
    return value


@register
class PayloadEquals(Scorer):
    id = "output.payload_equals"
    axis = Axis.OUTPUT
    description = (
        "A checkpoint payload field has the expected value (params: checkpoint, path, equals; "
        "checks the approved occurrence, else the last one)."
    )

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        checkpoint, path = spec.params.get("checkpoint"), spec.params.get("path")
        if (
            not isinstance(checkpoint, str)
            or not isinstance(path, str)
            or "equals" not in spec.params
        ):
            return self.infra(spec, "BAD_PARAMS: needs checkpoint, path and equals")
        expected = spec.params["equals"]
        hits = [i for i in record.interrupts if i.checkpoint == checkpoint]
        if not hits:
            return self.result(spec, 0.0, note=f"checkpoint {checkpoint!r} was never raised")
        approved = [i for i in hits if i.action == "approve"]
        chosen = approved[-1] if approved else hits[-1]
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


def _occurrence(record: RunRecord, checkpoint: str) -> Any:
    """The approved occurrence of a checkpoint, else its last one, else None."""
    hits = [i for i in record.interrupts if i.checkpoint == checkpoint]
    approved = [i for i in hits if i.action == "approve"]
    return approved[-1] if approved else (hits[-1] if hits else None)


@register
class RequiredFields(Scorer):
    id = "output.required_fields"
    axis = Axis.OUTPUT
    description = (
        "A checkpoint payload carries every listed field, present and not empty (params: "
        "checkpoint, fields; checks the approved occurrence, else the last one)."
    )

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        checkpoint, fields = spec.params.get("checkpoint"), spec.params.get("fields")
        if (
            not isinstance(checkpoint, str)
            or not isinstance(fields, list)
            or not fields
            or not all(isinstance(f, str) and f for f in fields)
        ):
            return self.infra(spec, "BAD_PARAMS: needs checkpoint and a list of field paths")
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
    axis = Axis.OUTPUT
    description = "Share of agent messages written mainly in the expected script (arabic|latin)."

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        script = spec.params.get("script", "arabic")
        minimum = spec.params.get("min_fraction", spec.threshold)
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
