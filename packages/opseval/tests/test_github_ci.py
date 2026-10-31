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
WORKFLOW_ONLY = {
    "allowed-hosts",
    "opseval-ref",
    "start-demo-agent",
    "simulator-base-url",
    "simulator-model",
    "judge-base-url",
    "judge-model",
    "opseval-env",
}
# The server key (ADR-0058 E2) is an action input, and the reusable workflow's secrets.
ACTION_ONLY = {"github-token", "opseval-public-key", "opseval-secret-key"}


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
        "report-to-server.yml",
    }


def test_the_root_action_is_the_source_byte_for_byte() -> None:
    """``Opsedia-ai/Opseval@<sha>`` users keep the root copy until go-public (ADR-0058). The
    install step finds Opseval next to either copy at run time, so there is nothing to differ.
    """
    assert ROOT_ACTION.read_bytes() == ACTION.read_bytes()


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
    assert job["if"].replace(" ", "") == (
        "github.event.deployment_status.state=='success'"
        "&&github.event.deployment.environment=='staging'"
    )
    first = job["steps"][0]
    assert first["env"]["ENVIRONMENT_URL"] == (
        "${{ github.event.deployment_status.environment_url }}"
    )
    assert first["env"]["OPSEVAL_AGENT_HOSTS"] == "${{ vars.OPSEVAL_AGENT_HOSTS }}"


def test_the_deployed_commit_is_data_only() -> None:
    """Its scenarios are read from a subdirectory; nothing is installed or run from it."""
    workflow = _load(ROOT / "examples" / "github" / "deployment-status.yml")
    (job,) = workflow["jobs"].values()
    (checkout,) = (s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["path"] == "deployed"
    assert checkout["with"]["ref"] == "${{ github.event.deployment.sha }}"
    (action,) = (s for s in job["steps"] if "opseval-action" in str(s.get("uses", "")))
    assert action["with"]["suite"].startswith("deployed/")
    assert action["with"]["profile"].startswith("deployed/")
    assert "read-dotenv" not in action["with"]
    runs = [str(s.get("run", "")) for s in job["steps"]]
    assert not any("deployed/" in r or "cd deployed" in r for r in runs)


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
        ("https://api.openai.com/", 3, None),
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
        "OPSEVAL_AGENT_HOSTS": "Agent.Staging.example.com, other-agent.example.com",
        # Allowlisted for the conversation is not a deployment target.
        "OPSEVAL_ALLOWED_HOSTS": "api.openai.com,other.example.com",
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


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("hosts", ["", " ", None])
def test_no_agent_hosts_is_exit_3(hosts: str | None, tmp_path: Path) -> None:
    env = {"ENVIRONMENT_URL": "https://agent.staging.example.com/", "AGENT_PATH": "/ws"}
    env["OPSEVAL_ALLOWED_HOSTS"] = "agent.staging.example.com"
    if hosts is not None:
        env["OPSEVAL_AGENT_HOSTS"] = hosts
    done, out = _gh_bash(_deployment_script(), env, tmp_path)
    assert done.returncode == 3, done.stdout + done.stderr
    assert "OPSEVAL_AGENT_HOSTS" in done.stdout
    assert out["output"] == ""


def test_the_reusable_workflow_runs_only_when_called() -> None:
    workflow = _load(REUSABLE)
    on = _on(workflow)
    assert isinstance(on, dict) and set(on) == {"workflow_call"}
    assert set(on["workflow_call"]["outputs"]) == {"exit-code", "gate"}
    assert set(on["workflow_call"]["secrets"]) == {
        "simulator-api-key",
        "judge-api-key",
        "agent-token",
        "opseval-public-key",
        "opseval-secret-key",
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
    steps = {s.get("id"): s for s in _steps(_load(REUSABLE))}
    assert steps["gate"]["uses"] == "./.opseval-action/integrations/github-action"
    assert steps["gate-self"]["uses"] == "./integrations/github-action"
    assert steps["gate"]["if"] == "steps.ref.outputs.source == 'pinned'"
    assert steps["gate-self"]["if"] == "steps.ref.outputs.source == 'self'"
    for step in (steps["gate"], steps["gate-self"]):
        assert step["with"] == {n: f"${{{{ inputs.{n} }}}}" for n in set(action) - ACTION_ONLY}
        assert step["env"] == {
            "OPSEVAL_SIMULATOR_API_KEY": "${{ secrets.simulator-api-key }}",
            "OPSEVAL_JUDGE_API_KEY": "${{ secrets.judge-api-key }}",
            "OPSEVAL_STATIC_TOKEN": "${{ secrets.agent-token }}",
            "OPSEVAL_PUBLIC_KEY": "${{ secrets.opseval-public-key }}",
            "OPSEVAL_SECRET_KEY": "${{ secrets.opseval-secret-key }}",
        }


def test_the_reusable_workflow_clones_opseval_only_when_pinned() -> None:
    """Inside Opseval's own repository the workspace already is the action: no second clone."""
    steps = list(_steps(_load(REUSABLE)))
    clones = [s for s in steps if s.get("with", {}).get("path") == ".opseval-action"]
    assert len(clones) == 1
    assert clones[0]["if"] == "steps.ref.outputs.source == 'pinned'"
    assert clones[0]["with"]["repository"] == "Opsedia-ai/Opseval"
    assert clones[0]["with"]["ref"] == "${{ inputs.opseval-ref }}"


def test_no_exit_code_from_the_reusable_workflow_is_a_harness_error() -> None:
    """If the action never ran to the end, the output must read 3, never empty or 0."""
    (job,) = _load(REUSABLE)["jobs"].values()
    assert job["outputs"]["exit-code"] == "${{ steps.result.outputs.exit-code }}"
    (result,) = (s for s in job["steps"] if s.get("id") == "result")
    assert result["if"] == "always()"


def test_ci_dogfoods_the_source_the_root_copy_and_the_reusable_workflow() -> None:
    action_job = _load(WORKFLOWS / "ci.yml")["jobs"]["action"]
    used = {step.get("uses") for step in action_job["steps"]}
    assert {"./", "./integrations/github-action"} <= used
    dogfood = _load(DOGFOOD)
    assert _on(dogfood) is not None
    calls = [job for job in dogfood["jobs"].values() if "uses" in job]
    assert [job["uses"] for job in calls] == ["./.github/workflows/opseval-eval.yml"]
    assert calls[0]["with"]["start-demo-agent"] is True


# --- bash run the way GitHub runs it ------------------------------------------------------


def _gh_bash(
    script: str, env: dict[str, str], tmp_path: Path, path: str | None = None
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    """Run a step's script as GitHub does (``bash -eo pipefail``); return the files it wrote."""
    bash = shutil.which("bash")
    assert bash is not None
    files = {k: tmp_path / k for k in ("output", "env", "path", "summary")}
    for f in files.values():
        f.write_text("", encoding="utf-8")
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    full = {
        "PATH": path or os.environ.get("PATH", ""),
        "GITHUB_OUTPUT": str(files["output"]),
        "GITHUB_ENV": str(files["env"]),
        "GITHUB_PATH": str(files["path"]),
        "GITHUB_STEP_SUMMARY": str(files["summary"]),
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_RUN_ID": "1",
        "GITHUB_RUN_ATTEMPT": "1",
        **env,
    }
    done = subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        env=full,
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    return done, {k: f.read_text(encoding="utf-8") for k, f in files.items()}


def _step(steps: list[dict[str, Any]], name: str) -> dict[str, Any]:
    (step,) = (s for s in steps if s.get("name") == name or s.get("id") == name)
    return step


def _action_steps() -> list[dict[str, Any]]:
    steps = _load(ACTION)["runs"]["steps"]
    assert isinstance(steps, list)
    return steps


def _reusable_steps() -> list[dict[str, Any]]:
    return list(_steps(_load(REUSABLE)))


needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


@needs_bash
@pytest.mark.parametrize(
    ("ref", "caller", "code", "source"),
    [
        ("", "Opsedia-ai/Opseval", 0, "self"),
        ("", "someone/else", 3, None),  # outside Opseval an empty ref is never "whatever main is"
        ("0123456789abcdef0123456789abcdef01234567", "someone/else", 0, "pinned"),
        ("0123456789abcdef0123456789abcdef01234567", "Opsedia-ai/Opseval", 0, "pinned"),
        ("main", "someone/else", 3, None),
        ("v1", "Opsedia-ai/Opseval", 3, None),
        ("0123456789ABCDEF0123456789ABCDEF01234567", "someone/else", 3, None),
        ("0123456789abcdef", "someone/else", 3, None),  # a short SHA can be ambiguous
        ("0123456789abcdef0123456789abcdef01234567\nx", "someone/else", 3, None),
    ],
)
def test_opseval_ref_is_a_full_sha_or_empty_inside_opseval(
    ref: str, caller: str, code: int, source: str | None, tmp_path: Path
) -> None:
    step = _step(_reusable_steps(), "ref")
    assert step["env"] == {
        "OPSEVAL_REF": "${{ inputs.opseval-ref }}",
        "CALLER": "${{ github.repository }}",
    }
    done, out = _gh_bash(step["run"], {"OPSEVAL_REF": ref, "CALLER": caller}, tmp_path)
    assert done.returncode == code, done.stdout + done.stderr
    assert out["output"].strip() == (f"source={source}" if source else "")


def _settings(
    tmp_path: Path, opseval_env: str = "", **inputs: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    env = {
        "SIMULATOR_BASE_URL": "",
        "SIMULATOR_MODEL": "",
        "JUDGE_BASE_URL": "",
        "JUDGE_MODEL": "",
        "HAS_SIMULATOR_KEY": "false",
        "HAS_JUDGE_KEY": "false",
        "OPSEVAL_ENV": opseval_env,
    }
    env.update(inputs)
    return _gh_bash(_step(_reusable_steps(), "Settings")["run"], env, tmp_path)


def test_settings_reach_the_step_only_through_env() -> None:
    step = _step(_reusable_steps(), "Settings")
    assert step["env"]["OPSEVAL_ENV"] == "${{ inputs.opseval-env }}"
    assert step["env"]["JUDGE_MODEL"] == "${{ inputs.judge-model }}"
    # Only whether a key is there, never the key itself.
    assert step["env"]["HAS_JUDGE_KEY"] == "${{ secrets.judge-api-key != '' }}"
    assert step["env"]["HAS_SIMULATOR_KEY"] == "${{ secrets.simulator-api-key != '' }}"


def test_the_secret_settings_the_workflow_refuses_are_configs() -> None:
    from opseval.config import SECRET_FIELDS

    script = _step(_reusable_steps(), "Settings")["run"]
    (line,) = (ln for ln in script.splitlines() if ln.strip().startswith('secret="'))
    listed = set(line.split('"')[1].split())
    assert listed == {f"OPSEVAL_{name.upper()}" for name in SECRET_FIELDS}


@needs_bash
def test_settings_are_written_to_the_job_environment(tmp_path: Path) -> None:
    done, out = _settings(
        tmp_path,
        "# a comment\n\nOPSEVAL_SIMULATOR_TIMEOUT_SECONDS=120\nOPSEVAL_LANGFUSE_HOST=https://l.example.com/a=b\n",
        JUDGE_BASE_URL="https://api.openai.com/v1",
        JUDGE_MODEL="gpt-4.1-mini",
        HAS_JUDGE_KEY="true",
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert out["env"].splitlines() == [
        "OPSEVAL_JUDGE_BASE_URL=https://api.openai.com/v1",
        "OPSEVAL_JUDGE_MODEL=gpt-4.1-mini",
        "OPSEVAL_SIMULATOR_TIMEOUT_SECONDS=120",
        "OPSEVAL_LANGFUSE_HOST=https://l.example.com/a=b",
    ]


@needs_bash
@pytest.mark.parametrize(
    ("opseval_env", "message"),
    [
        ("OPSEVAL_JUDGE_API_KEY=sk-x", "a secret"),
        ("OPSEVAL_DATABASE_URL=postgresql://u:p@h/db", "a secret"),
        ("OPSEVAL_WEBHOOK_URL=https://hooks.example.com/x", "a secret"),
        ("OPSEVAL_JUDGE_MODEL=gpt-4.1", "its own input"),
        ("OPSEVAL_ALLOWED_HOSTS=evil.example.com", "its own input"),
        ("OPSEVAL_DOTENV=true", "its own input"),
        ("PATH=/tmp/evil", "not OPSEVAL_NAME=value"),
        ("GITHUB_TOKEN=x", "not OPSEVAL_NAME=value"),
        ("opseval_judge_model=x", "not OPSEVAL_NAME=value"),
        ("OPSEVAL_X<<EOF", "not OPSEVAL_NAME=value"),
        ("OPSEVAL_X<<EOF=y", "not OPSEVAL_NAME=value"),
        (" OPSEVAL_X=y", "not OPSEVAL_NAME=value"),
        ("OPSEVAL_X=y\r", "carriage return"),
        ("OPSEVAL_X=1\nOPSEVAL_X=2", "twice"),
    ],
)
def test_opseval_env_refuses_secrets_and_other_shapes(
    opseval_env: str, message: str, tmp_path: Path
) -> None:
    done, out = _settings(tmp_path, opseval_env)
    assert done.returncode == 3, done.stdout + done.stderr
    assert message in done.stdout
    assert out["env"] == ""  # nothing written when anything is refused


@needs_bash
def test_an_endpoint_input_with_a_newline_is_refused(tmp_path: Path) -> None:
    done, out = _settings(tmp_path, JUDGE_MODEL="gpt\nOPSEVAL_ALLOWED_HOSTS=evil.example.com")
    assert done.returncode == 3, done.stdout + done.stderr
    assert out["env"] == ""


@needs_bash
@pytest.mark.parametrize("role", ["simulator", "judge"])
@pytest.mark.parametrize(
    ("base", "model"),
    [("", ""), ("https://api.openai.com/v1", ""), ("", "gpt-4.1-mini")],
)
def test_a_key_nothing_would_use_is_exit_3(
    role: str, base: str, model: str, tmp_path: Path
) -> None:
    upper = role.upper()
    done, out = _settings(
        tmp_path,
        **{f"HAS_{upper}_KEY": "true", f"{upper}_BASE_URL": base, f"{upper}_MODEL": model},
    )
    assert done.returncode == 3, done.stdout + done.stderr
    assert f"{role}-api-key is set" in done.stdout
    assert out["env"] == ""


@needs_bash
def test_an_endpoint_without_a_key_is_allowed(tmp_path: Path) -> None:
    """A keyless local model is a real setup; the endpoint is still allowlisted by Opseval."""
    done, _ = _settings(
        tmp_path, SIMULATOR_BASE_URL="http://127.0.0.1:11434/v1", SIMULATOR_MODEL="llama3"
    )
    assert done.returncode == 0, done.stdout + done.stderr


@needs_bash
@pytest.mark.parametrize(
    ("pinned", "own", "code"),
    [("0", "", "0"), ("", "2", "2"), ("1", "", "1"), ("", "", "3"), ("", "3", "3")],
)
def test_the_reusable_workflow_reports_the_code_of_whichever_action_ran(
    pinned: str, own: str, code: str, tmp_path: Path
) -> None:
    step = _step(_reusable_steps(), "result")
    env = {"PINNED_CODE": pinned, "PINNED_GATE": "", "SELF_CODE": own, "SELF_GATE": ""}
    done, out = _gh_bash(step["run"], env, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"exit-code={code}" in out["output"].splitlines()


# --- the action ---------------------------------------------------------------------------


def test_the_install_ignores_the_workspace() -> None:
    steps = _action_steps()
    (setup,) = (s for s in steps if str(s.get("uses", "")).startswith("astral-sh/setup-uv@"))
    assert setup["with"]["working-directory"] == "${{ runner.temp }}"
    assert setup["with"]["enable-cache"] == "false"
    install = _step(steps, "Install Opseval")
    assert install["working-directory"] == "${{ runner.temp }}"
    assert install["env"]["UV_NO_CONFIG"] == "1"
    assert '"$RUNNER_TEMP/opseval-venv"' in install["run"]


def _fake(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(exist_ok=True)
    tool = bin_dir / name
    tool.write_text("#!/usr/bin/env bash\n" + body + "\n", encoding="utf-8")
    tool.chmod(0o755)


def _bin_path(bin_dir: Path) -> str:
    return f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


@needs_bash
@pytest.mark.parametrize("copy", ["root", "source", "none", "spec"])
def test_the_install_finds_opseval_next_to_either_copy(copy: str, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    log = tmp_path / "uv.log"
    # A fake uv: records the install, and makes the venv's opseval answer --version.
    _fake(
        bin_dir,
        "uv",
        f'echo "$@" >> "{log}"\n'
        'if [ "$1" = venv ]; then for a; do v="$a"; done; mkdir -p "$v/bin";'
        ' printf "#!/usr/bin/env bash\\necho 0.0\\n" > "$v/bin/opseval";'
        ' chmod +x "$v/bin/opseval"; fi',
    )
    repo = tmp_path / "repo"
    (repo / "packages" / "opseval").mkdir(parents=True)
    (repo / "integrations" / "github-action").mkdir(parents=True)
    action_path = {
        "root": repo,
        "source": repo / "integrations" / "github-action",
        "none": tmp_path / "elsewhere",
        "spec": tmp_path / "elsewhere",
    }[copy]
    env = {
        "OPSEVAL_SPEC": "opseval==0.2.0" if copy == "spec" else "",
        "ACTION_PATH": str(action_path),
    }
    done, out = _gh_bash(
        _step(_action_steps(), "Install Opseval")["run"], env, tmp_path, _bin_path(bin_dir)
    )
    if copy == "none":
        assert done.returncode == 3, done.stdout + done.stderr
        return
    assert done.returncode == 0, done.stdout + done.stderr
    installed = log.read_text(encoding="utf-8").splitlines()[-1]
    want = {
        "root": f"{repo}/packages/opseval",
        "source": f"{repo}/integrations/github-action/../../packages/opseval",
        "spec": "opseval==0.2.0",
    }[copy]
    assert installed.endswith(" " + want), installed
    assert out["path"].strip().endswith("opseval-venv/bin")


def _run_env(**extra: str) -> dict[str, str]:
    env = {
        "SUITE": "s.yaml",
        "PROFILE": "p.yaml",
        "TARGET": "ws://127.0.0.1:1",
        "TRANSPORT": "websocket",
        "REPEATS": "",
        "FILTERS": "",
        "EXTRA_ARGS": "",
        "READ_DOTENV": "false",
        "SERVER_URL": "",
        "PROJECT": "",
        "TASK": "default",
        "LABEL": "production",
        "PUBLIC_KEY_INPUT": "",
        "SECRET_KEY_INPUT": "",
    }
    env.update(extra)
    return env


@needs_bash
@pytest.mark.parametrize(
    ("read_dotenv", "seen"), [("false", "false"), ("true", "true"), ("", "false")]
)
def test_the_run_reads_no_dotenv_unless_asked(read_dotenv: str, seen: str, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _fake(bin_dir, "opseval", f'echo "$OPSEVAL_DOTENV" > "{tmp_path}/dotenv"; exit 2')
    step = _step(_action_steps(), "run")
    assert step["env"]["READ_DOTENV"] == "${{ inputs.read-dotenv }}"
    done, out = _gh_bash(
        step["run"], _run_env(READ_DOTENV=read_dotenv), tmp_path, _bin_path(bin_dir)
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert (tmp_path / "dotenv").read_text(encoding="utf-8").strip() == seen
    assert "exit-code=2" in out["output"].splitlines()


@needs_bash
def test_an_unreadable_result_warns_and_keeps_the_code(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    # Writes a broken result.json into the reports dir it was given, then fails the agent.
    _fake(
        bin_dir,
        "opseval",
        'while [ $# -gt 0 ]; do if [ "$1" = --reports-dir ]; then d="$2"; fi; shift; done;'
        ' mkdir -p "$d/run1"; echo "{" > "$d/run1/result.json"; exit 1',
    )
    done, out = _gh_bash(
        _step(_action_steps(), "run")["run"], _run_env(), tmp_path, _bin_path(bin_dir)
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "exit-code=1" in out["output"].splitlines()
    assert "::warning::" in done.stdout


@needs_bash
@pytest.mark.parametrize("broken", ["summary", "list", "post"])
def test_a_broken_summary_or_comment_only_warns(broken: str, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "result.json").write_text("{}", encoding="utf-8")
    _fake(bin_dir, "opseval", "exit 1" if broken == "summary" else "echo '<!-- opseval-gate -->'")
    gh = {
        "list": "exit 1",
        "post": 'if [ "$2" = -X ]; then exit 1; fi; [ "$1" = api ] && [ "$2" = -X ] || true',
        "summary": "exit 0",
    }[broken]
    _fake(bin_dir, "gh", gh)
    step = _step(_action_steps(), "Gate summary")
    env = {
        "RUN_DIR": str(run_dir),
        "REPORT_URL": "",
        "COMMENT": "true",
        "GH_TOKEN": "t",
        "PR_NUMBER": "7",
        "REPO": "o/r",
        "OPSEVAL_DOTENV": "false",
    }
    done, _ = _gh_bash(step["run"], env, tmp_path, _bin_path(bin_dir))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "::warning::" in done.stdout


def test_the_exit_step_always_runs_last() -> None:
    steps = _action_steps()
    last = steps[-1]
    assert last["name"] == "Exit with Opseval's exit code"
    assert last["if"] == "always()"
    assert last["env"] == {"CODE": "${{ steps.run.outputs.exit-code }}"}


@needs_bash
@pytest.mark.parametrize(("code", "want"), [("0", 0), ("1", 1), ("2", 2), ("3", 3), ("", 3)])
def test_the_exit_step_keeps_every_code_and_no_code_is_3(
    code: str, want: int, tmp_path: Path
) -> None:
    done, _ = _gh_bash(_action_steps()[-1]["run"], {"CODE": code}, tmp_path)
    assert done.returncode == want


def test_a_fork_pr_gets_no_llm_endpoint_without_its_key() -> None:
    """With the endpoint set and the key missing every LLM request fails (exit 3); with
    neither, LLM personas are skipped and judges unmeasured (exit 2): the honest answer."""
    (job,) = _load(ROOT / "examples" / "github" / "pull_request.yml")["jobs"].values()
    (step,) = (s for s in job["steps"] if "opseval-action" in str(s.get("uses", "")))
    for role in ("SIMULATOR", "JUDGE"):
        for setting in ("BASE_URL", "MODEL"):
            value = step["env"][f"OPSEVAL_{role}_{setting}"]
            assert value == (
                f"${{{{ secrets.OPSEVAL_{role}_API_KEY != '' && vars.OPSEVAL_{role}_{setting}"
                " || '' }}"
            )


def test_the_tag_gate_passes_the_endpoints_as_inputs() -> None:
    (eval_job, _) = _load(ROOT / "examples" / "github" / "tag-gate.yml")["jobs"].values()
    for name in ("simulator-base-url", "simulator-model", "judge-base-url", "judge-model"):
        assert eval_job["with"][name].startswith("${{ vars.OPSEVAL_"), name
    for line in eval_job["with"]["opseval-env"].splitlines():
        assert re.match(r"^OPSEVAL_[A-Z0-9_]+=", line), line


# --- OPSEVAL_DOTENV ------------------------------------------------------------------------


def test_dotenv_false_reads_no_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from opseval.config import Settings

    (tmp_path / ".env").write_text(
        "OPSEVAL_JUDGE_MODEL=from-dotenv\nT_X=tenant\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPSEVAL_JUDGE_MODEL", raising=False)
    monkeypatch.delenv("T_X", raising=False)
    monkeypatch.delenv("OPSEVAL_DOTENV", raising=False)
    assert Settings().judge_model == "from-dotenv"
    assert Settings().secret_named("T_X") == "tenant"
    for off in ("false", "0", "No", "off"):
        monkeypatch.setenv("OPSEVAL_DOTENV", off)
        settings = Settings()
        assert settings.judge_model is None
        assert settings.secret_named("T_X") is None
        assert settings.dotenv is False
    monkeypatch.setenv("OPSEVAL_DOTENV", "true")
    assert Settings().judge_model == "from-dotenv"


# --- reporting to a server (ADR-0058 E2) ------------------------------------------------------


def _record_args(bin_dir: Path, tmp_path: Path) -> None:
    _fake(
        bin_dir,
        "opseval",
        f'printf "%s\\n" "$@" > "{tmp_path}/args"\n'
        'printf "%s\\n" "${OPSEVAL_PUBLIC_KEY:-}" "${OPSEVAL_SECRET_KEY:-}"'
        f' > "{tmp_path}/keys"\n'
        "exit 1",
    )


@needs_bash
def test_the_action_reports_to_a_server_with_the_key_from_its_inputs(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _record_args(bin_dir, tmp_path)
    step = _step(_action_steps(), "run")
    for name, value in {
        "SERVER_URL": "${{ inputs.server-url }}",
        "PROJECT": "${{ inputs.project }}",
        "TASK": "${{ inputs.task }}",
        "LABEL": "${{ inputs.label }}",
        "PUBLIC_KEY_INPUT": "${{ inputs.opseval-public-key }}",
        "SECRET_KEY_INPUT": "${{ inputs.opseval-secret-key }}",
    }.items():
        assert step["env"][name] == value
    env = _run_env(
        PROFILE="",
        SUITE="suites/smoke.yaml",
        SERVER_URL="https://opseval.example.com",
        PROJECT="acme/bot",
        LABEL="staging",
        PUBLIC_KEY_INPUT="pk-oe-1",
        SECRET_KEY_INPUT="sk-oe-secret",
    )
    done, out = _gh_bash(step["run"], env, tmp_path, _bin_path(bin_dir))
    assert done.returncode == 0, done.stdout + done.stderr
    args = (tmp_path / "args").read_text(encoding="utf-8").splitlines()
    assert "--profile" not in args  # the label's own profile
    at = args.index("--server")
    assert args[at : at + 8] == [
        "--server",
        "https://opseval.example.com",
        "--project",
        "acme/bot",
        "--task",
        "default",
        "--label",
        "staging",
    ]
    assert (tmp_path / "keys").read_text().splitlines() == ["pk-oe-1", "sk-oe-secret"]
    assert "::add-mask::sk-oe-secret" in done.stdout
    assert "exit-code=1" in out["output"].splitlines()  # the local verdict


@needs_bash
def test_the_action_keeps_the_jobs_own_key_when_the_inputs_are_empty(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _record_args(bin_dir, tmp_path)
    env = _run_env(SERVER_URL="http://127.0.0.1:8000", PROJECT="acme/bot")
    env.update(OPSEVAL_PUBLIC_KEY="pk-job", OPSEVAL_SECRET_KEY="sk-job")
    done, _ = _gh_bash(_step(_action_steps(), "run")["run"], env, tmp_path, _bin_path(bin_dir))
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "keys").read_text().splitlines() == ["pk-job", "sk-job"]
    assert "--profile" in (tmp_path / "args").read_text().splitlines()


@needs_bash
@pytest.mark.parametrize(
    ("server", "project", "said"),
    [("https://opseval.example.com", "", "needs project"), ("", "acme/bot", "needs server-url")],
)
def test_a_server_without_a_project_or_the_reverse_is_3(
    server: str, project: str, said: str, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "bin"
    _record_args(bin_dir, tmp_path)
    env = _run_env(SERVER_URL=server, PROJECT=project)
    done, out = _gh_bash(_step(_action_steps(), "run")["run"], env, tmp_path, _bin_path(bin_dir))
    assert done.returncode == 0 and said in done.stdout
    assert "exit-code=3" in out["output"].splitlines()
    assert not (tmp_path / "args").exists()


def test_the_reusable_workflow_passes_the_key_only_as_secrets() -> None:
    inputs = _on(_load(REUSABLE))["workflow_call"]["inputs"]
    assert {"server-url", "project", "task", "label"} <= set(inputs)
    assert not {"opseval-public-key", "opseval-secret-key"} & set(inputs)
    script = _step(_reusable_steps(), "Settings")["run"]
    (own,) = (ln for ln in script.splitlines() if ln.strip().startswith('own="'))
    assert " OPSEVAL_PUBLIC_KEY " in own  # its own secret, never in opseval-env
