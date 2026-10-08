# SPDX-License-Identifier: Apache-2.0
"""JUnit XML for CI systems (one test case per scenario).

Mapping keeps the core promise: ``failed`` is a failure; ``inconclusive`` is an
*error* (never a pass); ``skipped_precondition`` and ``blocked`` are skipped. Scenarios
that could not be loaded appear as one errored case, so they cannot vanish silently, and
a score made on degraded evidence is named even when the case passed.
"""

from __future__ import annotations

# Only builds XML; never parses untrusted input.
from xml.etree import ElementTree as ET  # nosec B405

from opseval.models.gate import describe
from opseval.models.results import ScoreStatus, SuiteResult, TestVerdict, VerdictStatus


def _duration(result: SuiteResult) -> str:
    return f"{max((result.ended_at - result.started_at).total_seconds(), 0.0):.3f}"


def _failure_text(t: TestVerdict) -> str:
    lines = []
    for a in t.attempts:
        state = "not judged" if a.passed is None else ("passed" if a.passed else "failed")
        lines.append(f"attempt {a.attempt_index}: {state} (ended: {a.terminal_state.value})")
        if a.invalid_reason:
            lines.append(f"  invalidated: {a.invalid_reason}")
        for s in a.scores:
            if s.status is not ScoreStatus.PASSED:
                value = "not measured" if s.score is None else f"{s.score:.2f}"
                reason = f" - {s.reason}" if s.reason else ""
                lines.append(f"  {s.status.value}: {s.scorer_id} = {value}{reason}")
    return "\n".join(lines)


def render_junit(result: SuiteResult) -> str:
    counts: dict[str, int] = {
        "tests": len(result.tests),
        "failures": 0,
        "errors": 0,
        "skipped": 0,
    }
    suite = ET.Element("testsuite", name=f"opseval.{result.suite}", time=_duration(result))
    props = ET.SubElement(suite, "properties")
    for key, value in (
        ("run_id", result.run_id),
        ("target", result.target),
        ("profile", result.profile),
        ("gate", result.gate.status),
        ("gate_policy", describe(result.gate.policy) if result.gate.policy else "standard"),
        ("exit_code", str(result.exit_code)),
    ):
        ET.SubElement(props, "property", name=key, value=value)
    if result.degraded_scorers:
        ET.SubElement(
            props,
            "property",
            name="degraded_scorers",
            value=",".join(result.degraded_scorers),
        )
    if result.vacuous_scorers:
        ET.SubElement(
            props,
            "property",
            name="vacuous_scorers",
            value=",".join(result.vacuous_scorers),
        )
    if result.unmeasured_scorers:
        ET.SubElement(
            props,
            "property",
            name="unmeasured_scorers",
            value=",".join(result.unmeasured_scorers),
        )

    for t in result.tests:
        case = ET.SubElement(
            suite, "testcase", classname=f"opseval.{result.suite}", name=f"{t.test_id}: {t.name}"
        )
        case_props = ET.SubElement(case, "properties")
        ET.SubElement(case_props, "property", name="criticality", value=t.criticality.value)
        if t.pass_rate is not None:
            ET.SubElement(case_props, "property", name="pass_rate", value=f"{t.pass_rate:.4f}")
        if t.flaky:
            ET.SubElement(case_props, "property", name="flaky", value="true")
        if t.degraded_scorers:
            ET.SubElement(
                case_props,
                "property",
                name="degraded_scorers",
                value=",".join(t.degraded_scorers),
            )
        if t.unmeasured_scorers:
            ET.SubElement(
                case_props,
                "property",
                name="unmeasured_scorers",
                value=",".join(t.unmeasured_scorers),
            )
        if t.status is VerdictStatus.FAILED:
            counts["failures"] += 1
            el = ET.SubElement(case, "failure", message=t.reason or "failed", type="failed")
            el.text = _failure_text(t)
        elif t.status is VerdictStatus.INCONCLUSIVE:
            counts["errors"] += 1
            el = ET.SubElement(
                case, "error", message=t.reason or "inconclusive", type="inconclusive"
            )
            el.text = _failure_text(t)
        elif t.status in (VerdictStatus.SKIPPED_PRECONDITION, VerdictStatus.BLOCKED):
            counts["skipped"] += 1
            ET.SubElement(case, "skipped", message=f"{t.status.value}: {t.reason or ''}")
        if t.degraded_scorers:
            # Added last: the JUnit sequence is properties, (error|failure)*, system-out,
            # and a schema-validating consumer may drop a failure that follows it. A
            # passed case has no failure element, so this is the only place the weaker
            # evidence can appear at all.
            note = ET.SubElement(case, "system-out")
            note.text = (
                "degraded evidence: "
                + ", ".join(t.degraded_scorers)
                + " scored on weaker evidence than they ask for; these scores counted "
                "toward the verdict"
            )

    if result.load_errors:
        counts["tests"] += 1
        counts["errors"] += 1
        case = ET.SubElement(
            suite, "testcase", classname=f"opseval.{result.suite}", name="catalogue load errors"
        )
        el = ET.SubElement(
            case, "error", message=f"{len(result.load_errors)} scenario(s) could not be loaded"
        )
        el.text = "\n".join(result.load_errors)

    for key, count in counts.items():
        suite.set(key, str(count))
    ET.indent(suite)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(suite, encoding="unicode") + "\n"
    )
