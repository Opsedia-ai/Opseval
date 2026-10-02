# SPDX-License-Identifier: Apache-2.0
"""Reports: a self-contained HTML page (no network requests) and a console summary."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from opseval.models.record import RunRecord
from opseval.models.results import SuiteResult

_TEMPLATES = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES)),
    autoescape=select_autoescape(["html", "j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def fmt_score(value: float | None) -> str:
    """Never render unmeasured as zero."""
    return "not measured" if value is None else f"{value:.2f}"


_env.filters["score"] = fmt_score


def render_html(result: SuiteResult, records: list[RunRecord]) -> str:
    by_key = {(r.test_id, r.attempt_index): r for r in records}
    return _env.get_template("report.html.j2").render(result=result, records=by_key)


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
    for err in result.load_errors:
        lines.append(f"  LOAD ERROR    {err}")
    lines += ["", f"Gate: {result.gate.status.upper()} — {result.gate.summary}"]
    lines.append(f"Exit code: {result.exit_code}")
    return "\n".join(lines)


__all__ = ["console_summary", "fmt_score", "render_html"]
