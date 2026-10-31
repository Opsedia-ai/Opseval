# SPDX-License-Identifier: Apache-2.0
"""The templates for other CI systems (ADR-0058, E3).

No GitLab, Jenkins, Azure DevOps, CircleCI or Bitbucket runs here, and no image is built. So
these tests check what can be checked offline: every file parses, every copy of the shared
script is identical to the source, that script is run with a fake ``opseval`` (and a fake
``docker``) on PATH to prove exit codes 0-3 come out unchanged and only 0 passes, settings
follow the E1 rules, secrets never reach a command line, and no template has a switch that
turns exit 2 or 3 green.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
TEMPLATES = ROOT / "templates"
SHARED = TEMPLATES / "shared" / "opseval-ci.sh"
GITLAB = TEMPLATES / "gitlab" / "opseval.yml"
GITLAB_EXAMPLE = TEMPLATES / "gitlab" / "example.gitlab-ci.yml"
JENKINS = TEMPLATES / "jenkins" / "vars" / "opseval.groovy"
JENKINS_RESOURCE = TEMPLATES / "jenkins" / "resources" / "opseval" / "opseval-ci.sh"
JENKINSFILE = TEMPLATES / "jenkins" / "Jenkinsfile.example"
AZURE = TEMPLATES / "azure" / "opseval.yml"
AZURE_EXAMPLE = TEMPLATES / "azure" / "azure-pipelines.example.yml"
ORB = TEMPLATES / "circleci" / "orb" / "orb.yml"
PIPE = TEMPLATES / "bitbucket" / "pipe" / "pipe.sh"
PIPE_YML = TEMPLATES / "bitbucket" / "pipe" / "pipe.yml"
PIPE_DOCKERFILE = TEMPLATES / "bitbucket" / "pipe" / "Dockerfile"
BITBUCKET_EXAMPLE = TEMPLATES / "bitbucket" / "bitbucket-pipelines.example.yml"
ACTION = ROOT / "integrations" / "github-action" / "action.yml"
GUIDE = ROOT / "docs" / "guide" / "ci.md"

YAML_FILES = sorted(TEMPLATES.rglob("*.yml"))
ALL_FILES = sorted(p for p in TEMPLATES.rglob("*") if p.is_file())
PLACEHOLDER = "ghcr.io/opsedia-ai/opseval:<version>"
SECRETS = ("OPSEVAL_SIMULATOR_API_KEY", "OPSEVAL_JUDGE_API_KEY", "OPSEVAL_STATIC_TOKEN")
SERVER_KEYS = ("OPSEVAL_PUBLIC_KEY", "OPSEVAL_SECRET_KEY")
"""The project key ``run --server`` reports with (ADR-0058 E2): secrets in every system, as
the three above are, and the action's inputs ``opseval-public-key``/``opseval-secret-key``."""
KEY_INPUTS = {"opseval-public-key", "opseval-secret-key"}

# The action's inputs that have a counterpart here, and the reusable workflow's settings
# inputs (ADR-0058, E1). python-version and opseval-spec do not apply (the image is the
# install), comment and github-token are GitHub's, artifact-name is offered where it means
# something.
GITHUB_ONLY = {"python-version", "opseval-spec", "comment", "github-token", "artifact-name"}
SETTINGS_INPUTS = {
    "allowed-hosts",
    "simulator-base-url",
    "simulator-model",
    "judge-base-url",
    "judge-model",
    "opseval-env",
}

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _ids(path: Path) -> str:
    return str(path.relative_to(ROOT))


def _load_sync() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ci_templates", ROOT / "scripts" / "ci_templates.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ci_templates"] = module
    spec.loader.exec_module(module)
    return module


SYNC = _load_sync()


def _body(path: Path) -> str:
    _, _, _, lines = SYNC.block(path.read_text(encoding="utf-8"))
    return "\n".join(lines) + "\n"


def _yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _gitlab() -> tuple[dict[str, Any], dict[str, Any]]:
    spec, jobs = yaml.safe_load_all(GITLAB.read_text(encoding="utf-8"))
    return spec, jobs


def _gitlab_job() -> dict[str, Any]:
    _, jobs = _gitlab()
    (job,) = jobs.values()
    return dict(job)


def _azure_step() -> dict[str, Any]:
    return dict(_yaml(AZURE)["steps"][0])


def _orb_step() -> dict[str, Any]:
    return dict(_yaml(ORB)["commands"]["evaluate"]["steps"][0]["run"])


def _kebab(name: str) -> str:
    if name.isupper():
        return name.lower().replace("_", "-")
    return re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower().replace("_", "-")


def _expected_inputs() -> set[str]:
    """The action's inputs, plus the secret settings named for the templates to pass on (the
    action reads every OPSEVAL_ secret from the job's own environment)."""
    action = set(_yaml(ACTION)["inputs"])
    return (action - GITHUB_ONLY - KEY_INPUTS) | SETTINGS_INPUTS | {"secret-env-names"}


def _code_lines(path: Path) -> list[str]:
    """The lines that are not comments (shell/YAML ``#``, Groovy ``//``)."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        out.append(line)
    return out


# --- every file ---------------------------------------------------------------------------


def test_the_templates_are_found() -> None:
    names = {_ids(p) for p in ALL_FILES}
    for path in (
        SHARED,
        GITLAB,
        GITLAB_EXAMPLE,
        JENKINS,
        JENKINS_RESOURCE,
        JENKINSFILE,
        AZURE,
        AZURE_EXAMPLE,
        ORB,
        PIPE,
        PIPE_YML,
        PIPE_DOCKERFILE,
        BITBUCKET_EXAMPLE,
    ):
        assert _ids(path) in names


@pytest.mark.parametrize("path", YAML_FILES, ids=_ids)
def test_every_yaml_file_parses(path: Path) -> None:
    docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    assert docs and all(isinstance(d, dict) for d in docs)


def test_every_copy_of_the_shared_script_is_identical() -> None:
    """One script, five copies: ``python scripts/ci_templates.py`` regenerates them."""
    source = _body(SHARED)
    for path in SYNC.COPIES:
        assert _body(path) == source, f"{_ids(path)} drifted: run python scripts/ci_templates.py"
    assert SYNC.main(["--check"]) == 0


def test_the_inlined_copies_are_the_scripts_the_systems_run() -> None:
    """The marker block is inside the script value itself, not a comment beside it."""
    source = _body(SHARED)
    (gitlab_script,) = _gitlab_job()["script"]
    assert gitlab_script == source
    assert _azure_step()["bash"].endswith(source)
    assert _orb_step()["command"].endswith(source)


@needs_bash
@pytest.mark.parametrize("path", [SHARED, PIPE, JENKINS_RESOURCE], ids=_ids)
def test_every_shell_script_parses(path: Path) -> None:
    done = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


@needs_bash
@pytest.mark.parametrize("which", ["gitlab", "azure", "circleci"])
def test_every_inlined_script_parses(which: str, tmp_path: Path) -> None:
    script = {
        "gitlab": lambda: _gitlab_job()["script"][0],
        "azure": lambda: _azure_step()["bash"],
        "circleci": lambda: _orb_step()["command"],
    }[which]()
    path = tmp_path / "script.sh"
    path.write_text(script, encoding="utf-8")
    done = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


def test_the_shared_script_has_no_ci_syntax_in_it() -> None:
    """The same text is read by four CI systems before bash sees it."""
    body = _body(SHARED)
    assert "<<" not in body  # CircleCI parameters (and so no heredocs)
    assert "${{" not in body  # Azure DevOps template expressions
    assert "$[[" not in body  # GitLab inputs
    # Azure DevOps expands $(name) macros anywhere in a script, even inside quotes.
    assert not re.findall(r"\$\([A-Za-z0-9._-]+\)", body)


def test_the_refused_secret_settings_are_configs() -> None:
    from opseval.config import SECRET_FIELDS

    (line,) = (ln for ln in _body(SHARED).splitlines() if ln.startswith('oe_secret="'))
    listed = set(line.split('"')[1].split())
    assert listed == {f"OPSEVAL_{name.upper()}" for name in SECRET_FIELDS}


def test_the_refused_own_settings_match_the_reusable_workflow() -> None:
    (line,) = (ln for ln in _body(SHARED).splitlines() if ln.startswith('oe_own="'))
    workflow = (ROOT / ".github" / "workflows" / "opseval-eval.yml").read_text(encoding="utf-8")
    (theirs,) = (ln.strip() for ln in workflow.splitlines() if ln.strip().startswith('own="'))
    assert set(line.split('"')[1].split()) == set(theirs.split('"')[1].split())


GREEN_SWITCHES = (
    "|| true",
    "|| :",
    "set +e",
    "allow_failure",
    "continue-on-error",
    "continueOnError",
    "catchError",
    "unstable(",
    "currentBuild.result",
    "ignore_exit",
)


@pytest.mark.parametrize("path", ALL_FILES, ids=_ids)
def test_no_template_turns_2_or_3_green(path: Path) -> None:
    for line in _code_lines(path):
        for switch in GREEN_SWITCHES:
            assert switch not in line, f"{_ids(path)}: {line.strip()}"


@pytest.mark.parametrize("path", [GITLAB, JENKINS, AZURE, ORB, PIPE_YML, PIPE_DOCKERFILE], ids=_ids)
def test_the_image_defaults_to_the_placeholder(path: Path) -> None:
    assert PLACEHOLDER in path.read_text(encoding="utf-8") or (
        path in (PIPE_YML,) and "opseval-pipe:<version>" in path.read_text(encoding="utf-8")
    )


# --- the shared script, run -----------------------------------------------------------------

FAKE_OPSEVAL = r"""#!/usr/bin/env bash
# A stand-in for the Opseval CLI: records what it was given, writes a run, exits FAKE_CODE.
if [ "$1" = summary ]; then
  if [ -n "${FAKE_SUMMARY_FAILS:-}" ]; then exit 1; fi
  echo "# Opseval gate: fake"
  exit 0
fi
printf '%s\n' "$@" > "$FAKE_LOG/args"
env | grep '^OPSEVAL_' | sort > "$FAKE_LOG/env"
if [ -n "${FAKE_OUTPUT:-}" ]; then printf '%b\n' "$FAKE_OUTPUT"; fi
while [ $# -gt 0 ]; do
  case "$1" in
    --reports-dir) reports="$2"; shift 2 ;;
    *) shift ;;
  esac
done
mkdir -p "$reports/20261028-run"
if [ -z "${FAKE_NO_RESULT:-}" ]; then
  # As SuiteResult.model_dump_json(indent=2) writes it: a nested exit_code is a policy.
  printf '{\n  "gate": {\n    "exit_code": "strict"\n  },\n  "exit_code": %s,\n  "x": 1\n}\n' \
    "${FAKE_RECORDED:-${FAKE_CODE:-0}}" > "$reports/20261028-run/result.json"
  echo '<testsuites/>' > "$reports/junit.xml"
fi
exit "${FAKE_CODE:-0}"
"""

# A stand-in for `docker run`: records its argv and env file, then runs the fake opseval with
# only the environment the container would get (the env file and the `-e NAME` variables).
FAKE_DOCKER = r"""#!/usr/bin/env bash
if [ -n "${FAKE_DOCKER_CODE:-}" ]; then exit "$FAKE_DOCKER_CODE"; fi
printf '%s\n' "$@" >> "$FAKE_LOG/docker"
shift  # run
envs=()
while [ $# -gt 0 ]; do
  case "$1" in
    --env-file)
      cp "$2" "$FAKE_LOG/envfile"
      while IFS= read -r l; do envs+=("$l"); done < "$2"
      shift 2 ;;
    -e) if [[ "$2" == *=* ]]; then envs+=("$2"); else envs+=("$2=${!2}"); fi; shift 2 ;;
    --user | -v | -w) shift 2 ;;
    --rm) shift ;;
    *) shift; break ;;  # the image
  esac
done
exec env -i PATH="$PATH" FAKE_LOG="$FAKE_LOG" FAKE_CODE="${FAKE_CODE:-0}" \
  FAKE_NO_RESULT="${FAKE_NO_RESULT:-}" FAKE_SUMMARY_FAILS="${FAKE_SUMMARY_FAILS:-}" \
  FAKE_RECORDED="${FAKE_RECORDED:-}" FAKE_OUTPUT="${FAKE_OUTPUT:-}" \
  ${envs[@]+"${envs[@]}"} opseval "$@"
"""


# Stand-ins for a job running as root: `id` answers FAKE_UID, `chown` and `setpriv` record what
# they were asked, and `setpriv` runs the rest as uid 10001 (unless told it keeps root).
FAKE_ID = r"""#!/usr/bin/env bash
case "$1" in
  -u | -g) if [ -n "${FAKE_UID:-}" ]; then echo "$FAKE_UID"; exit 0; fi ;;
esac
exec /usr/bin/id "$@"
"""
FAKE_CHOWN = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG/chown"
"""
FAKE_SETPRIV = r"""#!/usr/bin/env bash
printf '%s\n' "$@" > "$FAKE_LOG/setpriv"
while [ $# -gt 0 ]; do
  case "$1" in --*) shift ;; *) break ;; esac
done
if [ -z "${FAKE_SETPRIV_KEEPS_ROOT:-}" ]; then export FAKE_UID=10001; fi
exec "$@"
"""


def _executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run(
    tmp_path: Path, script: str | Path = SHARED, *, fake_root: bool = False, **env: str
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """Run a script in a fresh workspace; return the result, the workspace and the fake's log."""
    bin_dir = tmp_path / "bin"
    log = tmp_path / "log"
    work = tmp_path / "work"
    for d in (bin_dir, log, work):
        d.mkdir(exist_ok=True)
    _executable(bin_dir / "opseval", FAKE_OPSEVAL)
    _executable(bin_dir / "docker", FAKE_DOCKER)
    if fake_root:
        _executable(bin_dir / "id", FAKE_ID)
        _executable(bin_dir / "chown", FAKE_CHOWN)
        _executable(bin_dir / "setpriv", FAKE_SETPRIV)
        env.setdefault("FAKE_UID", "0")
    if isinstance(script, str):
        path = tmp_path / "inline.sh"
        path.write_text(script, encoding="utf-8")
    else:
        path = script
    base = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "FAKE_LOG": str(log),
        "SUITE": "evals/suites/smoke.yaml",
        "PROFILE": "evals/profile.yaml",
        "TARGET": "wss://agent.example.com/ws",
    }
    base.update(env)
    base = {k: v for k, v in base.items() if v is not None}
    # bash -e as GitLab and CircleCI run a job's script: the script must not depend on it.
    done = subprocess.run(
        ["bash", "-e", str(path)], cwd=work, env=base, capture_output=True, text=True, check=False
    )
    return done, work, log


def _args(log: Path) -> list[str]:
    return (log / "args").read_text(encoding="utf-8").splitlines()


def _env(log: Path) -> dict[str, str]:
    lines = (log / "env").read_text(encoding="utf-8").splitlines()
    # A line with no "=" continues another variable's multi-line value (SECRET_ENV_NAMES).
    return dict(line.split("=", 1) for line in lines if "=" in line)


@needs_bash
@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("0", "pass (exit 0)"),
        ("1", "the agent failed (exit 1)"),
        ("2", "not judged (exit 2)"),
        ("3", "harness or configuration error (exit 3)"),
    ],
)
@pytest.mark.parametrize("mode", ["local", "docker"])
def test_exit_codes_come_out_unchanged(code: str, message: str, mode: str, tmp_path: Path) -> None:
    done, work, _ = _run(tmp_path, FAKE_CODE=code, MODE=mode, IMAGE="opseval")
    assert done.returncode == int(code), done.stdout + done.stderr
    assert message in done.stdout + done.stderr
    assert (work / "opseval-reports" / "junit.xml").is_file()
    assert (work / "opseval-reports" / "summary.md").read_text().startswith("# Opseval gate")


@needs_bash
@pytest.mark.parametrize("code", ["0", "1", "2"])
@pytest.mark.parametrize("mode", ["local", "docker"])
def test_a_verdict_without_a_result_is_a_configuration_error(
    code: str, mode: str, tmp_path: Path
) -> None:
    """Click exits 2 on a bad flag, and an image whose entrypoint is not opseval may exit 0 or
    1: with no result.json nothing was judged, so it is never a pass or an agent failure."""
    done, _, _ = _run(tmp_path, FAKE_CODE=code, FAKE_NO_RESULT="1", MODE=mode, IMAGE="opseval")
    assert done.returncode == 3, done.stdout + done.stderr
    assert "nothing was judged" in done.stderr
    assert "refused its arguments" in done.stderr and "entrypoint" in done.stderr


@needs_bash
@pytest.mark.parametrize(("code", "recorded"), [("0", "1"), ("1", "0"), ("0", "3"), ("2", "")])
def test_a_code_the_run_did_not_record_is_3(code: str, recorded: str, tmp_path: Path) -> None:
    done, _, _ = _run(tmp_path, FAKE_CODE=code, FAKE_RECORDED=recorded or '"x"')
    assert done.returncode == 3, done.stdout + done.stderr
    assert "cannot be trusted" in done.stderr


@needs_bash
@pytest.mark.parametrize(
    "extra", ["--help", "--version", "--no-traces --help", "--reports-dir /tmp/x", "--junit=x.xml"]
)
def test_extra_args_that_would_judge_nothing_are_refused(extra: str, tmp_path: Path) -> None:
    done, _, log = _run(tmp_path, EXTRA_ARGS=extra)
    assert done.returncode == 3
    assert "EXTRA_ARGS has" in done.stderr
    assert not (log / "args").exists()


@needs_bash
@pytest.mark.parametrize("code", ["7", "125", "130"])
def test_any_other_code_is_a_harness_error(code: str, tmp_path: Path) -> None:
    done, _, _ = _run(tmp_path, FAKE_CODE=code)
    assert done.returncode == 3
    assert f"did not finish (exit {code})" in done.stderr


@needs_bash
def test_docker_that_cannot_start_the_image_is_3(tmp_path: Path) -> None:
    done, _, _ = _run(tmp_path, MODE="docker", IMAGE="opseval", FAKE_DOCKER_CODE="125")
    assert done.returncode == 3


@needs_bash
def test_a_stale_junit_file_is_never_published_as_this_runs(tmp_path: Path) -> None:
    reports = tmp_path / "work" / "opseval-reports"
    reports.mkdir(parents=True)
    for name in ("junit.xml", "summary.md", "run-dir"):
        (reports / name).write_text("from an earlier build", encoding="utf-8")
    done, _, _ = _run(tmp_path, FAKE_CODE="3", FAKE_NO_RESULT="1")
    assert done.returncode == 3
    assert not (reports / "junit.xml").exists()
    assert not (reports / "summary.md").exists()


@needs_bash
def test_a_summary_that_cannot_be_written_only_warns(tmp_path: Path) -> None:
    done, _, _ = _run(tmp_path, FAKE_CODE="0", FAKE_SUMMARY_FAILS="1")
    assert done.returncode == 0
    assert "could not write the gate summary" in done.stderr


@needs_bash
def test_the_run_gets_the_inputs_as_flags(tmp_path: Path) -> None:
    done, work, log = _run(
        tmp_path,
        TRANSPORT="http-json",
        REPEATS="3",
        FILTER="tag=smoke\nid=refund-1\n",
        EXTRA_ARGS="--no-traces  --no-preflight",
        JUNIT_PATH="out/junit.xml",
    )
    assert done.returncode == 0, done.stderr
    args = _args(log)
    run_dir = args[args.index("--reports-dir") + 1]
    assert args == [
        "run", "evals/suites/smoke.yaml", "--profile", "evals/profile.yaml",
        "--target", "wss://agent.example.com/ws", "--transport", "http-json",
        "--repeats", "3", "--filter", "tag=smoke", "--filter", "id=refund-1",
        "--reports-dir", run_dir, "--junit", f"{run_dir}/junit.xml",
        "--no-traces", "--no-preflight",
    ]  # fmt: skip
    assert run_dir.startswith("opseval-reports/build-")
    assert (work / "out" / "junit.xml").is_file()
    assert (work / "opseval-reports" / "run-dir").read_text().strip() == f"{run_dir}/20261028-run"


@needs_bash
@pytest.mark.parametrize("missing", ["SUITE", "PROFILE", "TARGET"])
def test_a_missing_required_input_is_3(missing: str, tmp_path: Path) -> None:
    done, _, log = _run(tmp_path, **{missing: ""})
    assert done.returncode == 3
    assert f"{missing} is required" in done.stderr
    assert not (log / "args").exists()


@needs_bash
@pytest.mark.parametrize(("value", "seen"), [("", "false"), ("false", "false"), ("true", "true")])
def test_no_dotenv_unless_asked(value: str, seen: str, tmp_path: Path) -> None:
    done, _, log = _run(tmp_path, READ_DOTENV=value)
    assert done.returncode == 0
    assert _env(log)["OPSEVAL_DOTENV"] == seen


@needs_bash
def test_read_dotenv_must_be_a_boolean(tmp_path: Path) -> None:
    done, _, _ = _run(tmp_path, READ_DOTENV="yes")
    assert done.returncode == 3


@needs_bash
def test_settings_reach_opseval(tmp_path: Path) -> None:
    done, _, log = _run(
        tmp_path,
        ALLOWED_HOSTS="agent.example.com,api.openai.com",
        JUDGE_BASE_URL="https://api.openai.com/v1",
        JUDGE_MODEL="gpt-4.1-mini",
        OPSEVAL_JUDGE_API_KEY="sk-judge",
        OPSEVAL_ENV="# a comment\n\nOPSEVAL_SIMULATOR_TIMEOUT_SECONDS=120\n"
        "OPSEVAL_LANGFUSE_HOST=https://l.example.com/a=b c\n",
    )
    assert done.returncode == 0, done.stderr
    env = _env(log)
    assert env["OPSEVAL_ALLOWED_HOSTS"] == "agent.example.com,api.openai.com"
    assert env["OPSEVAL_JUDGE_MODEL"] == "gpt-4.1-mini"
    assert env["OPSEVAL_SIMULATOR_TIMEOUT_SECONDS"] == "120"
    assert env["OPSEVAL_LANGFUSE_HOST"] == "https://l.example.com/a=b c"
    assert env["OPSEVAL_JUDGE_API_KEY"] == "sk-judge"
    assert "OPSEVAL_ENV" not in env


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
        (" OPSEVAL_X=y", "not OPSEVAL_NAME=value"),
        ("OPSEVAL_X=y\r", "carriage return"),
        ("OPSEVAL_X=1\nOPSEVAL_X=2", "twice"),
    ],
)
@pytest.mark.parametrize("mode", ["local", "docker"])
def test_opseval_env_refuses_secrets_and_other_shapes(
    opseval_env: str, message: str, mode: str, tmp_path: Path
) -> None:
    done, _, log = _run(tmp_path, OPSEVAL_ENV=opseval_env, MODE=mode, IMAGE="opseval")
    assert done.returncode == 3, done.stdout + done.stderr
    assert message in done.stderr
    assert not (log / "args").exists()  # nothing ran
    assert not (log / "docker").exists()


@needs_bash
def test_an_input_with_a_newline_is_refused(tmp_path: Path) -> None:
    done, _, log = _run(tmp_path, JUDGE_MODEL="gpt\nOPSEVAL_ALLOWED_HOSTS=evil.example.com")
    assert done.returncode == 3
    assert not (log / "args").exists()


@needs_bash
@pytest.mark.parametrize("role", ["SIMULATOR", "JUDGE"])
@pytest.mark.parametrize(
    ("base", "model"), [("", ""), ("https://api.openai.com/v1", ""), ("", "gpt-4.1-mini")]
)
def test_a_key_nothing_would_use_is_3(role: str, base: str, model: str, tmp_path: Path) -> None:
    done, _, log = _run(
        tmp_path,
        **{f"OPSEVAL_{role}_API_KEY": "sk-x", f"{role}_BASE_URL": base, f"{role}_MODEL": model},
    )
    assert done.returncode == 3
    assert f"OPSEVAL_{role}_API_KEY is set" in done.stderr
    assert not (log / "args").exists()


@needs_bash
def test_in_the_image_the_jobs_own_endpoint_counts(tmp_path: Path) -> None:
    """GitLab and CircleCI hand project variables to the job: an endpoint set there is used."""
    endpoint = {
        "OPSEVAL_JUDGE_API_KEY": "sk-x",
        "OPSEVAL_JUDGE_BASE_URL": "https://api.openai.com/v1",
        "OPSEVAL_JUDGE_MODEL": "gpt-4.1-mini",
    }
    done, _, _ = _run(tmp_path, MODE="local", **endpoint)
    assert done.returncode == 0, done.stderr
    # docker run passes the container only the inputs, so there the key would be unused.
    done, _, _ = _run(tmp_path, MODE="docker", IMAGE="opseval", **endpoint)
    assert done.returncode == 3


@needs_bash
def test_an_endpoint_without_a_key_is_allowed(tmp_path: Path) -> None:
    done, _, _ = _run(
        tmp_path, SIMULATOR_BASE_URL="http://127.0.0.1:11434/v1", SIMULATOR_MODEL="llama3"
    )
    assert done.returncode == 0, done.stderr


@needs_bash
def test_docker_gets_secrets_by_name_never_by_value(tmp_path: Path) -> None:
    done, _, log = _run(
        tmp_path,
        MODE="docker",
        IMAGE="registry.example.com/opseval:0.2.0",
        JUDGE_BASE_URL="https://api.openai.com/v1",
        JUDGE_MODEL="gpt-4.1-mini",
        OPSEVAL_JUDGE_API_KEY="sk-judge-secret",
        OPSEVAL_STATIC_TOKEN="agent-secret",
        OPSEVAL_ENV="OPSEVAL_SIMULATOR_TIMEOUT_SECONDS=120",
        OPSEVAL_LANGFUSE_HOST="https://not-forwarded.example.com",
    )
    assert done.returncode == 0, done.stderr
    argv = (log / "docker").read_text(encoding="utf-8")
    assert "sk-judge-secret" not in argv and "agent-secret" not in argv
    lines = argv.splitlines()
    assert lines[lines.index("OPSEVAL_JUDGE_API_KEY") - 1 :][:2] == ["-e", "OPSEVAL_JUDGE_API_KEY"]
    assert "OPSEVAL_SIMULATOR_API_KEY" not in lines  # not set, not passed
    envfile = (log / "envfile").read_text(encoding="utf-8")
    assert "secret" not in envfile
    assert envfile.splitlines() == [
        "OPSEVAL_DOTENV=false",
        "OPSEVAL_JUDGE_BASE_URL=https://api.openai.com/v1",
        "OPSEVAL_JUDGE_MODEL=gpt-4.1-mini",
        "OPSEVAL_SIMULATOR_TIMEOUT_SECONDS=120",
    ]
    env = _env(log)
    assert env["OPSEVAL_JUDGE_API_KEY"] == "sk-judge-secret"
    assert "OPSEVAL_LANGFUSE_HOST" not in env  # only the inputs reach the container
    # Never root, and the env file is gone afterwards.
    user = lines[lines.index("--user") + 1]
    assert not user.startswith("0:")
    assert user == f"{os.getuid()}:{os.getgid()}"
    assert not list(tmp_path.glob("opseval-env.*"))


@needs_bash
def test_the_placeholder_image_is_refused_with_how_to_build_it(tmp_path: Path) -> None:
    done, _, log = _run(tmp_path, MODE="docker", IMAGE=PLACEHOLDER)
    assert done.returncode == 3
    assert "docker build -t opseval ." in done.stderr
    assert not (log / "docker").exists()


@needs_bash
def test_an_unknown_mode_is_3(tmp_path: Path) -> None:
    done, _, _ = _run(tmp_path, MODE="kubernetes")
    assert done.returncode == 3


@needs_bash
def test_each_build_publishes_only_its_own_run(tmp_path: Path) -> None:
    """An earlier build's run folder (marked by the script) goes; nothing else is touched."""
    reports = tmp_path / "work" / "opseval-reports"
    old = reports / "build-earlier"
    (old / "20261027-run").mkdir(parents=True)
    (old / ".opseval-ci-build").write_text("", encoding="utf-8")
    (old / "20261027-run" / "result.json").write_text("{}", encoding="utf-8")
    unmarked = reports / "build-mine"
    unmarked.mkdir()
    (reports / "notes.txt").write_text("keep", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".opseval-ci-build").write_text("", encoding="utf-8")
    (reports / "build-link").symlink_to(elsewhere)
    done, _, _ = _run(tmp_path)
    assert done.returncode == 0, done.stderr
    assert not old.exists()
    assert unmarked.is_dir() and (reports / "notes.txt").is_file()
    assert (reports / "build-link").is_symlink() and (elsewhere / ".opseval-ci-build").exists()
    builds = [p for p in reports.glob("build-*") if (p / ".opseval-ci-build").is_file()]
    builds = [p for p in builds if not p.is_symlink()]
    assert len(builds) == 1
    run_dir = (reports / "run-dir").read_text(encoding="utf-8").strip()
    assert run_dir.startswith(f"opseval-reports/{builds[0].name}/")


@needs_bash
@pytest.mark.parametrize("mode", ["local", "docker"])
def test_named_secrets_reach_opseval_by_name_only(mode: str, tmp_path: Path) -> None:
    done, _, log = _run(
        tmp_path,
        MODE=mode,
        IMAGE="opseval",
        SECRET_ENV_NAMES="OPSEVAL_LANGFUSE_SECRET_KEY, OPSEVAL_WEBHOOK_URL\nOPSEVAL_TTS_KEY",
        OPSEVAL_LANGFUSE_SECRET_KEY="lf-secret-value",
        OPSEVAL_WEBHOOK_URL="https://hooks.example.com/secret-path",
    )
    assert done.returncode == 0, done.stderr
    env = _env(log)
    assert env["OPSEVAL_LANGFUSE_SECRET_KEY"] == "lf-secret-value"
    assert env["OPSEVAL_WEBHOOK_URL"] == "https://hooks.example.com/secret-path"
    assert "lists OPSEVAL_TTS_KEY, but this job has no value" in done.stderr
    if mode == "docker":
        argv = (log / "docker").read_text(encoding="utf-8")
        envfile = (log / "envfile").read_text(encoding="utf-8")
        for value in ("lf-secret-value", "secret-path"):
            assert value not in argv and value not in envfile
        lines = argv.splitlines()
        for name in ("OPSEVAL_LANGFUSE_SECRET_KEY", "OPSEVAL_WEBHOOK_URL"):
            assert lines[lines.index(name) - 1] == "-e"
        assert "OPSEVAL_TTS_KEY" not in lines  # named but unset: not passed


@needs_bash
def test_every_secret_setting_can_be_named(tmp_path: Path) -> None:
    from opseval.config import SECRET_FIELDS

    names = {f"OPSEVAL_{n.upper()}" for n in SECRET_FIELDS} - set(SECRETS) - set(SERVER_KEYS)
    done, _, _ = _run(tmp_path, SECRET_ENV_NAMES=" ".join(sorted(names)))
    assert done.returncode == 0, done.stderr


@needs_bash
@pytest.mark.parametrize(
    ("names", "message"),
    [
        ("OPSEVAL_LANGFUSE_HOST", "not a secret setting"),
        ("OPSEVAL_ALLOWED_HOSTS", "not a secret setting"),
        ("PATH", "not a secret setting"),
        ("OPSEVAL_LANGFUSE_SECRET_KEY OPSEVAL_NOPE", "not a secret setting"),
        ("OPSEVAL_JUDGE_API_KEY", "has its own input"),
        ("OPSEVAL_STATIC_TOKEN", "has its own input"),
    ],
)
def test_only_secret_settings_can_be_named(names: str, message: str, tmp_path: Path) -> None:
    done, _, log = _run(tmp_path, SECRET_ENV_NAMES=names, MODE="docker", IMAGE="opseval")
    assert done.returncode == 3
    assert message in done.stderr
    assert not (log / "docker").exists()


AZURE_COMMANDS = (
    "##vso[task.complete result=Succeeded;]done",
    "  ##vso[task.setvariable variable=x]y",
    "##VSO[task.complete result=Succeeded]",
    "##[error]looks like a failure",
    "the agent said ##vso[task.uploadfile]/etc/passwd",
)


@needs_bash
@pytest.mark.parametrize("code", ["0", "1"])
def test_on_azure_the_agents_words_cannot_be_logging_commands(code: str, tmp_path: Path) -> None:
    done, _, _ = _run(
        tmp_path, TF_BUILD="True", FAKE_CODE=code, FAKE_OUTPUT="\\n".join(AZURE_COMMANDS)
    )
    assert done.returncode == int(code), done.stderr
    out = done.stdout + done.stderr
    assert not re.search(r"##vso\[", out, re.IGNORECASE)
    assert "##[" not in out
    assert "#​#vso[task.complete" in out and "#​#[error]" in out
    # Off Azure DevOps the output is left alone.
    done, _, _ = _run(tmp_path, FAKE_OUTPUT=AZURE_COMMANDS[0])
    assert AZURE_COMMANDS[0] in done.stdout


@needs_bash
def test_docker_mounts_the_workspace_at_its_own_path(tmp_path: Path) -> None:
    done, work, log = _run(tmp_path, MODE="docker", IMAGE="opseval")
    assert done.returncode == 0, done.stderr
    lines = (log / "docker").read_text(encoding="utf-8").splitlines()
    ws = str(work.resolve())
    assert lines[lines.index("-v") + 1] == f"{ws}:{ws}"
    assert lines[lines.index("-w") + 1] == ws


@needs_bash
def test_docker_mounts_the_host_path_of_the_workspace_when_given(tmp_path: Path) -> None:
    done, work, log = _run(
        tmp_path, MODE="docker", IMAGE="opseval", DOCKER_WORKSPACE_HOST_PATH="/srv/jenkins/ws/app"
    )
    assert done.returncode == 0, done.stderr
    lines = (log / "docker").read_text(encoding="utf-8").splitlines()
    assert lines[lines.index("-v") + 1] == f"/srv/jenkins/ws/app:{work.resolve()}"


@needs_bash
@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"SUITE": "/etc/suite.yaml"}, "SUITE '/etc/suite.yaml' is outside the workspace"),
        ({"PROFILE": "../profile.yaml"}, "PROFILE '../profile.yaml' is outside the workspace"),
        ({"REPORTS_DIR": "../out"}, "REPORTS_DIR '../out' is outside the workspace"),
        ({"REPORTS_DIR": "/tmp/opseval-out"}, "outside the workspace"),
        ({"DOCKER_WORKSPACE_HOST_PATH": "ws"}, "must be an absolute path"),
        ({"DOCKER_WORKSPACE_HOST_PATH": "/a:b"}, "cannot mount"),
    ],
)
def test_docker_refuses_paths_the_container_cannot_see(
    env: dict[str, str], message: str, tmp_path: Path
) -> None:
    done, _, log = _run(tmp_path, MODE="docker", IMAGE="opseval", **env)
    assert done.returncode == 3, done.stdout + done.stderr
    assert message in done.stderr
    assert not (log / "docker").exists()


@needs_bash
def test_docker_accepts_paths_inside_the_workspace(tmp_path: Path) -> None:
    work = (tmp_path / "work").resolve()
    (work / "evals").mkdir(parents=True)
    (work / "evals" / "suite.yaml").write_text("", encoding="utf-8")
    done, _, log = _run(
        tmp_path,
        MODE="docker",
        IMAGE="opseval",
        SUITE=str(work / "evals" / "suite.yaml"),
        PROFILE="builtin:openai-chat",
        REPORTS_DIR="out/reports",
    )
    assert done.returncode == 0, done.stderr
    assert _args(log)[1] == str(work / "evals" / "suite.yaml")


@needs_bash
def test_a_root_job_gives_the_image_user_the_run_folder_never_0777(tmp_path: Path) -> None:
    done, work, log = _run(tmp_path, fake_root=True, MODE="docker", IMAGE="opseval")
    assert done.returncode == 0, done.stderr
    lines = (log / "docker").read_text(encoding="utf-8").splitlines()
    assert lines[lines.index("--user") + 1] == "10001:10001"
    (chown,) = (log / "chown").read_text(encoding="utf-8").splitlines()
    assert chown.startswith("-R 10001:10001 opseval-reports/build-")
    build = work / chown.split()[-1]
    assert stat.S_IMODE(build.stat().st_mode) == 0o700


# --- the platform preludes, run --------------------------------------------------------------


@needs_bash
@pytest.mark.parametrize("code", ["0", "1", "2", "3"])
def test_the_bitbucket_pipe_keeps_the_code(code: str, tmp_path: Path) -> None:
    done, work, _ = _run(tmp_path, PIPE, FAKE_CODE=code)
    assert done.returncode == int(code), done.stderr
    # Bitbucket finds test reports under test-results/; the run folder stays out of it.
    assert (work / "test-results" / "opseval" / "junit.xml").is_file()
    assert not list((work / "test-results").rglob("result.json"))


@needs_bash
def test_the_bitbucket_pipe_maps_its_secret_variables(tmp_path: Path) -> None:
    done, _, log = _run(
        tmp_path,
        PIPE,
        JUDGE_API_KEY="sk-judge",
        JUDGE_BASE_URL="https://api.openai.com/v1",
        JUDGE_MODEL="gpt-4.1-mini",
        OPSEVAL_SIMULATOR_API_KEY="from-the-image-env",
    )
    assert done.returncode == 0, done.stderr
    env = _env(log)
    assert env["OPSEVAL_JUDGE_API_KEY"] == "sk-judge"
    # Only the pipe's own variables count: nothing else sets a key.
    assert "OPSEVAL_SIMULATOR_API_KEY" not in env


@needs_bash
def test_the_bitbucket_pipe_drops_root_before_anything_runs(tmp_path: Path) -> None:
    done, work, log = _run(tmp_path, PIPE, fake_root=True, FAKE_CODE="1")
    assert done.returncode == 1, done.stderr
    assert _args(log)[0] == "run"
    assert (log / "setpriv").read_text(encoding="utf-8").splitlines()[:3] == [
        "--reuid=10001",
        "--regid=10001",
        "--clear-groups",
    ]
    (chown,) = (log / "chown").read_text(encoding="utf-8").splitlines()
    assert chown == "10001:10001 opseval-reports test-results/opseval"
    assert (work / "test-results" / "opseval" / "junit.xml").is_file()


@needs_bash
def test_the_bitbucket_pipe_never_runs_opseval_as_root(tmp_path: Path) -> None:
    done, _, log = _run(tmp_path, PIPE, fake_root=True, FAKE_SETPRIV_KEEPS_ROOT="1")
    assert done.returncode == 3
    assert "still root" in done.stderr
    assert not (log / "args").exists()


@needs_bash
def test_the_bitbucket_pipe_maps_secrets_after_dropping_root(tmp_path: Path) -> None:
    """The mapping runs once, as 10001: the re-run must not lose the keys."""
    done, _, log = _run(
        tmp_path,
        PIPE,
        fake_root=True,
        JUDGE_API_KEY="sk-judge",
        JUDGE_BASE_URL="https://api.openai.com/v1",
        JUDGE_MODEL="gpt-4.1-mini",
    )
    assert done.returncode == 0, done.stderr
    assert _env(log)["OPSEVAL_JUDGE_API_KEY"] == "sk-judge"


def _azure(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    return _run(tmp_path, _azure_step()["bash"], MODE="docker", IMAGE="opseval", **env)


@needs_bash
def test_azure_reads_key_vault_names(tmp_path: Path) -> None:
    """Key Vault secret names cannot hold "_": OPSEVAL-JUDGE-API-KEY is the judge key."""
    done, _, log = _azure(
        tmp_path,
        OPSEVAL_JUDGE_API_KEY="$(OPSEVAL_JUDGE_API_KEY)",
        OE_KV_OPSEVAL_JUDGE_API_KEY="sk-from-key-vault",
        OPSEVAL_STATIC_TOKEN="$(OPSEVAL_STATIC_TOKEN)",
        OE_KV_OPSEVAL_STATIC_TOKEN="$(OPSEVAL-STATIC-TOKEN)",
        SECRET_ENV_NAMES="OPSEVAL_LANGFUSE_SECRET_KEY",
        OPSEVAL_LANGFUSE_SECRET_KEY="$(OPSEVAL_LANGFUSE_SECRET_KEY)",
        OE_KV_OPSEVAL_LANGFUSE_SECRET_KEY="lf-from-key-vault",
        JUDGE_BASE_URL="https://api.openai.com/v1",
        JUDGE_MODEL="gpt-4.1-mini",
    )
    assert done.returncode == 0, done.stderr
    env = _env(log)
    assert env["OPSEVAL_JUDGE_API_KEY"] == "sk-from-key-vault"
    assert env["OPSEVAL_LANGFUSE_SECRET_KEY"] == "lf-from-key-vault"
    assert "OPSEVAL_STATIC_TOKEN" not in env
    assert not [k for k in env if k.startswith("OE_KV_")]
    argv = (log / "docker").read_text(encoding="utf-8")
    assert "from-key-vault" not in argv


@needs_bash
def test_azure_refuses_two_different_values_for_one_key(tmp_path: Path) -> None:
    done, _, _ = _azure(tmp_path, OPSEVAL_STATIC_TOKEN="one", OE_KV_OPSEVAL_STATIC_TOKEN="two")
    assert done.returncode == 3
    assert "both set, to different values" in done.stderr


@needs_bash
@pytest.mark.parametrize("role", ["SIMULATOR", "JUDGE"])
def test_azure_an_undefined_key_with_its_endpoint_set_is_3(role: str, tmp_path: Path) -> None:
    """Azure passes an undefined secret through as its macro text; never drop it silently."""
    endpoint = {f"{role}_BASE_URL": "https://api.openai.com/v1", f"{role}_MODEL": "gpt-4.1-mini"}
    key = f"OPSEVAL_{role}_API_KEY"
    literal = {key: f"$({key})", f"OE_KV_{key}": f"$({key.replace('_', '-')})"}
    done, _, log = _azure(tmp_path, **endpoint, **literal)
    assert done.returncode == 3
    assert f"{key} is not defined" in done.stderr and key.replace("_", "-") in done.stderr
    assert not (log / "docker").exists()
    # An endpoint that takes no key says so.
    done, _, _ = _azure(tmp_path, **endpoint, **literal, LLM_WITHOUT_KEY="true")
    assert done.returncode == 0, done.stderr


def test_azure_maps_each_named_secret_from_both_spellings() -> None:
    env = _azure_step()["env"]
    (each,) = (k for k in env if k.startswith("${{ each name in parameters.secretEnvNames"))
    assert env[each] == {
        "${{ name }}": "$(${{ name }})",
        "OE_KV_${{ name }}": "$(${{ replace(name, '_', '-') }})",
    }
    assert env["SECRET_ENV_NAMES"] == "${{ join(' ', parameters.secretEnvNames) }}"
    for name in SECRETS:
        assert env[f"OE_KV_{name}"] == f"$({name.replace('_', '-')})"


@needs_bash
def test_azure_treats_an_undefined_secret_as_no_key(tmp_path: Path) -> None:
    """An undefined secret variable reaches the step as its own macro text."""
    done, _, log = _run(
        tmp_path,
        _azure_step()["bash"],
        MODE="docker",
        IMAGE="opseval",
        OPSEVAL_JUDGE_API_KEY="$(OPSEVAL_JUDGE_API_KEY)",
        OPSEVAL_STATIC_TOKEN="real-token",
    )
    assert done.returncode == 0, done.stderr  # no "key would be unused"
    lines = (log / "docker").read_text(encoding="utf-8").splitlines()
    assert "OPSEVAL_JUDGE_API_KEY" not in lines
    assert "OPSEVAL_STATIC_TOKEN" in lines


@needs_bash
def test_circleci_reads_the_keys_from_the_named_variables(tmp_path: Path) -> None:
    done, _, log = _run(
        tmp_path,
        _orb_step()["command"],
        MODE="local",
        OE_SIMULATOR_API_KEY_VAR="OPSEVAL_SIMULATOR_API_KEY",
        OE_JUDGE_API_KEY_VAR="TEAM_JUDGE_KEY",
        OE_AGENT_TOKEN_VAR="OPSEVAL_STATIC_TOKEN",
        TEAM_JUDGE_KEY="sk-team",
        JUDGE_BASE_URL="https://api.openai.com/v1",
        JUDGE_MODEL="gpt-4.1-mini",
    )
    assert done.returncode == 0, done.stderr
    assert _env(log)["OPSEVAL_JUDGE_API_KEY"] == "sk-team"


# --- each system ------------------------------------------------------------------------------


def test_gitlab_inputs_mirror_the_action() -> None:
    spec, _ = _gitlab()
    inputs = set(spec["spec"]["inputs"])
    assert inputs - {"image", "stage", "job-name", "artifact-name"} == _expected_inputs()
    assert spec["spec"]["inputs"]["image"]["default"] == PLACEHOLDER


def test_gitlab_job_fails_on_every_nonzero_code_and_publishes_junit() -> None:
    job = _gitlab_job()
    assert "allow_failure" not in job
    assert job["image"] == {"name": "$[[ inputs.image ]]", "entrypoint": [""]}
    assert job["artifacts"]["when"] == "always"
    assert job["artifacts"]["reports"]["junit"] == "opseval-reports/junit.xml"
    assert job["variables"]["REPORTS_DIR"] == "opseval-reports"
    assert job["variables"]["MODE"] == "local"


def test_gitlab_inputs_reach_the_script_only_as_variables() -> None:
    spec, _ = _gitlab()
    job = _gitlab_job()
    assert "$[[" not in job["script"][0]
    used = set(re.findall(r"\$\[\[ inputs\.([a-z-]+) \]\]", GITLAB.read_text(encoding="utf-8")))
    assert used == set(spec["spec"]["inputs"])  # every input is used


def test_gitlab_never_expands_an_input() -> None:
    """A "$" in an input must stay text, not read another CI/CD variable."""
    variables = _gitlab_job()["variables"]
    from_inputs = {k: v for k, v in variables.items() if "inputs." in str(v)}
    assert len(from_inputs) == len(_gitlab()[0]["spec"]["inputs"]) - 4  # not stage, name, ...
    for name, value in from_inputs.items():
        assert isinstance(value, dict) and value["expand"] is False, name
        assert re.fullmatch(r"\$\[\[ inputs\.[a-z-]+ \]\]", value["value"])


def test_jenkins_parameters_mirror_the_action() -> None:
    text = JENKINS.read_text(encoding="utf-8")
    known = text[text.index("def known = [") : text.index("]", text.index("def known = ["))]
    names = {_kebab(n) for n in re.findall(r"'([A-Za-z]+)'", known)}
    extra = {"image", "reports-dir", "docker-workspace-host-path"} | {
        f"{s}-credentials-id"
        for s in (
            "simulator-api-key",
            "judge-api-key",
            "agent-token",
            "opseval-public-key",
            "opseval-secret-key",
        )
    }
    # secretCredentials maps each other secret setting's name to a credential id.
    names = {"secret-env-names" if n == "secret-credentials" else n for n in names}
    assert names - extra == _expected_inputs()


def test_jenkins_fails_the_build_on_1_2_and_3_with_distinct_messages() -> None:
    text = JENKINS.read_text(encoding="utf-8")
    assert "sh(returnStatus: true, script: 'bash \"$OE_SCRIPT\"')" in text
    assert "skipMarkingBuildUnstable: true" in text
    assert "allowEmptyResults: true" in text
    switch = text[text.index("switch (code)") :]
    messages = re.findall(r"case (\d):\s+(\S.*)", switch)
    assert [c for c, _ in messages] == ["0", "1", "2", "3"]
    assert messages[0][1] == "return 0"
    for _, call in messages[1:]:
        assert call.startswith("error(")
    assert "the agent failed" in switch and "not judged" in switch and "harness" in switch
    assert "default:" in switch and "error(" in switch.split("default:")[1]


def test_jenkins_secrets_come_only_from_credentials() -> None:
    text = JENKINS.read_text(encoding="utf-8")
    for name in SECRETS + SERVER_KEYS:
        assert f"variable: '{name}'" in text
    assert "withCredentials(secrets)" in text
    # The sh call is a constant: no Groovy interpolation of anything into the shell.
    for call in re.findall(r"sh\((.*)\)", text):
        assert '"' not in call.split("script:")[1].strip()[0]
    assert text.count("{") == text.count("}")
    assert text.count("(") == text.count(")")


def test_jenkins_ships_the_script_as_a_library_resource() -> None:
    assert "libraryResource('opseval/opseval-ci.sh')" in JENKINS.read_text(encoding="utf-8")
    assert '"MODE=docker"' in JENKINS.read_text(encoding="utf-8")


def test_the_jenkinsfile_uses_the_step_with_a_credential() -> None:
    text = JENKINSFILE.read_text(encoding="utf-8")
    assert "@Library('opseval') _" in text
    assert "opseval(" in text and "judgeApiKeyCredentialsId:" in text
    assert "sk-" not in text


def test_azure_parameters_mirror_the_action() -> None:
    params = {p["name"] for p in _yaml(AZURE)["parameters"]}
    extra = {"image", "artifact-name", "llm-without-key", "docker-workspace-host-path"}
    assert {_kebab(p) for p in params} - extra == _expected_inputs()
    default = {p["name"]: p.get("default") for p in _yaml(AZURE)["parameters"]}
    assert default["image"] == PLACEHOLDER


def test_azure_maps_exactly_the_secret_variables_and_no_secret_parameter() -> None:
    step = _azure_step()
    for name in SECRETS + SERVER_KEYS:
        assert step["env"][name] == f"$({name})"
        assert step["env"][f"OE_KV_{name}"] == f"$({name.replace('_', '-')})"
    for param in _yaml(AZURE)["parameters"]:
        if param["name"] in ("secretEnvNames", "llmWithoutKey"):  # names and a boolean
            assert param["type"] in ("object", "boolean")
            continue
        assert not re.search(r"key|token|secret|password", param["name"], re.IGNORECASE)
    # Parameters reach the script only through env, never pasted into it.
    for s in _yaml(AZURE)["steps"]:
        assert "${{" not in s.get("bash", "")
    assert step["env"]["MODE"] == "docker"


def test_azure_publishes_results_whatever_happened_and_never_fails_on_test_count() -> None:
    steps = _yaml(AZURE)["steps"]
    publish = next(s for s in steps if s.get("task") == "PublishTestResults@2")
    assert publish["condition"] == "succeededOrFailed()"
    assert publish["inputs"]["testResultsFormat"] == "JUnit"
    assert publish["inputs"]["testResultsFiles"] == "opseval-reports/junit.xml"
    assert publish["inputs"]["failTaskOnFailedTests"] is False
    for s in steps[1:]:
        assert s["condition"] == "succeededOrFailed()"
    assert any("task.uploadsummary" in s.get("bash", "") for s in steps)
    for s in steps:
        assert "continueOnError" not in s


def test_the_azure_example_uses_the_template_and_a_variable_group() -> None:
    text = AZURE_EXAMPLE.read_text(encoding="utf-8")
    assert "template: templates/azure/opseval.yml@opseval" in text
    assert "group: opseval-secrets" in text


def test_circleci_parameters_mirror_the_action() -> None:
    orb = _yaml(ORB)
    job = set(orb["jobs"]["run"]["parameters"])
    command = set(orb["commands"]["evaluate"]["parameters"])
    secrets = {
        "simulator_api_key",
        "judge_api_key",
        "agent_token",
        "opseval_public_key",
        "opseval_secret_key",
    }
    assert {_kebab(p) for p in job - secrets} - {"image"} == _expected_inputs()
    assert command == job - {"image"}
    assert orb["executors"]["default"]["parameters"]["image"]["default"] == PLACEHOLDER


def test_circleci_leaves_the_primary_containers_entrypoint_ignored() -> None:
    """With neither key, CircleCI ignores the image's ENTRYPOINT (opseval) for the primary
    container; setting one would make CircleCI run it."""
    (container,) = _yaml(ORB)["executors"]["default"]["docker"]
    assert "entrypoint" not in container and "command" not in container


def test_circleci_secrets_are_variable_names_never_values() -> None:
    params = _yaml(ORB)["commands"]["evaluate"]["parameters"]
    for name in (
        "simulator_api_key",
        "judge_api_key",
        "agent_token",
        "opseval_public_key",
        "opseval_secret_key",
    ):
        assert params[name]["type"] == "env_var_name"
    assert "<<" not in _orb_step()["command"]  # parameters reach it only through environment


def test_circleci_stores_results_and_the_run() -> None:
    steps = _yaml(ORB)["commands"]["evaluate"]["steps"]
    assert {"store_test_results": {"path": "opseval-reports/junit.xml"}} in steps
    assert any("store_artifacts" in s for s in steps)
    assert _orb_step()["environment"]["MODE"] == "local"


def test_the_bitbucket_pipe_variables_mirror_the_action() -> None:
    variables = {v["name"] for v in _yaml(PIPE_YML)["variables"]}
    secrets = {"SIMULATOR_API_KEY", "JUDGE_API_KEY", "AGENT_TOKEN", "PUBLIC_KEY", "SECRET_KEY"}
    assert {_kebab(v) for v in variables - secrets} == _expected_inputs() - {"image"}
    for v in _yaml(PIPE_YML)["variables"]:
        assert v.get("secret", False) == (v["name"] in secrets)


def test_the_bitbucket_pipe_starts_as_root_only_to_drop_it() -> None:
    """The clone is root's, so the pipe starts as root; setpriv must be there to drop it."""
    text = PIPE_DOCKERFILE.read_text(encoding="utf-8")
    assert f"ARG OPSEVAL_IMAGE={PLACEHOLDER}" in text
    assert "FROM ${OPSEVAL_IMAGE}" in text
    code = _code_lines(PIPE_DOCKERFILE)
    assert [ln for ln in code if ln.startswith("USER")] == ["USER root"]
    assert "RUN setpriv --version" in code
    for path in (PIPE_DOCKERFILE, PIPE, SHARED):
        assert not [ln for ln in _code_lines(path) if "777" in ln]
    assert 'ENTRYPOINT ["/usr/local/bin/opseval-pipe"]' in text
    assert os.access(PIPE, os.X_OK)


def test_the_bitbucket_example_passes_secrets_from_secured_variables() -> None:
    step = _yaml(BITBUCKET_EXAMPLE)["definitions"]["steps"][0]["step"]
    (pipe,) = step["script"]
    assert pipe["variables"]["JUDGE_API_KEY"] == "$OPSEVAL_JUDGE_API_KEY"
    assert "opseval-reports/**" in step["artifacts"]


# --- docs -------------------------------------------------------------------------------------


def test_the_guide_has_a_section_per_system() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    for heading in (
        "## GitLab",
        "## Jenkins",
        "## Azure DevOps",
        "## CircleCI",
        "## Bitbucket",
        "## Any other CI: `docker run`",
    ):
        assert heading in guide
    for path in (GITLAB, JENKINS, AZURE, ORB, PIPE):
        assert _ids(path) in guide
    assert "PowerShell" in guide and "```powershell" in guide


# --- reporting to a server (ADR-0058 E2) ------------------------------------------------------


@needs_bash
@pytest.mark.parametrize("mode", ["local", "docker"])
def test_the_script_reports_to_a_server_and_passes_the_key_by_name(
    mode: str, tmp_path: Path
) -> None:
    done, _, log = _run(
        tmp_path,
        MODE=mode,
        IMAGE="opseval",
        PROFILE="",
        SUITE="suites/smoke.yaml",
        SERVER_URL="https://opseval.example.com",
        PROJECT="acme/bot",
        TASK="voice",
        OPSEVAL_PUBLIC_KEY="pk-oe-1",
        OPSEVAL_SECRET_KEY="sk-oe-secret-value",
    )
    assert done.returncode == 0, done.stderr
    args = _args(log)
    assert "--profile" not in args
    at = args.index("--server")
    assert args[at : at + 8] == [
        "--server", "https://opseval.example.com", "--project", "acme/bot",
        "--task", "voice", "--label", "production",
    ]  # fmt: skip
    env = _env(log)
    assert env["OPSEVAL_SECRET_KEY"] == "sk-oe-secret-value"
    assert env["OPSEVAL_PUBLIC_KEY"] == "pk-oe-1"
    if mode == "docker":
        argv = (log / "docker").read_text(encoding="utf-8")
        assert "sk-oe-secret-value" not in argv and "OPSEVAL_SECRET_KEY" in argv.splitlines()
        assert "sk-oe-secret-value" not in (log / "envfile").read_text(encoding="utf-8")


@needs_bash
@pytest.mark.parametrize(
    ("env", "said"),
    [
        ({"SERVER_URL": "https://opseval.example.com"}, "SERVER_URL needs PROJECT"),
        ({"PROJECT": "acme/bot"}, "PROJECT needs SERVER_URL"),
        ({"SERVER_URL": "https://a\nOPSEVAL_X=1", "PROJECT": "p"}, "more than one line"),
        ({"PROFILE": ""}, "PROFILE is required"),
        ({"OPSEVAL_ENV": "OPSEVAL_SECRET_KEY=x"}, "a secret"),
        ({"OPSEVAL_ENV": "OPSEVAL_PUBLIC_KEY=x"}, "its own input"),
        ({"SECRET_ENV_NAMES": "OPSEVAL_SECRET_KEY"}, "its own input"),
    ],
)
def test_server_settings_are_checked(env: dict[str, str], said: str, tmp_path: Path) -> None:
    done, _, log = _run(tmp_path, **env)
    assert done.returncode == 3 and said in done.stderr, done.stderr
    assert not (log / "args").exists()


@needs_bash
def test_with_a_server_the_suite_is_inside_the_pulled_catalogue(tmp_path: Path) -> None:
    """In docker mode the suite is not a workspace path: Opseval pulls it in the container."""
    done, _, _ = _run(
        tmp_path,
        MODE="docker",
        IMAGE="opseval",
        PROFILE="",
        SUITE="suites/smoke.yaml",
        SERVER_URL="https://opseval.example.com",
        PROJECT="acme/bot",
    )
    assert done.returncode == 0, done.stderr


def test_every_template_offers_the_server_inputs() -> None:
    spec, _ = _gitlab()
    assert {"server-url", "project", "task", "label"} <= set(spec["spec"]["inputs"])
    assert spec["spec"]["inputs"]["profile"]["default"] == ""
    variables = _gitlab_job()["variables"]
    assert variables["SERVER_URL"] == {"value": "$[[ inputs.server-url ]]", "expand": False}
    azure = {p["name"]: p for p in _yaml(AZURE)["parameters"]}
    assert azure["label"]["default"] == "production" and azure["task"]["default"] == "default"
    assert _azure_step()["env"]["SERVER_URL"] == "${{ parameters.serverUrl }}"
    orb = _orb_step()
    assert orb["environment"]["SERVER_URL"] == "<< parameters.server_url >>"
    assert orb["environment"]["OE_SECRET_KEY_VAR"] == "<< parameters.opseval_secret_key >>"
    assert "\"SERVER_URL=${setting(config, 'serverUrl')}\"" in JENKINS.read_text("utf-8")
    pipe = {v["name"]: v for v in _yaml(PIPE_YML)["variables"]}
    assert pipe["SECRET_KEY"]["secret"] is True and pipe["SERVER_URL"]["default"] == ""


@needs_bash
def test_circleci_reads_the_server_key_from_the_named_variables(tmp_path: Path) -> None:
    done, _, log = _run(
        tmp_path,
        _orb_step()["command"],
        MODE="local",
        OE_SIMULATOR_API_KEY_VAR="OPSEVAL_SIMULATOR_API_KEY",
        OE_JUDGE_API_KEY_VAR="OPSEVAL_JUDGE_API_KEY",
        OE_AGENT_TOKEN_VAR="OPSEVAL_STATIC_TOKEN",
        OE_PUBLIC_KEY_VAR="TEAM_PK",
        OE_SECRET_KEY_VAR="TEAM_SK",
        TEAM_PK="pk-team",
        TEAM_SK="sk-team",
        SERVER_URL="https://opseval.example.com",
        PROJECT="acme/bot",
    )
    assert done.returncode == 0, done.stderr
    env = _env(log)
    assert env["OPSEVAL_PUBLIC_KEY"] == "pk-team" and env["OPSEVAL_SECRET_KEY"] == "sk-team"


@needs_bash
def test_the_bitbucket_pipe_maps_the_server_key(tmp_path: Path) -> None:
    done, _, log = _run(
        tmp_path,
        PIPE,
        PUBLIC_KEY="pk-pipe",
        SECRET_KEY="sk-pipe",
        SERVER_URL="https://opseval.example.com",
        PROJECT="acme/bot",
    )
    assert done.returncode == 0, done.stderr
    env = _env(log)
    assert env["OPSEVAL_PUBLIC_KEY"] == "pk-pipe" and env["OPSEVAL_SECRET_KEY"] == "sk-pipe"


@needs_bash
def test_azure_reads_the_server_key_from_either_spelling(tmp_path: Path) -> None:
    done, _, log = _run(
        tmp_path,
        _azure_step()["bash"],
        MODE="local",
        OPSEVAL_PUBLIC_KEY="$(OPSEVAL_PUBLIC_KEY)",  # undefined: its own macro text
        OE_KV_OPSEVAL_PUBLIC_KEY="pk-kv",
        OPSEVAL_SECRET_KEY="sk-var",
        OE_KV_OPSEVAL_SECRET_KEY="$(OPSEVAL-SECRET-KEY)",
        SERVER_URL="https://opseval.example.com",
        PROJECT="acme/bot",
    )
    assert done.returncode == 0, done.stderr
    env = _env(log)
    assert env["OPSEVAL_PUBLIC_KEY"] == "pk-kv" and env["OPSEVAL_SECRET_KEY"] == "sk-var"
