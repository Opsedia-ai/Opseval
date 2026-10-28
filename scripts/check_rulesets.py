# SPDX-License-Identifier: Apache-2.0
"""Check the committed ruleset for `main` against CI (ADR-0065).

    uv run python scripts/check_rulesets.py   # 0 when .github/rulesets/main.json is valid

`scripts/apply_rulesets.sh` runs this before it prints or applies anything, so a ruleset
that would block every merge (a required check no job reports) never reaches GitHub. The
required status checks must be exactly the checks `.github/workflows/ci.yml` reports, by
the names GitHub gives them: the job's ``name`` (or its id), and for a matrix job one check
per combination, ``check (3.11)``. A renamed or added job fails here until the ruleset
follows. A matrix this script cannot name for certain (``include``/``exclude``, an
expression in ``name``) fails closed rather than guessing.
"""

from __future__ import annotations

import itertools
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
RULESET = ROOT / ".github" / "rulesets" / "main.json"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
REQUIRED_RULES = (
    "deletion",
    "non_fast_forward",
    "required_linear_history",
    "pull_request",
    "required_status_checks",
)


class Unsupported(Exception):
    """A workflow shape whose check names cannot be derived for certain."""


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def check_names(workflow: dict[str, Any]) -> list[str]:
    """The status-check names GitHub reports for every job of a workflow."""
    names: list[str] = []
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        raise Unsupported("the workflow has no jobs")
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            raise Unsupported(f"job {job_id!r} is not a mapping")
        base = job.get("name", job_id)
        if not isinstance(base, str) or "${{" in base:
            raise Unsupported(f"job {job_id!r}: a name with an expression cannot be checked")
        matrix = (job.get("strategy") or {}).get("matrix")
        if matrix is None:
            names.append(base)
            continue
        if not isinstance(matrix, dict) or {"include", "exclude"} & set(matrix):
            raise Unsupported(f"job {job_id!r}: only a plain matrix of lists can be named")
        axes = list(matrix.values())
        if not axes or not all(isinstance(axis, list) and axis for axis in axes):
            raise Unsupported(f"job {job_id!r}: every matrix axis must be a non-empty list")
        for combo in itertools.product(*axes):
            names.append(f"{base} ({', '.join(_scalar(v) for v in combo)})")
    return names


def _rule(ruleset: dict[str, Any], kind: str) -> dict[str, Any]:
    for rule in ruleset.get("rules") or []:
        if isinstance(rule, dict) and rule.get("type") == kind:
            params = rule.get("parameters") or {}
            return params if isinstance(params, dict) else {}
    return {}


def problems(ruleset: Any, workflow: Any) -> list[str]:
    """Everything wrong with the ruleset; empty when it may be applied."""
    if not isinstance(ruleset, dict):
        return ["the ruleset is not a JSON object"]
    found: list[str] = []
    name = ruleset.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9 _.-]{1,100}", name):
        found.append(
            "`name` must be letters, digits, space, `_.-` (apply_rulesets.sh finds it by name)"
        )
    if ruleset.get("target") != "branch":
        found.append("`target` must be `branch`")
    if ruleset.get("enforcement") != "active":
        found.append("`enforcement` must be `active`")
    include = ((ruleset.get("conditions") or {}).get("ref_name") or {}).get("include") or []
    if "~DEFAULT_BRANCH" not in include:
        found.append("`conditions.ref_name.include` must contain `~DEFAULT_BRANCH`")
    kinds = [str(r.get("type")) for r in ruleset.get("rules") or [] if isinstance(r, dict)]
    found += [f"rule `{kind}` is missing" for kind in REQUIRED_RULES if kind not in kinds]
    found += [
        f"rule `{k}` appears more than once" for k in sorted(set(kinds)) if kinds.count(k) > 1
    ]

    review = _rule(ruleset, "pull_request")
    if "pull_request" in kinds:
        count = review.get("required_approving_review_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            found.append("`pull_request` needs at least one approving review")
        for flag in ("require_code_owner_review", "dismiss_stale_reviews_on_push"):
            if review.get(flag) is not True:
                found.append(f"`pull_request.{flag}` must be true")
        if "merge" in (review.get("allowed_merge_methods") or []):
            found.append("`allowed_merge_methods` cannot include `merge` with linear history")

    checks = _rule(ruleset, "required_status_checks").get("required_status_checks") or []
    required = [str(c.get("context")) for c in checks if isinstance(c, dict)]
    found += [
        f"required check `{c}` is listed twice"
        for c in sorted(set(required))
        if required.count(c) > 1
    ]
    if not isinstance(workflow, dict):
        return [*found, "the CI workflow is not a YAML mapping"]
    try:
        jobs = check_names(workflow)
    except Unsupported as exc:
        return [*found, f"cannot name the CI checks: {exc}"]
    found += [
        f"required check `{c}` is not reported by any CI job (renamed?)"
        for c in required
        if c not in jobs
    ]
    found += [f"CI check `{c}` is not required by the ruleset" for c in jobs if c not in required]
    if "dco" not in jobs:
        found.append("CI has no `dco` job (ADR-0065)")
    return found


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    ruleset_path = Path(args[0]) if args else RULESET
    workflow_path = Path(args[1]) if len(args) > 1 else WORKFLOW
    try:
        ruleset = json.loads(ruleset_path.read_text("utf-8"))
        workflow = yaml.safe_load(workflow_path.read_text("utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"rulesets: cannot read the ruleset or the workflow: {exc}", file=sys.stderr)
        return 1
    found = problems(ruleset, workflow)
    if found:
        print(
            f"rulesets: {ruleset_path.name} does not match {workflow_path.name}:", file=sys.stderr
        )
        for item in found:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print(
        f"rulesets: {ruleset_path.name} is valid and requires every check in {workflow_path.name}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
