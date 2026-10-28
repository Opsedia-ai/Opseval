# SPDX-License-Identifier: Apache-2.0
"""Voice: was the agent heard? (ADR-0053)

The voice bridge records, on every spoken reply, how it was heard (``VOICE_KEY`` in the
payload): by the STT, by the second STT only, or by neither, and then whether the STT canary
on the caller's own preceding turn was healthy. This module reads that evidence:

- :class:`VoiceIntelligible` (``voice.intelligible``, output axis, always on for voice
  attempts) fails when the agent spoke with energy and no STT could read it while the canary
  proved Opseval could hear: the agent's audio is what failed, with the proof attached. When
  the canary failed instead, it is ``infra_error`` ``ASR_NO_TRANSCRIPT``.
- :func:`voice_findings` tells the engine what the evidence means for the other scorers: a
  text-reading score is not measured when the canary failed (``ASR_NO_TRANSCRIPT``, naming
  the canary), degraded when a turn was heard only by the second STT, and a judge score is
  degraded when a transcript's confidence was under ``OPSEVAL_STT_MIN_CONFIDENCE``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from opseval.models.profile import FrameKind
from opseval.models.record import VOICE_KEY, FrameRecord, RunRecord
from opseval.models.results import ASR_NO_TRANSCRIPT, VOICE_INTELLIGIBLE, Axis, ScoreResult
from opseval.models.scenario import ScorerSpec
from opseval.scoring.base import NoParams, Scorer, ScoringContext, register


def _evidence(frame: FrameRecord) -> dict[str, Any] | None:
    payload = frame.payload
    if frame.direction != "received" or not isinstance(payload, dict):
        return None
    found = payload.get(VOICE_KEY)
    return found if isinstance(found, dict) else None


def _canary(evidence: dict[str, Any]) -> str:
    canary = evidence.get("canary")
    if not isinstance(canary, dict):
        return "no canary was run"
    who = f"{canary.get('provider')} ({canary.get('model') or 'default model'})"
    if canary.get("error"):
        return f"the STT canary on {who} failed: {canary['error']}"
    return (
        f"the STT canary on {who} misheard the caller's own turn: WER {canary.get('wer')} over "
        f"OPSEVAL_STT_CANARY_MAX_WER {canary.get('max_wer')}"
    )


@dataclass
class VoiceFindings:
    """What a voice record's evidence means for the attempt's other scores."""

    asr_failed: list[int] = field(default_factory=list)
    """Seqs of agent turns Opseval could not hear, with its own listening proven broken."""
    heard: list[int] = field(default_factory=list)
    """Seqs of agent turns that were heard (by the STT or the second STT)."""
    asr_reason: str = ""
    """Set only when no agent turn was heard: every text-reading score is then
    ``infra_error`` with it."""
    unheard: str | None = None
    """Set when some turns were heard and some not: the text-reading scores grade the heard
    turns (:func:`heard_only`) and are degraded with this reason."""
    text_degraded: str | None = None
    judge_degraded: str | None = None


def voice_findings(record: RunRecord) -> VoiceFindings | None:
    """``None`` for a text attempt."""
    if record.voice is None:
        return None
    found = VoiceFindings()
    canary = ""
    second: list[int] = []
    off_script: list[int] = []
    low: list[str] = []
    for frame in record.frames:
        evidence = _evidence(frame)
        if evidence is None:
            continue
        heard = evidence.get("heard")
        if heard == "asr_failed":
            found.asr_failed.append(frame.seq)
            canary = canary or _canary(evidence)
        elif heard == "second_stt":
            second.append(frame.seq)
            found.heard.append(frame.seq)
        elif heard == "stt":
            found.heard.append(frame.seq)
        if isinstance(evidence.get("off_script"), dict):
            off_script.append(frame.seq)
        flagged = evidence.get("low_confidence")
        if isinstance(flagged, dict):
            low.append(f"frame {frame.seq}: {flagged.get('confidence')} < {flagged.get('min')}")
    if found.asr_failed and not found.heard:
        found.asr_reason = (
            f"{ASR_NO_TRANSCRIPT}: Opseval could not transcribe {len(found.asr_failed)} agent "
            f"turn(s) (frames {found.asr_failed}) and heard no other, and {canary}: its own "
            "listening is broken, so what the agent said is not measured"
        )
    elif found.asr_failed:
        # One unheard turn must not void the turns that were heard (a credential leaked in
        # one of them still fails): those are graded, and the score says what is missing.
        found.unheard = (
            f"voice: Opseval could not hear {len(found.asr_failed)} agent turn(s) (frames "
            f"{found.asr_failed}; {canary}), so the text checks graded only the "
            f"{len(found.heard)} turn(s) it heard (frames {found.heard})"
        )
    text_reasons: list[str] = []
    if second:
        tried = [
            f"{t.get('provider')}:{t.get('model')}"
            for f in record.frames
            if f.seq in second and (ev := _evidence(f)) is not None
            for t in ev.get("transcripts", [])
            if isinstance(t, dict)
        ]
        text_reasons.append(
            f"voice: the STT could not read {len(second)} agent turn(s) (frames {second}) and "
            f"only the second STT did ({', '.join(dict.fromkeys(tried))})"
        )
    if off_script:
        text_reasons.append(
            f"voice: {len(off_script)} agent turn(s) (frames {off_script}) were heard in "
            "another script than the scenario language's, so the transcript may be the STT's "
            "rendering rather than the agent's words"
        )
    if text_reasons:
        found.text_degraded = "; ".join(text_reasons)
    if low:
        found.judge_degraded = (
            "voice: transcript confidence under OPSEVAL_STT_MIN_CONFIDENCE (" + "; ".join(low) + ")"
        )
    return found


def unheard_turn(frame: FrameRecord) -> bool:
    """A spoken agent turn Opseval could not hear, with its own listening proven broken."""
    evidence = _evidence(frame)
    return evidence is not None and evidence.get("heard") == "asr_failed"


def heard_only(record: RunRecord, findings: VoiceFindings) -> RunRecord:
    """The record the text-reading scorers grade when some turns were heard and some not:
    an unheard turn's transcript (empty, or the STT's garbage) is removed, so no check reads
    Opseval's failure to listen as something the agent said. The frame stays, with its
    evidence, so ``conversation.agent_replied`` still sees that the agent spoke there and a
    judge is told which turns are missing (``Judge.harness_facts``)."""
    unheard = set(findings.asr_failed)
    if not unheard:
        return record
    frames = tuple(
        f.model_copy(update={"text": None}) if f.seq in unheard else f for f in record.frames
    )
    return record.model_copy(update={"frames": frames})


@register
class VoiceIntelligible(Scorer):
    """ADR-0053: every agent turn with energy was heard, or the agent's audio is proven bad.

    A turn whose transcript was empty or had no word in the language's script, while the STT
    canary (the caller's own preceding turn through the same STT, model and codec path) was
    healthy, is the agent's audio failing. A turn heard only by the second STT counts as
    heard (degraded). When the canary failed, this is not measured (``ASR_NO_TRANSCRIPT``).
    Undecodable audio is an error frame (``conversation.no_errors``) and a turn nobody could
    understand, so it fails here too, with its reason and size: never a vacuous pass.
    """

    id = VOICE_INTELLIGIBLE
    version = "2"  # 2: undecodable audio fails it; unheard turns degrade, never void it
    Params = NoParams
    axis = Axis.OUTPUT
    reads_text = False
    description = (
        "Always on for voice. Fails when the agent spoke with energy that no STT could read "
        "while the STT canary proved Opseval could hear, or sent audio that cannot be decoded; "
        "an unheard turn (the canary failed) is left out and degrades it, and with no turn "
        "heard it is not measured."
    )

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        turns: list[tuple[int, dict[str, Any]]] = [
            (f.seq, ev)
            for f in record.frames
            if (ev := _evidence(f)) is not None
            and (f.kind is FrameKind.MESSAGE or "undecodable" in ev)
        ]
        if not turns:
            return self.result(spec, 1.0, vacuous=True, note="the agent spoke no turn")
        bad = [(seq, ev) for seq, ev in turns if ev.get("heard") == "unintelligible"]
        # Audio the agent sent that could not even be decoded is a turn nobody could
        # understand: the agent's fault, with the bytes' hash as proof, never a vacuous pass.
        broken = [(seq, ev) for seq, ev in turns if "undecodable" in ev]
        failed = [seq for seq, ev in turns if ev.get("heard") == "asr_failed"]
        measured = len(turns) - len(failed)
        if measured == 0:
            found = voice_findings(record)
            return self.infra(spec, found.asr_reason if found else ASR_NO_TRANSCRIPT)
        second = [seq for seq, ev in turns if ev.get("heard") == "second_stt"]
        unheard = [
            {
                "seq": seq,
                "level_dbfs": ev.get("level_dbfs"),
                "duration_ms": ev.get("duration_ms"),
                "transcripts": ev.get("transcripts"),
                "canary": ev.get("canary"),
            }
            for seq, ev in bad
        ]
        undecodable = [
            {"seq": seq, "reason": ev.get("undecodable"), "bytes": ev.get("bytes")}
            for seq, ev in broken
        ]
        return self.result(
            spec,
            (measured - len(bad) - len(broken)) / measured,
            degraded=bool(second or failed),
            turns=len(turns),
            unintelligible=unheard,
            undecodable=undecodable,
            heard_by_second_stt=second,
            not_measured=failed,
        )
