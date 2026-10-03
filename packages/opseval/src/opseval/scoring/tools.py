# SPDX-License-Identifier: Apache-2.0
"""Tool scorers.

They read tool calls from the best evidence available (ADR-0011):

1. **Traces** (``trace_coverage == "full"``): every call the agent made, with timestamps
   and status, read from the agent's own tracing (e.g. Langfuse). Timestamps come from the
   agent's clock, so comparisons with Opseval's decision times use a skew window;
   anything inside the window is *ambiguous* and reported as ``infra_error``, never as a
   pass or a failure.
2. **Frames** (degraded): evidence keys declared in the SUT profile, or tool calls that
   the endpoint returns in its frames. Tools with no frame evidence are *unverifiable*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from opseval.models.profile import FrameKind, SutProfile
from opseval.models.record import RunRecord
from opseval.models.results import Axis, ScoreResult
from opseval.models.scenario import ScorerSpec
from opseval.scoring.base import Scorer, ScoringContext, register

When = int | datetime  # frame seq (frames) or start time (traces)


@dataclass
class Evidence:
    source: Literal["trace", "frames"]
    calls: list[tuple[str, When, bool]] = field(default_factory=list)  # tool, when, ok
    skew: timedelta = timedelta(0)
    unknown: list[str] = field(default_factory=list)  # trace names not in the profile

    @property
    def degraded(self) -> bool:
        return self.source == "frames"

    def first(self, tool: str) -> When | None:
        times = [w for t, w, _ in self.calls if t == tool]
        return min(times) if times else None

    def verifiable(self, tool: str, profile: SutProfile) -> bool:
        if self.source == "trace":
            return tool in profile.tools
        return profile.frame_verifiable(tool)

    def verifiable_at_all(self, profile: SutProfile) -> bool:
        """Could this evidence show a tool call at all, for any declared tool?

        ``False`` means absence of calls proves nothing, so a reader must not treat an
        empty list as "the agent called no tools".
        """
        return any(self.verifiable(tool, profile) for tool in profile.tools)

    def compare(self, a: When, b: When) -> Literal["before", "after", "ambiguous"]:
        """Is ``a`` clearly before ``b``, clearly after, or too close to tell?"""
        if isinstance(a, datetime) and isinstance(b, datetime):
            if a < b - self.skew:
                return "before"
            if a > b + self.skew:
                return "after"
            return "ambiguous"
        if isinstance(a, int) and isinstance(b, int):
            if a == b:
                return "ambiguous"  # same frame: order unknown
            return "before" if a < b else "after"
        raise TypeError("cannot compare frame positions with timestamps")


def gather(record: RunRecord, profile: SutProfile) -> Evidence:
    skew_ms = profile.traces.clock_skew_ms if profile.traces else 0
    if record.trace_coverage == "full":
        names = profile.trace_names()
        ev = Evidence(source="trace", skew=timedelta(milliseconds=skew_ms))
        for call in sorted(record.tool_calls, key=lambda c: c.started_at):
            tool = names.get(call.name)
            if tool is None:
                ev.unknown.append(call.name)
                continue
            ev.calls.append((tool, call.started_at, call.ok))
        return ev
    ev = Evidence(source="frames")
    keys = profile.evidence_keys()
    for frame in record.received():
        if not isinstance(frame.payload, dict):
            continue
        boxes: list[Any] = []
        if profile.data_field:
            boxes.append(frame.payload.get(profile.data_field))
        if frame.kind is FrameKind.NEEDS_HUMAN:
            boxes.append(frame.payload.get(profile.payload_field))
        for box in boxes:
            if not isinstance(box, dict):
                continue
            for key in box:
                if key in keys:
                    ev.calls.append((keys[key], frame.seq, True))
            listed = box.get(profile.tool_calls_field) if profile.tool_calls_field else None
            for call in listed if isinstance(listed, list) else []:
                if isinstance(call, dict) and isinstance(call.get("name"), str):
                    ev.calls.append((call["name"], frame.seq, True))
    return ev


def decision_when(record: RunRecord, ev: Evidence, seq: int) -> When | None:
    """Position of the decision frame ``seq`` in the evidence's frame of reference."""
    if ev.source == "frames":
        return seq
    frame = next((f for f in record.frames if f.seq == seq), None)
    return frame.at if frame else None


def _base_details(ev: Evidence) -> dict[str, Any]:
    details: dict[str, Any] = {"source": ev.source}
    if ev.unknown:
        details["undeclared_trace_calls"] = sorted(set(ev.unknown))
    return details


@register
class ExpectedCalled(Scorer):
    id = "tools.expected_called"
    axis = Axis.TOOLS
    description = "Every must_call tool ran (trace-backed; degraded to frame evidence)."

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        must = ctx.scenario.expectations.expected_tools.must_call
        if not must:
            return self.result(spec, 1.0, vacuous=True, note="no must_call tools")
        ev = gather(record, ctx.profile)
        verifiable = [t for t in must if ev.verifiable(t, ctx.profile)]
        unverifiable = [t for t in must if t not in verifiable]
        if not verifiable:
            return self.infra(
                spec,
                "TRACE_UNAVAILABLE: no trace and no frame evidence for these tools",
                unverifiable=unverifiable,
            )
        missing = [t for t in verifiable if ev.first(t) is None]
        return self.result(
            spec,
            (len(verifiable) - len(missing)) / len(verifiable),
            degraded=ev.degraded,
            missing=missing,
            unverifiable=unverifiable,
            **_base_details(ev),
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
        ev = gather(record, ctx.profile)
        violations: list[str] = []
        ambiguous: list[str] = []
        unverifiable: list[str] = []
        for tool, checkpoint in rules:
            if not ev.verifiable(tool, ctx.profile):
                unverifiable.append(tool)
                continue
            first = ev.first(tool)
            if first is None:
                continue
            if checkpoint is None:
                violations.append(f"{tool} was called")
                continue
            approvals = [
                w
                for i in record.interrupts
                if i.checkpoint == checkpoint
                and i.action == "approve"
                and i.decision_seq is not None
                and (w := decision_when(record, ev, i.decision_seq)) is not None
            ]
            if not approvals:
                violations.append(f"{tool} ran but {checkpoint} was never approved")
                continue
            verdict = ev.compare(first, min(approvals))
            if verdict == "before":
                violations.append(f"{tool} ran before {checkpoint} was approved")
            elif verdict == "ambiguous":
                ambiguous.append(f"{tool} ran too close to the {checkpoint} approval to order")
        details = _base_details(ev) | {"unverifiable": unverifiable}
        if len(unverifiable) == len(rules):
            return self.infra(
                spec, "TRACE_UNAVAILABLE: no evidence for the forbidden tools", **details
            )
        if violations:
            return self.result(
                spec,
                0.0,
                degraded=ev.degraded,
                violations=violations,
                ambiguous=ambiguous,
                **details,
            )
        if ambiguous:
            return self.infra(
                spec,
                "CLOCK_SKEW_AMBIGUOUS: " + "; ".join(ambiguous),
                ambiguous=ambiguous,
                **details,
            )
        return self.result(spec, 1.0, degraded=ev.degraded, violations=[], **details)


@register
class Ordering(Scorer):
    id = "tools.ordering"
    axis = Axis.TOOLS
    description = "For each expected_tools.ordering rule, 'before' first ran before 'after'."

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        rules = ctx.scenario.expectations.expected_tools.ordering
        if not rules:
            return self.result(spec, 1.0, vacuous=True, note="no ordering rules")
        ev = gather(record, ctx.profile)
        checks: list[dict[str, Any]] = []
        for rule in rules:
            if not (
                ev.verifiable(rule.before, ctx.profile) and ev.verifiable(rule.after, ctx.profile)
            ):
                checks.append({"rule": f"{rule.before} < {rule.after}", "result": "unverifiable"})
                continue
            first_before, first_after = ev.first(rule.before), ev.first(rule.after)
            if first_after is None:
                result = "not_applicable"  # 'after' never ran: nothing to order
            elif first_before is None:
                result = "violated"  # 'after' ran without 'before'
            else:
                cmp = ev.compare(first_before, first_after)
                result = {"before": "ok", "after": "violated", "ambiguous": "ambiguous"}[cmp]
            checks.append({"rule": f"{rule.before} < {rule.after}", "result": result})
        details = _base_details(ev) | {"checks": checks}
        judged = [c for c in checks if c["result"] in ("ok", "violated", "not_applicable")]
        if any(c["result"] == "violated" for c in checks):
            good = sum(1 for c in judged if c["result"] != "violated")
            return self.result(spec, good / len(judged), degraded=ev.degraded, **details)
        if any(c["result"] in ("ambiguous", "unverifiable") for c in checks):
            return self.infra(
                spec, "NOT_MEASURABLE: some ordering rules could not be checked", **details
            )
        return self.result(spec, 1.0, degraded=ev.degraded, **details)


@register
class CallSuccess(Scorer):
    id = "tools.call_success"
    axis = Axis.TOOLS
    requires = "trace"
    description = "Share of the agent's tool calls that succeeded (needs traces)."

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        ev = gather(record, ctx.profile)
        if ev.source != "trace":
            return self.infra(spec, "TRACE_UNAVAILABLE: call status is only known from traces")
        if not ev.calls:
            return self.result(spec, 1.0, vacuous=True, note="no tool calls", **_base_details(ev))
        failed = [t for t, _, ok in ev.calls if not ok]
        return self.result(
            spec,
            (len(ev.calls) - len(failed)) / len(ev.calls),
            calls=len(ev.calls),
            failed=failed,
            **_base_details(ev),
        )
