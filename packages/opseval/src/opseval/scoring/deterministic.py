# SPDX-License-Identifier: Apache-2.0
"""Deterministic scorers over frame evidence.

Tool scorers run in *degraded* mode here: without a trace source they infer tool calls
from evidence keys declared in the SUT profile (e.g. ``order_summary`` proves that
``lookup_order`` ran). Tools with no evidence key are reported as unverifiable, never
as failed.
"""

from __future__ import annotations

import re
from typing import Any

from opseval.models.profile import FrameKind, SutProfile
from opseval.models.record import RunRecord, TerminalState
from opseval.models.results import Axis, ScoreResult
from opseval.models.scenario import RESERVED_STOP_AT, ScorerSpec
from opseval.scoring.base import Scorer, ScoringContext, register


def _evidence(record: RunRecord, profile: SutProfile) -> dict[str, int]:
    """First frame seq at which each tool's evidence key appeared."""
    keys = profile.evidence_keys()
    seen: dict[str, int] = {}
    for frame in record.received():
        if not isinstance(frame.payload, dict):
            continue
        containers: list[Any] = []
        if profile.data_field:
            containers.append(frame.payload.get(profile.data_field))
        if frame.kind is FrameKind.NEEDS_HUMAN:
            containers.append(frame.payload.get(profile.payload_field))
        for box in containers:
            if not isinstance(box, dict):
                continue
            for key in box:
                tool = keys.get(key)
                if tool and tool not in seen:
                    seen[tool] = frame.seq
    return seen


@register
class NoErrors(Scorer):
    id = "conversation.no_errors"
    axis = Axis.FLOW
    description = "Fails if the agent sent any error frame."

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        errors = [f.seq for f in record.received() if f.kind is FrameKind.ERROR]
        return self.result(spec, 0.0 if errors else 1.0, error_frames=errors)


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
        texts = [f.text for f in record.received() if f.text]
        if not texts:
            return self.infra(spec, "NOT_MEASURABLE: the agent sent no text")
        want, other = (ARABIC, LATIN) if script == "arabic" else (LATIN, ARABIC)
        ok = [len(want.findall(t)) > len(other.findall(t)) for t in texts]
        fraction = sum(ok) / len(ok)
        spec_eff = spec.model_copy(update={"threshold": float(minimum)})
        return self.result(spec_eff, fraction, messages=len(texts), in_script=sum(ok))


def _check_tools(
    record: RunRecord, profile: SutProfile, tools: list[str]
) -> tuple[list[str], list[str]]:
    verifiable = [t for t in tools if profile.tools.get(t) and profile.tools[t].evidence_key]
    return verifiable, [t for t in tools if t not in verifiable]


@register
class ExpectedCalled(Scorer):
    id = "tools.expected_called"
    axis = Axis.TOOLS
    description = "Every must_call tool ran (degraded: inferred from evidence keys)."

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        must = ctx.scenario.expectations.expected_tools.must_call
        if not must:
            return self.result(spec, 1.0, vacuous=True, note="no must_call tools")
        verifiable, unverifiable = _check_tools(record, ctx.profile, must)
        if not verifiable:
            return self.infra(
                spec,
                "TRACE_UNAVAILABLE: none of the tools has an evidence key",
                unverifiable=unverifiable,
            )
        seen = _evidence(record, ctx.profile)
        missing = [t for t in verifiable if t not in seen]
        return self.result(
            spec,
            (len(verifiable) - len(missing)) / len(verifiable),
            degraded=True,
            missing=missing,
            unverifiable=unverifiable,
        )


@register
class ForbiddenNotCalled(Scorer):
    id = "tools.forbidden_not_called"
    axis = Axis.TOOLS
    description = (
        "No must_not_call tool ran, no forbidden_before tool ran before its checkpoint was "
        "approved, and no committing tool ran before the commit gate was approved."
    )

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        exp = ctx.scenario.expectations.expected_tools
        rules: list[tuple[str, str | None]] = [(t, None) for t in exp.must_not_call]
        rules += [(r.tool, r.checkpoint) for r in exp.forbidden_before]
        if ctx.profile.commit_gate:
            rules += [
                (n, ctx.profile.commit_gate) for n, t in ctx.profile.tools.items() if t.commits
            ]
        rules = list(dict.fromkeys(rules))
        if not rules:
            return self.result(spec, 1.0, vacuous=True, note="nothing is forbidden")
        seen = _evidence(record, ctx.profile)
        violations, unverifiable = [], []
        for tool, checkpoint in rules:
            if not (ctx.profile.tools.get(tool) and ctx.profile.tools[tool].evidence_key):
                unverifiable.append(tool)
                continue
            if tool not in seen:
                continue
            if checkpoint is None:
                violations.append(f"{tool} was called")
                continue
            approvals = [
                i.decision_seq
                for i in record.interrupts
                if i.checkpoint == checkpoint
                and i.action == "approve"
                and i.decision_seq is not None
            ]
            if not approvals or seen[tool] < min(approvals):
                violations.append(f"{tool} ran before {checkpoint} was approved")
        if len(unverifiable) == len(rules):
            return self.infra(
                spec,
                "TRACE_UNAVAILABLE: no evidence keys for the forbidden tools",
                unverifiable=unverifiable,
            )
        return self.result(
            spec,
            0.0 if violations else 1.0,
            degraded=True,
            violations=violations,
            unverifiable=unverifiable,
        )
