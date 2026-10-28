# SPDX-License-Identifier: Apache-2.0
"""The GitHub drop-ins (ADR-0058, E1): the action, the reusable workflow and the examples.

There is no GitHub here, so these tests check what can be checked offline: every file parses,
every action is pinned, permissions are minimal, inputs reach scripts only through ``env:``,
the copies of the action cannot drift, and no example turns exit code 2 or 3 green.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
ROOT_ACTION = ROOT / "action.yml"
ACTION = ROOT / "integrations" / "github-action" / "action.yml"
WORKFLOWS = ROOT / ".github" / "workflows"
REUSABLE = WORKFLOWS / "opseval-eval.yml"
DOGFOOD = WORKFLOWS / "reusable-workflow.yml"
EXAMPLES = sorted((ROOT / "examples" / "github").glob("*.yml"))

SHA_PINNED = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(/[^@\s]+)?@[0-9a-f]{40}$")
# The refs an example may use instead of a SHA: readable, each with a comment to pin a SHA.
EXAMPLE_REFS = {
    "opsedia-ai/opseval-action@v1",
    "Opsedia-ai/Opseval/.github/workflows/opseval-eval.yml@<sha>",
}
# Inputs the reusable workflow adds to the action's, and the one it leaves out (the token
# is the called workflow's own).
WORKFLOW_ONLY = {"allowed-hosts", "opseval-ref", "start-demo-agent"}
ACTION_ONLY = {"github-token"}


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name} is not a mapping"
    return data


def _on(workflow: dict[str, Any]) -> Any:
    # YAML 1.1 reads a bare `on` key as True.
    return workflow.get("on", workflow.get(True))


def _workflow_files() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml")) + EXAMPLES


def _uses(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "uses" and isinstance(value, str):
                yield value
            else:
                yield from _uses(value)
    elif isinstance(node, list):
        for item in node:
            yield from _uses(item)


def _steps(workflow: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for job in workflow.get("jobs", {}).values():
        yield from job.get("steps", [])


def _ids(path: Path) -> str:
    return str(path.relative_to(ROOT))


def test_the_examples_are_found() -> None:
    names = {p.name for p in EXAMPLES}
    assert names == {
        "minimal.yml",
        "pull_request.yml",
        "tag-gate.yml",
        "release-published.yml",
        "deployment-status.yml",
    }


def test_the_root_action_is_the_source_apart_from_where_opseval_comes_from() -> None:
    """``Opsedia-ai/Opseval@<sha>`` users keep the root copy until go-public (ADR-0058)."""
    root = ROOT_ACTION.read_text(encoding="utf-8").splitlines()
    source = ACTION.read_text(encoding="utf-8").splitlines()
    assert len(root) == len(source)
    differ = [(a, b) for a, b in zip(root, source, strict=True) if a != b]
    assert len(differ) == 1, differ
    (in_root, in_source) = differ[0]
    assert in_root.replace('"$ACTION_PATH/packages/opseval"', "X") == in_source.replace(
        '"$ACTION_PATH/../../packages/opseval"', "X"
    )


@pytest.mark.parametrize("path", [ROOT_ACTION, ACTION, *_workflow_files()], ids=_ids)
def test_every_file_parses(path: Path) -> None:
    _load(path)


@pytest.mark.parametrize("path", [ROOT_ACTION, ACTION, *_workflow_files()], ids=_ids)
def test_every_action_is_pinned(path: Path) -> None:
    allowed = EXAMPLE_REFS if path in EXAMPLES else set()
    for ref in _uses(_load(path)):
        assert ref.startswith("./") or SHA_PINNED.match(ref) or ref in allowed, (
            f"{path.name}: {ref!r} is neither a full commit SHA nor a local path"
        )


@pytest.mark.parametrize("path", [REUSABLE, DOGFOOD, *EXAMPLES], ids=_ids)
def test_checkout_keeps_no_credentials(path: Path) -> None:
    for step in _steps(_load(path)):
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            assert step.get("with", {}).get("persist-credentials") is False, path.name


@pytest.mark.parametrize("path", [DOGFOOD, *EXAMPLES], ids=_ids)
def test_permissions_are_declared_and_minimal(path: Path) -> None:
    workflow = _load(path)
    assert "permissions" in workflow, f"{path.name} declares no top-level permissions"
    scopes = [workflow["permissions"]]
    for name, job in workflow["jobs"].items():
        if "steps" in job or "uses" in job:
            if path.name != "minimal.yml":
                assert "permissions" in job, f"{path.name}: job {name} declares no permissions"
            scopes.append(job.get("permissions", {}))
    for scope in scopes:
        assert isinstance(scope, dict), f"{path.name}: {scope!r} (never read-all/write-all)"
        for permission, level in scope.items():
            assert level in ("read", "none") or (permission, level) == (
                "pull-requests",
                "write",
            ), f"{path.name}: {permission}: {level}"


def _grants(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    scopes = [workflow.get("permissions", {})]
    scopes += [job.get("permissions", {}) for job in workflow["jobs"].values()]
    return [scope for scope in scopes if isinstance(scope, dict)]


@pytest.mark.parametrize("path", EXAMPLES, ids=_ids)
def test_only_a_pull_request_workflow_may_write_to_pull_requests(path: Path) -> None:
    workflow = _load(path)
    if any(scope.get("pull-requests") == "write" for scope in _grants(workflow)):
        assert _on(workflow) in ("pull_request", {"pull_request": None}), path.name


@pytest.mark.parametrize("path", [ROOT_ACTION, ACTION, REUSABLE, DOGFOOD, *EXAMPLES], ids=_ids)
def test_untrusted_values_reach_scripts_only_through_env(path: Path) -> None:
    """A ``${{ }}`` inside ``run:`` is pasted into the script: an injection (ADR-0015)."""
    data = _load(path)
    steps = data["runs"]["steps"] if "runs" in data else list(_steps(data))
    assert steps
    for step in steps:
        assert "${{" not in str(step.get("run", "")), f"{path.name}: {step.get('name')}"


@pytest.mark.parametrize("path", EXAMPLES, ids=_ids)
def test_no_example_turns_2_or_3_green(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    workflow = _load(path)
    for job in workflow["jobs"].values():
        assert "continue-on-error" not in job
        assert "always()" not in str(job.get("if", ""))
        assert "failure()" not in str(job.get("if", ""))
        for step in job.get("steps", []):
            assert "continue-on-error" not in step
            assert "|| true" not in str(step.get("run", ""))
    comments = "\n".join(line for line in text.splitlines() if line.lstrip().startswith("#"))
    assert "not judged" in comments and "harness" in comments, path.name
    assert all(code in comments for code in ("0 ", "1 ", "2 ", "3 ")), path.name


@pytest.mark.parametrize("path", EXAMPLES, ids=_ids)
def test_examples_say_where_the_action_comes_from_until_go_public(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "Gate 0" in text and "SHA" in text, path.name


def test_the_deployment_example_runs_only_on_success_from_the_deployed_url() -> None:
    workflow = _load(ROOT / "examples" / "github" / "deployment-status.yml")
    assert _on(workflow) in ("deployment_status", {"deployment_status": None})
    (job,) = workflow["jobs"].values()
    assert job["if"].replace(" ", "") == "github.event.deployment_status.state=='success'"
    first = job["steps"][0]
    assert first["env"]["ENVIRONMENT_URL"] == (
        "${{ github.event.deployment_status.environment_url }}"
    )


def _deployment_script() -> str:
    workflow = _load(ROOT / "examples" / "github" / "deployment-status.yml")
    (job,) = workflow["jobs"].values()
    return str(job["steps"][0]["run"])


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    ("url", "code", "target"),
    [
        ("https://Agent.Staging.example.com/", 0, "wss://agent.staging.example.com/ws"),
        (
            "https://agent.staging.example.com:8443/app",
            0,
            "wss://agent.staging.example.com:8443/ws",
        ),
        ("https://other.example.com/", 3, None),  # not allowlisted: exit 3, never a run
        ("http://agent.staging.example.com/", 3, None),  # no TLS
        ("https://agent.staging.example.com;id", 3, None),
        ("https://user@agent.staging.example.com/", 3, None),
        ("", 3, None),
    ],
)
def test_the_deployment_url_is_checked_before_it_is_used(
    url: str, code: int, target: str | None, tmp_path: Path
) -> None:
    output = tmp_path / "github_output"
    output.touch()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "ENVIRONMENT_URL": url,
        "OPSEVAL_ALLOWED_HOSTS": "api.openai.com, agent.staging.example.com",
        "AGENT_PATH": "/ws",
        "GITHUB_OUTPUT": str(output),
    }
    bash = shutil.which("bash")
    assert bash is not None
    done = subprocess.run(
        [bash, "-c", _deployment_script()], env=env, capture_output=True, text=True, check=False
    )
    assert done.returncode == code, done.stdout + done.stderr
    written = output.read_text(encoding="utf-8").strip()
    assert written == (f"target={target}" if target else "")


def test_the_reusable_workflow_runs_only_when_called() -> None:
    workflow = _load(REUSABLE)
    on = _on(workflow)
    assert isinstance(on, dict) and set(on) == {"workflow_call"}
    assert set(on["workflow_call"]["outputs"]) == {"exit-code", "gate"}
    assert set(on["workflow_call"]["secrets"]) == {
        "simulator-api-key",
        "judge-api-key",
        "agent-token",
    }
    assert all(not s.get("required") for s in on["workflow_call"]["secrets"].values())


def test_the_reusable_workflow_inherits_the_callers_permissions() -> None:
    """A called workflow can only lower the caller's token; declaring pull-requests: write
    would make every caller without it (a tag gate) fail, so each caller grants what it needs.
    """
    workflow = _load(REUSABLE)
    assert "permissions" not in workflow
    assert all("permissions" not in job for job in workflow["jobs"].values())


def test_the_reusable_workflow_mirrors_the_action() -> None:
    action = _load(ACTION)["inputs"]
    inputs = _on(_load(REUSABLE))["workflow_call"]["inputs"]
    assert set(inputs) == (set(action) - ACTION_ONLY) | WORKFLOW_ONLY
    for name in set(action) - ACTION_ONLY:
        assert inputs[name].get("required", False) == action[name].get("required", False), name
        assert inputs[name].get("default") == action[name].get("default"), name
        assert inputs[name]["type"] == "string", name
    (step,) = (s for s in _steps(_load(REUSABLE)) if s.get("id") == "gate")
    assert step["uses"] == "./.opseval-action/integrations/github-action"
    assert step["with"] == {n: f"${{{{ inputs.{n} }}}}" for n in set(action) - ACTION_ONLY}
    assert step["env"] == {
        "OPSEVAL_SIMULATOR_API_KEY": "${{ secrets.simulator-api-key }}",
        "OPSEVAL_JUDGE_API_KEY": "${{ secrets.judge-api-key }}",
        "OPSEVAL_STATIC_TOKEN": "${{ secrets.agent-token }}",
    }


def test_no_exit_code_from_the_reusable_workflow_is_a_harness_error() -> None:
    """If the action never ran to the end, the output must read 3, never empty or 0."""
    (job,) = _load(REUSABLE)["jobs"].values()
    assert job["outputs"]["exit-code"] == "${{ steps.gate.outputs.exit-code || '3' }}"


def test_ci_dogfoods_the_source_the_root_copy_and_the_reusable_workflow() -> None:
    action_job = _load(WORKFLOWS / "ci.yml")["jobs"]["action"]
    used = {step.get("uses") for step in action_job["steps"]}
    assert {"./", "./integrations/github-action"} <= used
    dogfood = _load(DOGFOOD)
    assert _on(dogfood) is not None
    calls = [job for job in dogfood["jobs"].values() if "uses" in job]
    assert [job["uses"] for job in calls] == ["./.github/workflows/opseval-eval.yml"]
    assert calls[0]["with"]["start-demo-agent"] is True
