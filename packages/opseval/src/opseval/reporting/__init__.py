# SPDX-License-Identifier: Apache-2.0
"""Reports: a self-contained HTML page (no network requests) and a console summary."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from opseval.flow import flow_maps
from opseval.models.gate import describe, looser_than_standard
from opseval.models.profile import FrameKind, SutProfile
from opseval.models.record import RunRecord
from opseval.models.results import SuiteResult
from opseval.storage import FileRunAudio, RunAudio
from opseval.transcript import connection, transcript

_TEMPLATES = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES)),
    autoescape=select_autoescape(["html", "j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.globals.update(describe_policy=describe, looser_than_standard=looser_than_standard)


def fmt_score(value: float | None) -> str:
    """Never render unmeasured as zero."""
    return "not measured" if value is None else f"{value:.2f}"


_env.filters["score"] = fmt_score


@dataclass(frozen=True)
class AudioNote:
    """What the report says about a voice attempt's audio. The report embeds none: audio is
    played in the console, or exported with the command shown (ADR-0053, V0d)."""

    turns: int
    state: str
    command: str | None
    """Exports the whole call; ``None`` when there are no bytes to export."""
    raw: dict[int, str] = field(default_factory=dict)
    """By seq: the command that exports an undecodable agent turn's bytes as they came."""


def audio_note(record: RunRecord, run_dir: RunAudio | str | Path | None) -> AudioNote | None:
    refs = [f.audio for f in record.frames if f.audio is not None]
    if not refs:
        return None
    if run_dir is None or isinstance(run_dir, str | Path):
        path = Path(run_dir) if run_dir is not None else Path("reports") / record.run_id
        audio: RunAudio = FileRunAudio(path, record.run_id)
    else:
        audio = run_dir
    marker = audio.pruned()  # the player's own parser: they never disagree
    files = [r for r in refs if r.path is not None]
    if marker is not None or any(r.pruned for r in refs):
        said = marker.note if marker is not None else "audio pruned by retention"
        return AudioNote(len(refs), f"{said}; the transcript and hashes remain", None)
    if not files:
        return AudioNote(len(refs), "only hashes were kept (OPSEVAL_RECORD_AUDIO=hashes)", None)
    sealed = sum(r.encryption != "none" for r in files)
    state = "encrypted" if sealed == len(files) else "plain WAV (synthetic demo voices)"
    if 0 < sealed < len(files):
        state = f"{sealed} of {len(files)} encrypted"
    test, n = record.test_id, record.attempt_index
    base = f"opseval audio export {audio.export_args()} --test {shlex.quote(test)} --attempt {n}"
    raw = {
        f.seq: f"{base} --turn {f.seq} --raw --out "
        + shlex.quote(f"{test}-attempt{n}-turn{f.seq}.raw")
        for f in record.frames
        if f.audio is not None and f.audio.path is not None and f.kind is FrameKind.ERROR
    }
    return AudioNote(len(refs), state, f"{base} --out {shlex.quote(f'{test}-attempt{n}.wav')}", raw)


def render_html(
    result: SuiteResult,
    records: list[RunRecord],
    profile: SutProfile | None = None,
    run_dir: RunAudio | str | Path | None = None,
) -> str:
    """Render the self-contained report.

    ``profile`` supplies the declared phase order for the flow map (FR-RPT-05). Without
    it the flow map reports itself unmeasured rather than inventing an expected order.
    ``run_dir`` is the run folder, named in the command that exports a voice attempt's audio.
    """
    by_key = {(r.test_id, r.attempt_index): r for r in records}
    flows = flow_maps(records, profile) if profile is not None else {}
    # The same conversation mapping as the console (opseval.transcript), so they never differ.
    talks = {key: transcript(r) for key, r in by_key.items()}
    links = {key: connection(r) for key, r in by_key.items()}
    audio = {key: audio_note(r, run_dir) for key, r in by_key.items()}
    return _env.get_template("report.html.j2").render(
        result=result, records=by_key, flows=flows, talks=talks, links=links, audio=audio
    )


STATUS_ICON = {
    "passed": "PASS",
    "failed": "FAIL",
    "inconclusive": "INCONCLUSIVE",
    "skipped_precondition": "SKIPPED",
    "blocked": "BLOCKED",
}


def console_summary(result: SuiteResult) -> str:
    lines = [f"Opseval run {result.run_id} · suite {result.suite} · target {result.target}", ""]
    for t in result.tests:
        rate = "—" if t.pass_rate is None else f"{t.pass_rate:.0%}"
        flaky = " flaky" if t.flaky else ""
        lines.append(
            f"  {STATUS_ICON[t.status.value]:<13} {t.test_id:<10} [{t.criticality.value}] "
            f"pass rate {rate}{flaky}  {t.name}"
        )
        if t.reason:
            lines.append(f"  {'':<13} ↳ {t.reason}")
        if t.degraded_scorers:
            lines.append(f"  {'':<13} ↳ degraded: {', '.join(t.degraded_scorers)}")
        if t.vacuous_scorers:
            lines.append(f"  {'':<13} ↳ vacuous: {', '.join(t.vacuous_scorers)}")
        if t.unmeasured_scorers:
            lines.append(f"  {'':<13} ↳ not measured: {', '.join(t.unmeasured_scorers)}")
    for err in result.load_errors:
        lines.append(f"  LOAD ERROR    {err}")
    for outcome in result.sinks:
        mark = "published" if outcome.published else "NOT PUBLISHED"
        lines.append(f"  {mark:<13} {outcome.sink}: {outcome.detail}")
    if result.stopped is not None:
        lines += ["", f"Stopped early: {result.stopped.detail}"]
    lines += ["", f"Gate: {result.gate.status.upper()} — {result.gate.summary}"]
    if result.gate.policy is not None:
        lines.append(f"Gate policy: {describe(result.gate.policy)}")
        looser = looser_than_standard(result.gate.policy)
        if looser:
            lines.append(f"  ⚠ looser than standard: {', '.join(looser)}")
    lines.append(f"Exit code: {result.exit_code}")
    return "\n".join(lines)


__all__ = ["console_summary", "fmt_score", "render_html"]
