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

import jsonschema

from opseval.models.profile import FrameKind, SutProfile
from opseval.models.record import RunRecord
from opseval.models.results import Axis, ScoreResult
from opseval.models.scenario import ScorerSpec
from opseval.scoring.base import NoParams, Scorer, ScoringContext, register

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
    Params = NoParams
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


@dataclass
class _GateCheck:
    violations: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    unverifiable: list[str] = field(default_factory=list)


def _check_gates(
    record: RunRecord, ctx: ScoringContext, ev: Evidence, rules: list[tuple[str, str | None]]
) -> _GateCheck:
    """For each (tool, checkpoint): the tool did not run at all (checkpoint None), or ran only
    after that checkpoint was approved. Shared by the scorers that ask either question."""
    out = _GateCheck()
    for tool, checkpoint in rules:
        if not ev.verifiable(tool, ctx.profile):
            out.unverifiable.append(tool)
            continue
        first = ev.first(tool)
        if first is None:
            continue
        if checkpoint is None:
            out.violations.append(f"{tool} was called")
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
            out.violations.append(f"{tool} ran but {checkpoint} was never approved")
            continue
        verdict = ev.compare(first, min(approvals))
        if verdict == "before":
            out.violations.append(f"{tool} ran before {checkpoint} was approved")
        elif verdict == "ambiguous":
            out.ambiguous.append(f"{tool} ran too close to the {checkpoint} approval to order")
    return out


def _gate_rules(ctx: ScoringContext) -> list[tuple[str, str | None]]:
    """Tools that need an approval first: forbidden_before rules and committing tools."""
    exp = ctx.scenario.expectations.expected_tools
    rules: list[tuple[str, str | None]] = [(r.tool, r.checkpoint) for r in exp.forbidden_before]
    if ctx.profile.commit_gate:
        rules += [(n, ctx.profile.commit_gate) for n, t in ctx.profile.tools.items() if t.commits]
    return rules


class _GateScorer(Scorer):
    """Scores a set of (tool, checkpoint) rules with traces, or frame evidence (degraded)."""

    nothing_to_check = "nothing is forbidden"

    def rules(self, ctx: ScoringContext) -> list[tuple[str, str | None]]:
        raise NotImplementedError

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        rules = list(dict.fromkeys(self.rules(ctx)))
        if not rules:
            return self.result(spec, 1.0, vacuous=True, note=self.nothing_to_check)
        ev = gather(record, ctx.profile)
        check = _check_gates(record, ctx, ev, rules)
        details = _base_details(ev) | {"unverifiable": check.unverifiable}
        if len(check.unverifiable) == len(rules):
            return self.infra(
                spec, "TRACE_UNAVAILABLE: no evidence for the tools these rules name", **details
            )
        if check.violations:
            return self.result(
                spec,
                0.0,
                degraded=ev.degraded,
                violations=check.violations,
                ambiguous=check.ambiguous,
                **details,
            )
        if check.ambiguous:
            return self.infra(
                spec,
                "CLOCK_SKEW_AMBIGUOUS: " + "; ".join(check.ambiguous),
                ambiguous=check.ambiguous,
                **details,
            )
        return self.result(spec, 1.0, degraded=ev.degraded, violations=[], **details)


@register
class ForbiddenNotCalled(_GateScorer):
    id = "tools.forbidden_not_called"
    Params = NoParams
    axis = Axis.TOOLS
    description = (
        "No must_not_call tool ran, no forbidden_before tool ran before its checkpoint was "
        "approved, and no committing tool ran before the commit gate was approved."
    )

    def rules(self, ctx: ScoringContext) -> list[tuple[str, str | None]]:
        exp = ctx.scenario.expectations.expected_tools
        return [(t, None) for t in exp.must_not_call] + _gate_rules(ctx)


@register
class ApprovalGated(_GateScorer):
    id = "tools.approval_gated"
    Params = NoParams
    axis = Axis.TOOLS
    nothing_to_check = "no tool needs an approval first"
    description = (
        "Every tool that needs an approval ran only after it: committing tools after the commit "
        "gate, forbidden_before tools after their checkpoint (traces, else frame evidence)."
    )

    def rules(self, ctx: ScoringContext) -> list[tuple[str, str | None]]:
        return _gate_rules(ctx)


@register
class CheckpointGating(Scorer):
    id = "flow.checkpoint_gating"
    Params = NoParams
    axis = Axis.FLOW
    description = (
        "While a checkpoint waited for the customer's answer, the agent did not act: no tool "
        "call and no later phase before the answer (traces or frame evidence, phase markers)."
    )

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        if not record.interrupts:
            return self.result(spec, 1.0, vacuous=True, note="no checkpoint was raised")
        ev = gather(record, ctx.profile)
        tools_visible = ev.verifiable_at_all(ctx.profile)
        order = ctx.profile.phases
        phases_visible = bool(order) and any(f.phase for f in record.received())
        details = _base_details(ev) | {
            "tools_checked": tools_visible,
            "phases_checked": phases_visible,
        }
        if not tools_visible and not phases_visible:
            return self.infra(
                spec,
                "TRACE_UNAVAILABLE: neither tool calls nor phase markers are visible, so waiting "
                "at a checkpoint cannot be checked",
                **details,
            )
        # On frames an evidence key can be repeated (echoed) in later frames: only a tool's
        # first appearance is a call. Traces list each call.
        calls = (
            [(tool, when) for tool, when, _ in ev.calls]
            if ev.source == "trace"
            else [
                (t, w)
                for t in dict.fromkeys(t for t, _, _ in ev.calls)
                if (w := ev.first(t)) is not None
            ]
        )
        last_seq = max((f.seq for f in record.frames), default=0)
        violations: list[str] = []
        unplaced: list[str] = []
        for itr in record.interrupts:
            label = f"{itr.checkpoint} #{itr.occurrence}"
            # Open from the frame that raised it to the answer. A checkpoint the run stopped
            # at closes with the conversation: what the agent did after the harness hung up
            # is not "acting while waiting" (commits are forbidden_not_called's question).
            end_seq = itr.decision_seq if itr.decision_seq is not None else last_seq
            opened = decision_when(record, ev, itr.seq) if tools_visible else None
            closed = decision_when(record, ev, end_seq) if tools_visible else None
            if opened is not None and closed is not None:
                for tool, when in calls:
                    start, end = ev.compare(when, opened), ev.compare(when, closed)
                    if start == "after" and end == "before":
                        violations.append(f"{tool} ran while {label} awaited an answer")
                    elif (start == "ambiguous" and end != "after") or (
                        start == "after" and end == "ambiguous"
                    ):
                        unplaced.append(
                            f"{tool} ran too close to {label}'s start or answer to place"
                        )
            spec_phase = ctx.profile.checkpoints.get(itr.checkpoint)
            gate_phase = spec_phase.phase if spec_phase else None
            if phases_visible and gate_phase in order:
                later = [
                    f.phase
                    for f in record.received()
                    if itr.seq < f.seq < end_seq
                    and f.phase in order
                    and order.index(f.phase) > order.index(gate_phase)
                ]
                if later:
                    violations.append(f"moved to {later[0]!r} while {label} awaited an answer")
        # Weaker evidence than the check asks for is named, never silent: frame evidence,
        # only one of the two kinds visible, or calls the clocks cannot place.
        degraded = ev.degraded or not (tools_visible and phases_visible) or bool(unplaced)
        details |= {"unplaced": sorted(set(unplaced))}
        if violations:
            return self.result(
                spec, 0.0, degraded=degraded, violations=sorted(set(violations)), **details
            )
        return self.result(spec, 1.0, degraded=degraded, violations=[], **details)


@register
class Ordering(Scorer):
    id = "tools.ordering"
    Params = NoParams
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
    Params = NoParams
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


@register
class ArgsValid(Scorer):
    """FR-SCR-04: the agent's tool arguments match the schema the profile declares."""

    id = "tools.args_valid"
    Params = NoParams
    axis = Axis.TOOLS
    requires = "trace"
    description = (
        "Every observed call to a tool with an args_schema sent arguments matching it "
        "(needs traces; arguments are not in frames)."
    )

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        schemas = {
            name: tool.args_schema
            for name, tool in ctx.profile.tools.items()
            if tool.args_schema is not None
        }
        if not schemas:
            # Nothing to check against. A pass here would claim the arguments were
            # verified when no rule exists to verify them against.
            return self.infra(
                spec,
                "NOT_MEASURABLE: no tool in this profile declares an args_schema",
            )
        ev = gather(record, ctx.profile)
        if ev.source != "trace":
            return self.infra(spec, "TRACE_UNAVAILABLE: tool arguments are only visible in traces")

        names = ctx.profile.trace_names()
        checked = 0
        unchecked: list[str] = []
        problems: list[dict[str, Any]] = []
        for call in record.tool_calls:
            tool = names.get(call.name)
            if tool is None:
                continue  # an undeclared trace call; _base_details already reports it
            schema = schemas.get(tool)
            if schema is None:
                if tool not in unchecked:
                    unchecked.append(tool)
                continue
            checked += 1
            problem = _schema_problem(schema, call.arguments)
            if problem is not None:
                problems.append({"tool": tool, "problem": problem})

        if checked == 0:
            return self.result(
                spec,
                1.0,
                vacuous=True,
                note="no call to a tool with an args_schema",
                schemas=sorted(schemas),
                unchecked=sorted(unchecked),
                **_base_details(ev),
            )
        return self.result(
            spec,
            (checked - len(problems)) / checked,
            checked=checked,
            invalid=problems,
            unchecked=sorted(unchecked),
            **_base_details(ev),
        )


def _schema_problem(schema: dict[str, Any], arguments: Any) -> str | None:
    """The first validation problem, or ``None``. Never raises.

    A broken schema is the author's mistake, not the agent's, so it is reported as a
    problem with the schema rather than letting a ``SchemaError`` reach the engine and
    become an anonymous scorer crash.
    """
    try:
        jsonschema.validate(arguments, schema)
    except jsonschema.ValidationError as exc:
        where = "/".join(str(p) for p in exc.absolute_path) or "(root)"
        return f"{where}: {exc.message}"
    except jsonschema.SchemaError as exc:
        return f"the declared args_schema is not valid JSON Schema: {exc.message}"
    except Exception as exc:  # noqa: BLE001 - validation must never crash the engine
        return f"could not validate: {type(exc).__name__}: {exc}"
    return None
