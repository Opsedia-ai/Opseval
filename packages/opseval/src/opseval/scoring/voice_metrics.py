# SPDX-License-Identifier: Apache-2.0
"""Voice metrics as gate thresholds (ADR-0055).

Each scorer reads the raw rows the voice bridge wrote into ``RunRecord.voice.metrics`` (one
per turn, or one per attempt) and compares them with the scenario's thresholds, given as
params. Percentiles (p50, p95) are computed here from the per-turn rows, never stored folded.

The one rule, for metrics:

- A row that was not measured is ``None`` with a reason. A threshold over nothing measured is
  ``infra_error`` ``NOT_MEASURABLE``, named as not measured (ADR-0030), never a pass on no
  evidence and never a 0.
- A row measured on partial evidence (a gap the clocks could not place was left out) marks the
  score ``degraded``, with the reason.
- A record from before voice metrics (layouts 3 to 6) has none: ``NOT_MEASURABLE``.

No scorer here reads what the agent said (``reads_text`` is ``False``): a voice attempt whose
STT failed is still timed.
"""

from __future__ import annotations

import math
from typing import Any, ClassVar

from pydantic import Field, ValidationError, model_validator

from opseval.models.record import RunRecord, VoiceMetric, VoiceMetricName
from opseval.models.results import Axis, ScoreResult
from opseval.models.scenario import ScorerSpec
from opseval.scoring.base import NoParams, Scorer, ScoringContext, register

NOT_MEASURABLE = "NOT_MEASURABLE"


def percentile(values: list[float], pct: float) -> float:
    """The nearest-rank percentile: the smallest value with at least ``pct`` % of the values
    at or below it. With five turns, p95 is the slowest; it never invents a value between
    two turns."""
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def _shown(row: VoiceMetric) -> dict[str, Any]:
    out: dict[str, Any] = {"turn": row.turn, "value": row.value}
    if row.reason:
        out["reason"] = row.reason
    if row.degraded:
        out["degraded"] = True
    return out


class _MetricScorer(Scorer):
    """Shared reading of the rows: not measured, degraded and the threshold check."""

    metric: ClassVar[VoiceMetricName]
    reads_text = False

    def _rows(self, record: RunRecord, spec: ScorerSpec) -> list[VoiceMetric] | ScoreResult:
        if record.voice is None:
            return self.infra(spec, f"{NOT_MEASURABLE}: not a voice attempt")
        if record.voice.metrics is None:
            return self.infra(
                spec,
                f"{NOT_MEASURABLE}: this record was written before voice metrics (ADR-0055)",
            )
        return [m for m in record.voice.metrics if m.name == self.metric]

    def _params(self, spec: ScorerSpec) -> Any:
        assert self.Params is not None  # nosec B101 - every scorer here has one
        return self.Params.model_validate(spec.params)

    def _judge(
        self,
        spec: ScorerSpec,
        rows: list[VoiceMetric],
        failures: list[str],
        **details: Any,
    ) -> ScoreResult:
        measured = [r for r in rows if r.value is not None]
        unmeasured = [r for r in rows if r.value is None]
        degraded = [r for r in rows if r.degraded]
        notes = [f"turn {r.turn}: {r.reason}" for r in degraded if r.reason]
        return self.result(
            spec,
            0.0 if failures else 1.0,
            degraded=bool(degraded),
            failures=failures,
            rows=[_shown(r) for r in rows],
            measured=len(measured),
            not_measured=[_shown(r) for r in unmeasured],
            degraded_by=notes or None,
            **details,
        )

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        try:
            params = self._params(spec)
        except ValidationError as exc:
            return self.infra(spec, f"BAD_PARAMS: {exc.errors()[0].get('msg')}")
        found = self._rows(record, spec)
        if isinstance(found, ScoreResult):
            return found
        measured = [r for r in found if r.value is not None]
        if not measured:
            why = "; ".join(dict.fromkeys(r.reason or "" for r in found if r.reason))
            return self.infra(
                spec,
                f"{NOT_MEASURABLE}: {why or 'no row was recorded'}",
                rows=[_shown(r) for r in found],
            )
        return self.check(params, found, [r.value for r in measured if r.value is not None], spec)

    def check(
        self, params: Any, rows: list[VoiceMetric], values: list[float], spec: ScorerSpec
    ) -> ScoreResult:
        raise NotImplementedError


def _over(label: str, value: float, bound: float | None, unit: str = " ms") -> list[str]:
    if bound is None or value <= bound:
        return []
    return [f"{label} {value:g}{unit} > {bound:g}{unit}"]


def _under(label: str, value: float, bound: float | None) -> list[str]:
    if bound is None or value >= bound:
        return []
    return [f"{label} {value:g} < {bound:g}"]


class _AtLeastOne(NoParams):
    @model_validator(mode="after")
    def _one(self) -> _AtLeastOne:
        if all(getattr(self, name) is None for name in type(self).model_fields):
            names = ", ".join(type(self).model_fields)
            raise ValueError(f"set at least one bound ({names})")
        return self


class FirstAudioParams(NoParams):
    max_ms: float = Field(gt=0)
    """The longest the caller may wait for the agent's first audio: from the call's start
    when the agent greets, else from the end of the caller's opening turn."""


@register
class TimeToFirstAudio(_MetricScorer):
    id = "voice.time_to_first_audio_ms"
    metric = "voice.time_to_first_audio_ms"
    version = "1"
    Params = FirstAudioParams
    axis = Axis.OUTPUT
    description = (
        "Voice (ADR-0055): how long until the agent's first audio, against max_ms. Never "
        "counts Opseval's own TTS or STT time."
    )

    def check(
        self, params: Any, rows: list[VoiceMetric], values: list[float], spec: ScorerSpec
    ) -> ScoreResult:
        return self._judge(spec, rows, _over("first audio", values[0], params.max_ms))


class LatencyParams(_AtLeastOne):
    p50_max_ms: float | None = Field(default=None, gt=0)
    """The median turn latency may not exceed this."""
    p95_max_ms: float | None = Field(default=None, gt=0)
    """The 95th-percentile turn latency (nearest rank) may not exceed this."""
    max_ms: float | None = Field(default=None, gt=0)
    """No single turn may take longer."""


@register
class TurnLatency(_MetricScorer):
    id = "voice.turn_latency_ms"
    metric = "voice.turn_latency_ms"
    version = "1"
    Params = LatencyParams
    axis = Axis.OUTPUT
    description = (
        "Voice (ADR-0055): from the caller's last sample to the agent's first audio, per "
        "turn; p50/p95 are computed from the turns. A turn the agent began over the caller, "
        "or whose gap the harness overlapped, is not measured (the latter degrades it)."
    )

    def check(
        self, params: Any, rows: list[VoiceMetric], values: list[float], spec: ScorerSpec
    ) -> ScoreResult:
        p50, p95 = percentile(values, 50), percentile(values, 95)
        failures = [
            *_over("p50", p50, params.p50_max_ms),
            *_over("p95", p95, params.p95_max_ms),
            *_over("slowest turn", max(values), params.max_ms),
        ]
        return self._judge(spec, rows, failures, p50_ms=p50, p95_ms=p95, max_ms=max(values))


class YieldParams(NoParams):
    max_ms: float = Field(gt=0)
    """How long the agent may keep talking after the caller cuts in."""


@register
class BargeInYield(_MetricScorer):
    id = "voice.barge_in_yield_ms"
    metric = "voice.barge_in_yield_ms"
    version = "1"
    Params = YieldParams
    axis = Axis.FLOW
    description = (
        "Voice (ADR-0055, duplex): after a planted barge-in, how long the agent kept talking. "
        "Not measured when no barge-in was planted or the agent had already stopped."
    )

    def check(
        self, params: Any, rows: list[VoiceMetric], values: list[float], spec: ScorerSpec
    ) -> ScoreResult:
        failures = [
            f"agent turn {r.turn}: kept talking {r.value:g} ms after the caller cut in "
            f"(> {params.max_ms:g} ms)"
            for r in rows
            if r.value is not None and r.value > params.max_ms
        ]
        result = self._judge(spec, rows, failures)
        unfired = [r for r in rows if r.value is None]
        if unfired and not result.degraded:
            # A planted interruption that never happened is evidence missing, named, never a
            # silent pass on the ones that did.
            result = result.model_copy(update={"degraded": True})
        return result


class TalkOverParams(NoParams):
    max_ratio: float = Field(ge=0, le=1)
    """The share of the agent's speech it may start while the caller is talking."""


@register
class TalkOver(_MetricScorer):
    id = "voice.talk_over_ratio"
    metric = "voice.talk_over_ratio"
    version = "1"
    Params = TalkOverParams
    axis = Axis.FLOW
    description = (
        "Voice (ADR-0055, duplex): share of the agent's speech it began while the caller was "
        "talking. Overlap the caller started (a barge-in) is never counted."
    )

    def check(
        self, params: Any, rows: list[VoiceMetric], values: list[float], spec: ScorerSpec
    ) -> ScoreResult:
        return self._judge(spec, rows, _over("talk-over", values[0], params.max_ratio, ""))


class DeadAirParams(_AtLeastOne):
    max_turn_ms: float | None = Field(default=None, gt=0)
    """The most silence allowed in one agent turn (its answer gap and its pauses)."""
    max_total_ms: float | None = Field(default=None, gt=0)
    """The most silence allowed over the whole call."""


@register
class DeadAir(_MetricScorer):
    id = "voice.dead_air_ms"
    metric = "voice.dead_air_ms"
    version = "1"
    Params = DeadAirParams
    axis = Axis.FLOW
    description = (
        "Voice (ADR-0055): silence while the agent is the party expected to speak, per turn. "
        "Planted silences and Opseval's simulator, TTS and STT time never count; a gap the "
        "clocks cannot place is left out and degrades it."
    )

    def check(
        self, params: Any, rows: list[VoiceMetric], values: list[float], spec: ScorerSpec
    ) -> ScoreResult:
        failures = [
            f"agent turn {r.turn}: {r.value:g} ms of dead air (> {params.max_turn_ms:g} ms)"
            for r in rows
            if params.max_turn_ms is not None
            and r.value is not None
            and r.value > params.max_turn_ms
        ]
        failures += _over("total dead air", sum(values), params.max_total_ms)
        return self._judge(spec, rows, failures, total_ms=sum(values))


class AgentWerParams(_AtLeastOne):
    max_mean: float | None = Field(default=None, ge=0)
    """The highest mean word error rate of the agent's transcripts of the caller."""
    max_turn: float | None = Field(default=None, ge=0)
    """The highest word error rate on any one caller turn."""


@register
class AgentAsrWer(_MetricScorer):
    id = "voice.agent_asr_wer"
    metric = "voice.agent_asr_wer"
    version = "1"
    Params = AgentWerParams
    axis = Axis.OUTPUT
    description = (
        "Voice (ADR-0055): how well the agent heard the caller, as the WER of its own "
        "transcript (a transcript event of the caller) against what the caller said, before "
        "any planted noise. Not measured when the agent exposes no such transcript."
    )

    def check(
        self, params: Any, rows: list[VoiceMetric], values: list[float], spec: ScorerSpec
    ) -> ScoreResult:
        mean = sum(values) / len(values)
        failures = [
            *_over("mean WER", round(mean, 3), params.max_mean, ""),
            *_over("worst turn WER", max(values), params.max_turn, ""),
        ]
        return self._judge(spec, rows, failures, mean_wer=round(mean, 3))

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        result = await super().score(record, spec, ctx)
        noise = record.voice.noise if record.voice else None
        if noise is None:
            return result
        # A WER under planted noise is the agent's result at that SNR: said with it.
        details = {**result.details, "noise": noise.model_dump(mode="json")}
        return result.model_copy(update={"details": details})


class ConfidenceParams(_AtLeastOne):
    min_mean: float | None = Field(default=None, ge=0, le=1)
    """The lowest mean STT confidence over the agent's heard turns."""
    min_turn: float | None = Field(default=None, ge=0, le=1)
    """The lowest STT confidence on any one heard agent turn."""


@register
class AsrConfidence(_MetricScorer):
    id = "voice.asr_confidence"
    metric = "voice.asr_confidence"
    version = "1"
    Params = ConfidenceParams
    axis = Axis.OUTPUT
    description = (
        "Voice (ADR-0055): Opseval's STT confidence on the agent's turns (mean and lowest). "
        "A turn with no confidence, or not heard, is not measured, never 0."
    )

    def check(
        self, params: Any, rows: list[VoiceMetric], values: list[float], spec: ScorerSpec
    ) -> ScoreResult:
        mean = sum(values) / len(values)
        failures = [
            *_under("mean confidence", round(mean, 3), params.min_mean),
            *_under("lowest turn confidence", min(values), params.min_turn),
        ]
        return self._judge(spec, rows, failures, mean=round(mean, 3), lowest=min(values))


class TransferParams(NoParams):
    expected: bool = True
    """Whether the agent should hand the call over. ``false``: any transfer fails."""
    to: str | None = Field(default=None, min_length=1)
    """The target it must transfer to (as its protocol names it)."""
    within_ms: float | None = Field(default=None, gt=0)
    """It must transfer within this long of the call's start."""


@register
class VoiceTransfer(Scorer):
    id = "voice.transfer"
    version = "1"
    Params = TransferParams
    axis = Axis.FLOW
    reads_text = False
    description = (
        "Voice (ADR-0055): the agent transferred the call (or did not), to the expected "
        "target, in time; read from the call's timeline."
    )

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        try:
            params = TransferParams.model_validate(spec.params)
        except ValidationError as exc:
            return self.infra(spec, f"BAD_PARAMS: {exc.errors()[0].get('msg')}")
        if record.voice is None:
            return self.infra(spec, f"{NOT_MEASURABLE}: not a voice attempt")
        found = [e for e in record.voice.events if e.kind == "transfer"]
        seen = [{"at_ms": e.at_ms, "to": e.detail} for e in found]
        if not params.expected:
            failures = [f"the agent transferred the call to {e.detail!r}" for e in found]
            return self.result(spec, 0.0 if failures else 1.0, failures=failures, transfers=seen)
        if not found:
            return self.result(
                spec, 0.0, failures=["the agent never transferred the call"], transfers=seen
            )
        first = found[0]
        failures = []
        if params.to is not None and first.detail != params.to:
            failures.append(f"transferred to {first.detail!r}, expected {params.to!r}")
        failures += _over("transfer at", round(first.at_ms, 1), params.within_ms)
        return self.result(spec, 0.0 if failures else 1.0, failures=failures, transfers=seen)


__all__ = [
    "AgentAsrWer",
    "AsrConfidence",
    "BargeInYield",
    "DeadAir",
    "TalkOver",
    "TimeToFirstAudio",
    "TurnLatency",
    "VoiceTransfer",
    "percentile",
]
