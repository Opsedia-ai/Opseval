# SPDX-License-Identifier: Apache-2.0
"""Reports: a self-contained HTML page (no network requests) and a console summary."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from opseval.flow import flow_maps
from opseval.models.gate import describe, looser_than_standard
from opseval.models.profile import SutProfile
from opseval.models.record import RunRecord
from opseval.models.results import SuiteResult
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


def render_html(
    result: SuiteResult, records: list[RunRecord], profile: SutProfile | None = None
) -> str:
    """Render the self-contained report.

    ``profile`` supplies the declared phase order for the flow map (FR-RPT-05). Without
    it the flow map reports itself unmeasured rather than inventing an expected order.
    """
    by_key = {(r.test_id, r.attempt_index): r for r in records}
    flows = flow_maps(records, profile) if profile is not None else {}
    # The same conversation mapping as the console (opseval.transcript), so they never differ.
    talks = {key: transcript(r) for key, r in by_key.items()}
    links = {key: connection(r) for key, r in by_key.items()}
    return _env.get_template("report.html.j2").render(
        result=result, records=by_key, flows=flows, talks=talks, links=links
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
    lines += ["", f"Gate: {result.gate.status.upper()} — {result.gate.summary}"]
    if result.gate.policy is not None:
        lines.append(f"Gate policy: {describe(result.gate.policy)}")
        looser = looser_than_standard(result.gate.policy)
        if looser:
            lines.append(f"  ⚠ looser than standard: {', '.join(looser)}")
    lines.append(f"Exit code: {result.exit_code}")
    return "\n".join(lines)


__all__ = ["console_summary", "fmt_score", "render_html"]
