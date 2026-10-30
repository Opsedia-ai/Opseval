# SPDX-License-Identifier: Apache-2.0
"""The voice metrics of ADR-0055, computed from a call's spans when the attempt ends.

Every value is raw (one row per turn, or one per attempt) and every row that could not be
measured is ``None`` with a reason, never 0. The rules that keep the one rule:

- **Harness time is never the agent's.** A gap a ``harness`` span (Opseval's TTS or STT)
  overlaps cannot be placed: it is left out and the row is ``degraded``, so a harness stall
  never reads as the agent's latency or dead air.
- **Dead air is only the agent's silence**: the gap between the end of a caller turn and the
  agent's answer, and the pauses inside an agent turn (at least :data:`PAUSE_FLOOR_MS`, so the
  gaps between words are not counted). A gap that ends in caller speech, a planted silence (it
  sits inside the caller's turn) and the time before the caller speaks are never counted.
- **A barge-in the caller makes is never the agent's fault.** Talk-over counts only agent
  speech that *began* while the caller was talking; overlap the caller started (a planted
  interruption, or the caller speaking while the agent still was) is not talk-over. How fast the
  agent stopped after a planted interruption is ``voice.barge_in_yield_ms``.
- **Only the duplex driver can see overlap.** On the turn-based bridge the caller's audio is
  sent as fast as the line takes it, so its span is not its real length: talk-over and
  barge-in yield are not measured there, and say so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from opseval.models.record import (
    VoiceAgentHeard,
    VoiceAgentTurn,
    VoiceBargeIn,
    VoiceCallerTurn,
    VoiceMetric,
    VoiceMetricInputs,
    VoiceMetricName,
    VoiceSpan,
    VoiceTimeline,
)
from opseval.speech.wer import word_error_rate

METRICS_VERSION = "2"
"""The metric rules' version, recorded with the inputs (``VoiceMetricInputs.version``). Bump it
when a rule changes: ``opseval rescore`` then recomputes the rows of a stored call from its
inputs instead of reusing them. 2: review of #91 (play-out per turn, harness spans beside the
agent's path, the agent's transcripts attributed by time and joined per caller turn)."""

PAUSE_FLOOR_MS = 200.0
"""A pause inside an agent turn shorter than this is the gap between words, not dead air."""

NOT_MEASURABLE = "NOT_MEASURABLE"
UNPLACEABLE = "UNPLACEABLE"


@dataclass
class CallerTurn:
    """What the bridge knows about one caller turn beyond its span."""

    turn: int
    text: str
    """What the caller said: the reference for the agent's transcript (before any noise)."""
    start_ms: float = 0.0
    end_ms: float = 0.0
    barge_in: bool = False


@dataclass
class AgentTurn:
    turn: int
    confidence: float | None = None
    heard: str | None = None
    """How Opseval heard the turn (``stt``, ``second_stt``, ``unintelligible``,
    ``asr_failed``, ``cut_short``); None when it was never transcribed."""
    burst: bool = False
    """Its audio arrived faster than real time on the turn-based bridge (see
    :class:`opseval.models.record.VoiceAgentTurn`)."""


@dataclass
class PlannedBargeIn:
    agent_turn: int
    after_ms: int
    fired_at_ms: float | None = None
    """When the caller began talking over the agent; None if it never did."""
    ended_at_ms: float | None = None
    """When the caller finished the interruption."""
    reason: str | None = None
    """Why it never fired (the agent's turn was shorter than ``after_ms``, or never came)."""


@dataclass
class CallLog:
    """Everything the metrics are computed from, filled by the bridge during the call."""

    mode: Literal["turns", "duplex"]
    call_start_ms: float = 0.0
    spans: list[VoiceSpan] = field(default_factory=list)
    callers: list[CallerTurn] = field(default_factory=list)
    agents: list[AgentTurn] = field(default_factory=list)
    barge_ins: list[PlannedBargeIn] = field(default_factory=list)
    heard: list[tuple[float, str]] = field(default_factory=list)
    """Every final transcript the agent sent of the caller, as (arrival, text), redacted."""
    agent_transcripts_seen: bool = False

    def inputs(self) -> VoiceMetricInputs:
        """The record's copy of everything but the spans (layout 8), for ``rescore``."""
        return VoiceMetricInputs(
            version=METRICS_VERSION,
            call_start_ms=max(0.0, self.call_start_ms),
            callers=tuple(
                VoiceCallerTurn(
                    turn=c.turn,
                    text=c.text,
                    start_ms=max(0.0, c.start_ms),
                    end_ms=max(0.0, c.end_ms),
                    barge_in=c.barge_in,
                )
                for c in self.callers
            ),
            agents=tuple(
                VoiceAgentTurn(turn=a.turn, heard=a.heard, confidence=a.confidence, burst=a.burst)
                for a in self.agents
            ),
            agent_heard=(
                tuple(VoiceAgentHeard(at_ms=max(0.0, at), text=t) for at, t in self.heard)
                if self.agent_transcripts_seen
                else None
            ),
            barge_ins=tuple(
                VoiceBargeIn(
                    agent_turn=b.agent_turn,
                    after_ms=b.after_ms,
                    fired_at_ms=b.fired_at_ms,
                    ended_at_ms=b.ended_at_ms,
                    reason=b.reason,
                )
                for b in self.barge_ins
            ),
        )

    @classmethod
    def from_timeline(cls, timeline: VoiceTimeline) -> CallLog | None:
        """The log a stored call was computed from, or None when the record predates the
        inputs (layouts before 8)."""
        found = timeline.inputs
        if found is None or timeline.spans is None:
            return None
        return cls(
            mode=timeline.mode,
            call_start_ms=found.call_start_ms,
            spans=list(timeline.spans),
            callers=[
                CallerTurn(c.turn, c.text, c.start_ms, c.end_ms, c.barge_in) for c in found.callers
            ],
            agents=[AgentTurn(a.turn, a.confidence, a.heard, a.burst) for a in found.agents],
            barge_ins=[
                PlannedBargeIn(b.agent_turn, b.after_ms, b.fired_at_ms, b.ended_at_ms, b.reason)
                for b in found.barge_ins
            ],
            heard=[(h.at_ms, h.text) for h in found.agent_heard or ()],
            agent_transcripts_seen=found.agent_heard is not None,
        )


def _row(
    name: VoiceMetricName,
    value: float | None,
    unit: Literal["ms", "ratio", "wer", "confidence"],
    *,
    turn: int | None = None,
    reason: str | None = None,
    degraded: bool = False,
) -> VoiceMetric:
    return VoiceMetric(
        name=name,
        turn=turn,
        value=None if value is None else round(value, 3),
        unit=unit,
        reason=reason,
        degraded=degraded,
    )


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


@dataclass
class _Turn:
    turn: int
    spans: list[VoiceSpan]

    @property
    def start(self) -> float:
        return min(s.start_ms for s in self.spans)

    @property
    def end(self) -> float:
        return max(s.end_ms for s in self.spans)


def _turns(spans: list[VoiceSpan], speaker: str) -> list[_Turn]:
    found: dict[int, list[VoiceSpan]] = {}
    for span in spans:
        if span.kind == "speech" and span.speaker == speaker and span.turn is not None:
            found.setdefault(span.turn, []).append(span)
    return [
        _Turn(n, sorted(found[n], key=lambda s: s.start_ms))
        for n in sorted(found, key=lambda n: min(s.start_ms for s in found[n]))
    ]


def _harness_in(spans: list[VoiceSpan], start: float, end: float) -> VoiceSpan | None:
    """A harness span that held the line during ``[start, end]``. One that ran beside the
    agent's path (``holds_line`` False: the session kept stamping the agent's audio on
    arrival meanwhile, the duplex driver) does not make a gap unplaceable: the agent could
    have spoken, and when it did is known."""
    return next(
        (
            s
            for s in spans
            if s.kind == "harness" and s.holds_line and _overlap(s.start_ms, s.end_ms, start, end)
        ),
        None,
    )


@dataclass
class _Gap:
    """The answer gap before one agent turn: where it starts, or why there is none.

    ``silence_from`` is where the agent's silence began when that is not the caller's end:
    the agent was still talking (its previous turn) when the caller finished, so the gap is
    not a latency, but the silence after it stopped is still the agent's to fill."""

    start: float | None
    reason: str | None = None
    agent_first: bool = False
    silence_from: float | None = None


def _gap_before(agent: _Turn, callers: list[_Turn], agents: list[_Turn]) -> _Gap:
    before = [c for c in callers if c.start < agent.start]
    if not before:
        return _Gap(None, "the agent spoke before the caller said anything", agent_first=True)
    caller = before[-1]
    if caller.end > agent.start:
        return _Gap(
            None,
            f"the agent began speaking before the caller finished turn {caller.turn} "
            "(no gap to measure)",
        )
    talking = [a for a in agents if a.start < caller.end < a.end and a.end <= agent.start]
    if talking:
        return _Gap(
            None,
            f"the agent was still speaking (its turn {talking[-1].turn}) when the caller "
            f"finished turn {caller.turn}: no latency to measure",
            silence_from=talking[-1].end,
        )
    between = [a for a in agents if caller.end <= a.start < agent.start]
    if between:
        return _Gap(
            None,
            f"follows the agent's own turn {between[-1].turn}, not a caller turn: nobody "
            "can say which party was expected to speak",
        )
    return _Gap(caller.end)


def _latency_and_dead_air(log: CallLog, spans: list[VoiceSpan]) -> list[VoiceMetric]:
    callers, agents = _turns(spans, "caller"), _turns(spans, "agent")
    bursts = {a.turn for a in log.agents if a.burst}
    rows: list[VoiceMetric] = []
    for agent in agents:
        gap = _gap_before(agent, callers, agents)
        latency: float | None = None
        latency_reason = gap.reason
        latency_degraded = False
        gap_air = 0.0
        notes: list[str] = []
        degraded = False
        if gap.start is not None:
            stall = _harness_in(spans, gap.start, agent.start)
            if stall is not None:
                latency_reason = (
                    f"{UNPLACEABLE}: Opseval's {stall.detail or 'harness'} was working between "
                    f"the caller's end ({gap.start:.0f} ms) and the agent's answer "
                    f"({agent.start:.0f} ms), so the gap is left out"
                )
                latency_degraded = degraded = True
                notes.append(latency_reason)
            else:
                latency = agent.start - gap.start
                gap_air = latency
        elif gap.silence_from is not None:
            stall = _harness_in(spans, gap.silence_from, agent.start)
            if stall is not None:
                degraded = True
                notes.append(
                    f"{UNPLACEABLE}: Opseval's {stall.detail or 'harness'} was working in the "
                    f"silence before agent turn {agent.turn}, which is left out"
                )
            else:
                gap_air = max(0.0, agent.start - gap.silence_from)
        rows.append(
            _row(
                "voice.turn_latency_ms",
                latency,
                "ms",
                turn=agent.turn,
                reason=latency_reason,
                degraded=latency_degraded,
            )
        )
        if agent.turn in bursts:
            degraded = True
            notes.append(
                f"{UNPLACEABLE}: agent turn {agent.turn}'s audio arrived faster than it plays "
                "(the turn-based bridge places it by arrival), so pauses inside it are not on "
                "the clock"
            )
        pauses = 0.0
        for left, right in zip(agent.spans, agent.spans[1:], strict=False):
            start, end = left.end_ms, right.start_ms
            if end - start < PAUSE_FLOOR_MS:
                continue
            if any(_overlap(c.start, c.end, start, end) for c in callers):
                continue  # the caller was talking: not silence, and not the agent's to fill
            stall = _harness_in(spans, start, end)
            if stall is not None:
                degraded = True
                notes.append(
                    f"{UNPLACEABLE}: a pause in agent turn {agent.turn} overlapped Opseval's "
                    f"{stall.detail or 'harness'} work and is left out"
                )
                continue
            pauses += end - start
        rows.append(
            _row(
                "voice.dead_air_ms",
                gap_air + pauses,
                "ms",
                turn=agent.turn,
                reason="; ".join(notes) or None,
                degraded=degraded,
            )
        )
    return rows


def _first_audio(log: CallLog, spans: list[VoiceSpan]) -> VoiceMetric:
    callers, agents = _turns(spans, "caller"), _turns(spans, "agent")
    name: VoiceMetricName = "voice.time_to_first_audio_ms"
    if not agents:
        return _row(name, None, "ms", reason=f"{NOT_MEASURABLE}: the agent never spoke")
    first = agents[0]
    if not callers or first.start < callers[0].start:
        # The agent greets: its clock runs from the moment the call was open, and nothing
        # Opseval does on its side can hold the agent's greeting back.
        return _row(name, max(0.0, first.start - log.call_start_ms), "ms")
    opening = callers[0]
    if first.start < opening.end:
        return _row(
            name,
            None,
            "ms",
            reason="the agent began speaking before the caller's opening turn ended",
        )
    stall = _harness_in(spans, opening.end, first.start)
    if stall is not None:
        return _row(
            name,
            None,
            "ms",
            reason=f"{UNPLACEABLE}: Opseval's {stall.detail or 'harness'} was working before "
            "the agent's first audio",
            degraded=True,
        )
    return _row(name, first.start - opening.end, "ms")


def _talk_over(log: CallLog, spans: list[VoiceSpan]) -> VoiceMetric:
    name: VoiceMetricName = "voice.talk_over_ratio"
    if log.mode != "duplex":
        return _row(
            name,
            None,
            "ratio",
            reason=f"{NOT_MEASURABLE}: the turn-based bridge never plays both parties at once; "
            "run the scenario with voice.duplex: true",
        )
    agent = [s for s in spans if s.kind == "speech" and s.speaker == "agent"]
    callers = _turns(spans, "caller")
    total = sum(s.end_ms - s.start_ms for s in agent)
    if total <= 0:
        return _row(name, None, "ratio", reason=f"{NOT_MEASURABLE}: the agent spoke no audio")
    over = 0.0
    for span in agent:
        for caller in callers:
            # Only speech the agent began while the caller was already talking is its own
            # talk-over; overlap the caller started is the caller's (a barge-in).
            if caller.start <= span.start_ms < caller.end:
                over += _overlap(span.start_ms, span.end_ms, caller.start, caller.end)
    return _row(name, over / total, "ratio")


def _yields(log: CallLog, spans: list[VoiceSpan]) -> list[VoiceMetric]:
    name: VoiceMetricName = "voice.barge_in_yield_ms"
    if log.mode != "duplex":
        return [
            _row(
                name,
                None,
                "ms",
                reason=f"{NOT_MEASURABLE}: the turn-based bridge cannot interrupt the agent; "
                "plant voice.barge_in (duplex)",
            )
        ]
    if not log.barge_ins:
        return [_row(name, None, "ms", reason=f"{NOT_MEASURABLE}: no barge-in was planted")]
    agents = {t.turn: t for t in _turns(spans, "agent")}
    rows = []
    for plan in log.barge_ins:
        turn = agents.get(plan.agent_turn)
        if plan.fired_at_ms is None:
            reason = plan.reason or f"the agent never reached turn {plan.agent_turn}"
            rows.append(_row(name, None, "ms", turn=plan.agent_turn, reason=reason))
            continue
        if turn is None or turn.end <= plan.fired_at_ms:
            rows.append(
                _row(
                    name,
                    None,
                    "ms",
                    turn=plan.agent_turn,
                    reason=f"the agent had stopped speaking before the caller cut in at "
                    f"{plan.fired_at_ms:.0f} ms: nothing to yield",
                )
            )
            continue
        rows.append(_row(name, turn.end - plan.fired_at_ms, "ms", turn=plan.agent_turn))
    return rows


def _attributed(log: CallLog) -> dict[int, list[str]]:
    """The agent's final transcripts per caller turn, attributed by time: a transcript belongs
    to the last caller turn that had started when it arrived (it cannot be of a turn not yet
    spoken), and every final of one turn is kept, in order: an agent that sends one final per
    phrase (a caller pausing mid-sentence) heard the whole turn, not its last phrase."""
    starts = sorted(log.callers, key=lambda c: c.start_ms)
    found: dict[int, list[str]] = {}
    for at, text in sorted(log.heard, key=lambda h: h[0]):
        began = [c for c in starts if c.start_ms <= at]
        if began:
            found.setdefault(began[-1].turn, []).append(text)
    return found


def _asr_wer(log: CallLog) -> list[VoiceMetric]:
    name: VoiceMetricName = "voice.agent_asr_wer"
    if not log.agent_transcripts_seen:
        return [
            _row(
                name,
                None,
                "wer",
                reason=f"{NOT_MEASURABLE}: the agent exposes no transcript of the caller "
                "(a transcript event with of: caller)",
            )
        ]
    heard = _attributed(log)
    rows = []
    for caller in log.callers:
        said = heard.get(caller.turn)
        if not said:
            reason = "the agent sent no transcript of this caller turn"
            rows.append(_row(name, None, "wer", turn=caller.turn, reason=reason))
            continue
        wer = word_error_rate(caller.text, " ".join(said))
        rows.append(_row(name, wer, "wer", turn=caller.turn))
    return rows


def _confidence(log: CallLog) -> list[VoiceMetric]:
    name: VoiceMetricName = "voice.asr_confidence"
    rows = []
    for agent in log.agents:
        if agent.heard is None:
            reason = "this agent turn was not transcribed"
            rows.append(_row(name, None, "confidence", turn=agent.turn, reason=reason))
        elif agent.heard in ("asr_failed", "unintelligible"):
            reason = f"no transcript was heard in this turn ({agent.heard})"
            rows.append(_row(name, None, "confidence", turn=agent.turn, reason=reason))
        elif agent.confidence is None:
            reason = "the STT reported no confidence"
            rows.append(_row(name, None, "confidence", turn=agent.turn, reason=reason))
        else:
            rows.append(_row(name, agent.confidence, "confidence", turn=agent.turn))
    if not rows:
        rows.append(_row(name, None, "confidence", reason="the agent spoke no turn"))
    return rows


def compute(log: CallLog) -> tuple[VoiceMetric, ...]:
    """Every metric row for one attempt."""
    spans = sorted(log.spans, key=lambda s: (s.start_ms, s.end_ms))
    return (
        _first_audio(log, spans),
        *_latency_and_dead_air(log, spans),
        _talk_over(log, spans),
        *_yields(log, spans),
        *_asr_wer(log),
        *_confidence(log),
    )


__all__ = [
    "METRICS_VERSION",
    "NOT_MEASURABLE",
    "PAUSE_FLOOR_MS",
    "UNPLACEABLE",
    "AgentTurn",
    "CallLog",
    "CallerTurn",
    "PlannedBargeIn",
    "compute",
]
