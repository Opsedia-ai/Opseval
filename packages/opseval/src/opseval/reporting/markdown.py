# SPDX-License-Identifier: Apache-2.0
"""A short Markdown gate summary for PR comments and CI job summaries.

Content rules match the HTML report: unmeasured values say "not measured", inconclusive
is never shown as a pass, degraded evidence is named rather than passing as a full
measurement, and scenario text is escaped so a scenario name cannot inject Markdown or
mentions into a pull-request comment.
"""

from __future__ import annotations

import html
import re

from opseval.models.results import SuiteResult, VerdictStatus

MARKER = "<!-- opseval-gate -->"
"""Hidden marker so CI can update its previous comment instead of adding a new one."""

ICON = {
    VerdictStatus.PASSED: "✅ passed",
    VerdictStatus.FAILED: "❌ failed",
    VerdictStatus.INCONCLUSIVE: "⚠️ not judged",
    VerdictStatus.SKIPPED_PRECONDITION: "⏭️ skipped",
    VerdictStatus.BLOCKED: "⏸️ blocked",
}
EXIT_MEANING = {0: "pass", 1: "fail", 2: "not judged", 3: "harness or configuration error"}
_ESCAPE = re.compile(r"([\\`*_{}\[\]()#+\-.!|@~])")


def _md(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return html.escape(_ESCAPE.sub(r"\\\1", text), quote=False)


def _code(text: str, limit: int = 80) -> str:
    """Text for inside a backtick code span: no escaping, but no backticks either."""
    text = " ".join(text.replace("`", "'").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _rate(value: float | None) -> str:
    return "not measured" if value is None else f"{value:.0%}"


def markdown_summary(result: SuiteResult, *, report_url: str | None = None) -> str:
    blocked = result.gate.status != "pass"
    head = "🔴 **Release blocked**" if blocked else "🟢 **Safe to release**"
    exit_note = EXIT_MEANING.get(result.exit_code, "unknown")
    lines = [
        MARKER,
        f"### Opseval · {_md(result.suite)}",
        "",
        f"{head} · exit code {result.exit_code} ({exit_note})",
        "",
        _md(result.gate.summary, 600),
        "",
        "| Scenario | Criticality | Verdict | Pass rate |",
        "|---|---|---|---|",
    ]
    for t in result.tests:
        flaky = " · flaky" if t.flaky else ""
        lines.append(
            f"| `{_code(t.test_id, 60)}` {_md(t.name, 80)} | {t.criticality.value} | "
            f"{ICON[t.status]}{flaky} | {_rate(t.pass_rate)} |"
        )
    details = [t for t in result.tests if t.reason and t.status is not VerdictStatus.PASSED]
    if details:
        lines += ["", "<details><summary>Why</summary>", ""]
        lines += [f"- `{_code(t.test_id, 60)}`: {_md(t.reason or '', 300)}" for t in details]
        lines += ["", "</details>"]
    degraded = result.degraded_scorers
    if degraded:
        listed = ", ".join(f"`{_code(s, 60)}`" for s in degraded)
        lines += [
            "",
            f"⚠️ **Degraded evidence** · {listed} scored on weaker evidence than they ask for "
            "(frame evidence instead of traces, or a judge from the graded model's own vendor "
            "family). These scores counted toward the verdict above.",
        ]
    vacuous = result.vacuous_scorers
    if vacuous:
        listed = ", ".join(f"`{_code(s, 60)}`" for s in vacuous)
        lines += [
            "",
            f"ℹ️ **Passed vacuously** · {listed} passed because the situation they check "
            "never arose (nothing forbidden, no matching call, no rule to apply). Those "
            "passes are evidence of nothing.",
        ]
    if result.load_errors:
        lines += ["", f"⚠️ {len(result.load_errors)} scenario file(s) could not be loaded."]
    footer = f"Run `{_code(result.run_id)}`"
    if report_url:
        footer += f" · [full report]({report_url})"
    lines += ["", f"<sub>{footer} · Opseval {_md(result.opseval_version, 30)}</sub>", ""]
    return "\n".join(lines)


__all__ = ["MARKER", "markdown_summary"]
