# SPDX-License-Identifier: Apache-2.0
"""Protecting `main` (ADR-0065): the DCO check, the pre-push hook and the committed ruleset."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
HOOK = SCRIPTS / "githooks" / "pre-push"
APPLY = SCRIPTS / "apply_rulesets.sh"
RULESET = ROOT / ".github" / "rulesets" / "main.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
EXPECTED_CHECKS = [
    "check (3.11)",
    "check (3.12)",
    "check (3.13)",
    "console",
    "container",
    "audit",
    "action",
    "dco",
]
DEPENDABOT = "49699333+dependabot[bot]@users.noreply.github.com"

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered first: a dataclass looks its module up while the module is executing.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def dco() -> ModuleType:
    return _load("check_dco")


@pytest.fixture(scope="module")
def rules() -> ModuleType:
    return _load("check_rulesets")


# --- a scratch git repository --------------------------------------------------------------


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.git("init", "-q", "-b", "work")

    def git(self, *args: str, env: dict[str, str] | None = None) -> str:
        out = subprocess.run(
            ["git", *args],
            cwd=self.path,
            env={**os.environ, **(env or {})},
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()

    def commit(
        self,
        subject: str,
        *,
        author: tuple[str, str] = ("Ada Lovelace", "ada@example.com"),
        committer: tuple[str, str] | None = None,
        trailers: str = "",
        body: str = "",
        file: str = "f.txt",
    ) -> str:
        name, email = author
        cname, cemail = committer or author
        (self.path / file).write_text(subject, "utf-8")
        self.git("add", file)
        message = (
            subject + (f"\n\n{body}" if body else "") + (f"\n\n{trailers}" if trailers else "")
        )
        self.git(
            "commit",
            "-q",
            "-m",
            message,
            env={
                "GIT_AUTHOR_NAME": name,
                "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": cname,
                "GIT_COMMITTER_EMAIL": cemail,
            },
        )
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repo:
    # The developer's own git configuration (signing, hooks, templates) must not leak in.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Base")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "base@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Base")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "base@example.com")
    (tmp_path / "repo").mkdir()
    return Repo(tmp_path / "repo")


def _run_dco(dco: ModuleType, repo: Repo, base: str, head: str = "HEAD") -> int:
    result: int = dco.main([base, head], cwd=str(repo.path))
    return result


SIGNED = "Signed-off-by: Ada Lovelace <ada@example.com>"


# --- the DCO check -------------------------------------------------------------------------


def test_only_the_pull_requests_commits_are_checked(dco: ModuleType, repo: Repo) -> None:
    """History before the range has no sign-off (as on main today); it is never read."""
    base = repo.commit("old, unsigned")
    repo.commit("feat: one", trailers=SIGNED)
    repo.commit("fix: two", trailers=f"Co-Authored-By: Someone <s@example.com>\n{SIGNED}")
    assert _run_dco(dco, repo, base) == 0


def test_a_commit_without_a_sign_off_fails(
    dco: ModuleType, repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    base = repo.commit("base")
    repo.commit("feat: signed", trailers=SIGNED)
    repo.commit("feat: forgot")
    assert _run_dco(dco, repo, base) == 1
    err = capsys.readouterr().err
    assert "1 of 2 commits" in err and "'feat: forgot': no Signed-off-by" in err
    assert "git commit -s" in err


def test_a_sign_off_by_someone_else_fails(dco: ModuleType, repo: Repo) -> None:
    base = repo.commit("base")
    repo.commit("feat: x", trailers="Signed-off-by: Bob <bob@example.com>")
    assert _run_dco(dco, repo, base) == 1


def test_the_email_decides_not_the_name_or_its_case(dco: ModuleType, repo: Repo) -> None:
    base = repo.commit("base")
    repo.commit("feat: x", trailers="Signed-off-by: A. Lovelace <ADA@Example.com>")
    assert _run_dco(dco, repo, base) == 0


def test_a_sign_off_in_the_body_is_not_a_trailer(dco: ModuleType, repo: Repo) -> None:
    base = repo.commit("base")
    repo.commit("feat: x", body=f"{SIGNED}\n\nmore text after it")
    assert _run_dco(dco, repo, base) == 1


def test_dependabot_signs_off_with_its_own_address(dco: ModuleType, repo: Repo) -> None:
    base = repo.commit("base")
    bot = ("dependabot[bot]", DEPENDABOT)
    github = ("GitHub", "noreply@github.com")
    repo.commit(
        "chore(deps): bump x",
        author=bot,
        committer=github,
        trailers="Signed-off-by: dependabot[bot] <support@github.com>",
    )
    assert _run_dco(dco, repo, base) == 0
    repo.commit("chore(deps): bump y", author=bot, committer=github)
    assert _run_dco(dco, repo, base) == 1, "the bot is not exempt, only its sign-off address"


def _side(repo: Repo) -> None:
    """A branch ``side`` off ``HEAD~1`` that adds g.txt, while ``work`` changed f.txt."""
    repo.git("switch", "-q", "-C", "side", "HEAD~1")
    (repo.path / "g.txt").write_text("side", "utf-8")
    repo.git("add", "g.txt")
    repo.git(
        "commit", "-q", "-m", "side\n\n" + SIGNED, env=_ident(("Ada Lovelace", "ada@example.com"))
    )
    repo.git("switch", "-q", "work")


def _merge(
    repo: Repo,
    committer: tuple[str, str],
    trailers: str = "",
    change: Callable[[Path], None] | None = None,
) -> None:
    """Merge ``side`` into ``work``; ``change`` edits the result before it is committed."""
    _side(repo)
    env = {**_ident(("Ada Lovelace", "ada@example.com")), **_ident(committer, "COMMITTER")}
    message = "Merge branch 'side'" + (f"\n\n{trailers}" if trailers else "")
    if change is None:
        repo.git("merge", "-q", "--no-ff", "-m", message, "side", env=env)
        return
    repo.git("merge", "-q", "--no-ff", "--no-commit", "side", env=env)
    change(repo.path)
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", message, env=env)


def _ident(who: tuple[str, str], role: str = "AUTHOR") -> dict[str, str]:
    env = {f"GIT_{role}_NAME": who[0], f"GIT_{role}_EMAIL": who[1]}
    if role == "AUTHOR":
        env |= {"GIT_COMMITTER_NAME": who[0], "GIT_COMMITTER_EMAIL": who[1]}
    return env


GITHUB = ("GitHub", "noreply@github.com")


@pytest.mark.parametrize("committer", [GITHUB, ("Ada Lovelace", "ada@example.com")])
def test_a_merge_with_no_changes_of_its_own_is_skipped_whoever_made_it(
    dco: ModuleType, repo: Repo, capsys: pytest.CaptureFixture[str], committer: tuple[str, str]
) -> None:
    base = repo.commit("base")
    repo.commit("feat: x", trailers=SIGNED)
    _merge(repo, committer)
    assert _run_dco(dco, repo, base) == 0
    assert "1 merge commit(s) with no changes of their own skipped" in capsys.readouterr().out


def _extra_file(path: Path) -> None:
    (path / "evil.txt").write_text("not in either parent", "utf-8")


def _drop_sides_change(path: Path) -> None:
    # Takes work's side of everything: the combined diff is empty, but side's g.txt is gone.
    (path / "g.txt").unlink()


@pytest.mark.parametrize("change", [_extra_file, _drop_sides_change])
def test_a_merge_with_changes_of_its_own_must_be_signed_off(
    dco: ModuleType,
    repo: Repo,
    capsys: pytest.CaptureFixture[str],
    change: Callable[[Path], None],
) -> None:
    """A committer address is trivially spoofed; the merge's own content decides."""
    base = repo.commit("base")
    repo.commit("feat: x", trailers=SIGNED)
    _merge(repo, GITHUB, change=change)
    assert _run_dco(dco, repo, base) == 1
    assert "a merge with changes of its own" in capsys.readouterr().err
    repo.git("reset", "-q", "--hard", "HEAD~1")
    _merge(repo, GITHUB, trailers=SIGNED, change=change)
    assert _run_dco(dco, repo, base) == 0


def test_a_conflict_resolution_must_be_signed_off(dco: ModuleType, repo: Repo) -> None:
    base = repo.commit("base")
    repo.git("switch", "-q", "-c", "side")
    repo.commit("side edit", trailers=SIGNED)
    repo.git("switch", "-q", "work")
    repo.commit("work edit", trailers=SIGNED)
    with pytest.raises(subprocess.CalledProcessError):
        repo.git("merge", "-q", "side")
    (repo.path / "f.txt").write_text("resolved", "utf-8")
    repo.git("add", "f.txt")
    repo.git("commit", "-q", "-m", "Merge branch 'side'", env=_ident(GITHUB, "COMMITTER"))
    assert _run_dco(dco, repo, base) == 1


def test_an_octopus_merge_is_always_checked(dco: ModuleType, repo: Repo) -> None:
    base = repo.commit("base")
    for name in ("a", "b"):
        repo.git("switch", "-q", "-c", name, base)
        (repo.path / f"{name}.txt").write_text(name, "utf-8")
        repo.git("add", f"{name}.txt")
        repo.git(
            "commit", "-q", "-m", f"{name}\n\n{SIGNED}", env=_ident(("Ada", "ada@example.com"))
        )
    repo.git("switch", "-q", "work")
    repo.git("merge", "-q", "--no-ff", "-m", "octopus", "a", "b", env=_ident(GITHUB, "COMMITTER"))
    assert _run_dco(dco, repo, base) == 1


def test_git_configuration_cannot_change_what_is_parsed(
    dco: ModuleType, repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``log.showSignature`` prints verification lines into ``git log``'s output."""
    base = repo.commit("base")
    repo.git("config", "log.showSignature", "true")
    repo.git("config", "color.ui", "always")
    keygen = shutil.which("ssh-keygen")
    if keygen is not None:  # an SSH-signed commit: git log then prints "No signature" lines
        key = tmp_path / "key"
        subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        repo.git("config", "gpg.format", "ssh")
        repo.git("config", "user.signingkey", str(key))
        repo.git("config", "commit.gpgSign", "true")
    repo.commit("feat: x", trailers=SIGNED)
    calls: list[list[str]] = []
    real = subprocess.run

    def spy(args: list[str], **kwargs: Any) -> Any:
        calls.append(list(args))
        return real(args, **kwargs)

    monkeypatch.setattr(dco.subprocess, "run", spy)
    assert _run_dco(dco, repo, base) == 0
    (log,) = [c for c in calls if "log" in c]
    assert log[1:5] == ["-c", "log.showSignature=false", "-c", "color.ui=never"]
    assert "--no-color" in log and "-z" in log
    assert any(a.startswith("--format=%H") for a in log)


@pytest.mark.parametrize(
    "subject",
    [
        "feat: \x1f a separator",
        # A forged sign-off: the subject tries to end its own record with one.
        "feat: x\x1fSigned-off-by: Ada Lovelace <ada@example.com>",
    ],
)
def test_a_control_character_in_a_record_is_an_unreadable_range(
    dco: ModuleType, repo: Repo, capsys: pytest.CaptureFixture[str], subject: str
) -> None:
    base = repo.commit("base")
    repo.commit(subject)
    assert _run_dco(dco, repo, base) == 2
    assert "unreadable commit record" in capsys.readouterr().err


def test_a_record_separator_in_a_subject_is_only_text(dco: ModuleType, repo: Repo) -> None:
    """Records are NUL-separated, which a commit message cannot hold."""
    base = repo.commit("base")
    repo.commit("feat: x\x1e", trailers=SIGNED)
    assert _run_dco(dco, repo, base) == 0


def test_an_empty_or_unreadable_range_is_not_a_pass(
    dco: ModuleType, repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    repo.commit("base")
    assert _run_dco(dco, repo, "HEAD", "HEAD") == 2
    assert "nothing to check is not a pass" in capsys.readouterr().err
    assert _run_dco(dco, repo, "no-such-ref") == 2
    assert _run_dco(dco, repo, "--all") == 2, "a ref that looks like an option is refused"
    assert dco.main(["only-one"]) == 2
    assert dco.main([], cwd=str(repo.path)) == 2


def test_signoff_email(dco: ModuleType) -> None:
    assert dco.signoff_email("Ada <Ada@Example.com>") == "ada@example.com"
    assert dco.signoff_email("Ada ada@example.com") is None


# --- the CI job ----------------------------------------------------------------------------


def _ci() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(CI.read_text("utf-8"))
    return loaded


def test_the_dco_job_checks_the_pull_request_range_with_no_credentials() -> None:
    jobs = _ci()["jobs"]
    job = jobs["dco"]
    # No job-level `if:`: a skipped job reports a passing required check.
    assert "if" not in job
    assert job["permissions"] == {"contents": "read"}
    on_pr = "github.event_name == 'pull_request'"
    idle, checkout, step = job["steps"]
    assert idle["if"] == "github.event_name != 'pull_request'" and "uses" not in idle
    pinned = jobs["check"]["steps"][0]["uses"]
    assert checkout["uses"] == pinned, "the same SHA-pinned checkout as the other jobs"
    assert checkout["if"] == on_pr
    # The default ref of a pull_request run: the merge ref, whose parents are the range.
    assert checkout["with"] == {"fetch-depth": 0, "persist-credentials": False}
    assert step["if"] == on_pr
    # The SHA goes through env, never interpolated into the shell.
    assert step["env"] == {"PR_HEAD": "${{ github.event.pull_request.head.sha }}"}
    script = step["run"]
    assert "${{" not in script
    assert '[ "$merged" != "$PR_HEAD" ]' in script and "exit 2" in script
    # HEAD^1..HEAD^2, never base.sha..head.sha: main commits merged into the branch after
    # base.sha was recorded would be checked as the pull request's own.
    assert script.rstrip().endswith("python3 scripts/check_dco.py HEAD^1 HEAD^2")


def test_the_merge_ref_range_leaves_out_main_merged_into_the_branch(
    dco: ModuleType, repo: Repo
) -> None:
    """What CI does, with a stale ``base.sha``: main moved on and was merged into the branch."""
    stale_base = repo.commit("main: old, unsigned")
    repo.git("branch", "main")
    repo.commit("feat: mine", trailers=SIGNED)
    repo.git("switch", "-q", "main")
    repo.commit("main: newer, unsigned", file="main.txt")
    repo.git("switch", "-q", "work")
    repo.git("merge", "-q", "--no-ff", "-m", "Merge main", "main", env=_ident(GITHUB, "COMMITTER"))
    head = repo.git("rev-parse", "HEAD")
    assert _run_dco(dco, repo, stale_base, head) == 1, "base.sha..head checks main's commit"
    repo.git("switch", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "-m", "the merge ref", head, env=_ident(GITHUB, "COMMITTER"))
    assert _run_dco(dco, repo, "HEAD^1", "HEAD^2") == 0


def test_every_action_in_ci_is_pinned_to_a_commit() -> None:
    for job_id, job in _ci()["jobs"].items():
        for step in job["steps"]:
            uses = step.get("uses")
            if uses and not uses.startswith("./"):
                ref = uses.rpartition("@")[2]
                assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref), (
                    f"{job_id}: {uses} is not pinned to a SHA"
                )


def test_ci_runs_the_ruleset_dry_run() -> None:
    runs = [s.get("run", "") for s in _ci()["jobs"]["check"]["steps"]]
    assert "bash scripts/apply_rulesets.sh --dry-run" in runs


# --- the ruleset ---------------------------------------------------------------------------


def _ruleset() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(RULESET.read_text("utf-8"))
    return loaded


def _rule(ruleset: dict[str, Any], kind: str) -> dict[str, Any]:
    (found,) = [r for r in ruleset["rules"] if r["type"] == kind]
    return found


def test_the_committed_ruleset_is_valid(rules: ModuleType) -> None:
    assert rules.problems(_ruleset(), _ci()) == []
    assert rules.main([]) == 0


def test_the_ruleset_is_what_adr_0065_decides() -> None:
    ruleset = _ruleset()
    assert ruleset["target"] == "branch" and ruleset["enforcement"] == "active"
    assert ruleset["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]
    assert {r["type"] for r in ruleset["rules"]} == {
        "deletion",
        "non_fast_forward",
        "required_linear_history",
        "pull_request",
        "required_status_checks",
    }
    review = _rule(ruleset, "pull_request")["parameters"]
    assert review["required_approving_review_count"] == 1
    assert review["require_code_owner_review"] is True
    assert review["dismiss_stale_reviews_on_push"] is True
    assert set(review["allowed_merge_methods"]) == {"squash", "rebase"}
    checks = _rule(ruleset, "required_status_checks")["parameters"]["required_status_checks"]
    assert [c["context"] for c in checks] == EXPECTED_CHECKS
    # Reported by GitHub Actions only, so another app cannot post a passing `dco`.
    assert {c["integration_id"] for c in checks} == {15368}
    # The admin bypass while there is one maintainer; removed in a PR once a team exists.
    # In pull_request mode: an admin may merge their own pull request without the review,
    # never push to main directly, force-push or delete it.
    assert ruleset["bypass_actors"] == [
        {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}
    ]


def test_check_names_follow_github(rules: ModuleType) -> None:
    workflow = {
        "jobs": {
            "check": {"strategy": {"matrix": {"python": ["3.11", "3.12"]}}},
            "two": {"strategy": {"matrix": {"os": ["linux"], "flag": [True]}}},
            "named": {"name": "Pretty"},
        }
    }
    assert rules.check_names(workflow) == [
        "check (3.11)",
        "check (3.12)",
        "two (linux, true)",
        "Pretty",
    ]


@pytest.mark.parametrize(
    "job",
    [
        {"strategy": {"matrix": {"python": ["3.11"], "include": [{"python": "3.14"}]}}},
        {"name": "check ${{ matrix.python }}"},
        {"strategy": {"matrix": {"python": []}}},
        {"strategy": {"matrix": {"python": ["3.11"]}}, "if": "github.event_name == 'push'"},
        {"strategy": {"matrix": {"cfg": [{"os": "linux"}]}}},
        {"strategy": {"matrix": {"cfg": [["a", "b"]]}}},
        {"strategy": {"matrix": {"cfg": [None]}}},
    ],
)
def test_a_shape_that_cannot_be_named_fails_closed(rules: ModuleType, job: dict[str, Any]) -> None:
    found = rules.problems(_ruleset(), {"jobs": {**_ci()["jobs"], "check": job}})
    assert any("cannot name the CI checks" in p for p in found)


def test_a_job_level_if_is_refused(rules: ModuleType) -> None:
    with pytest.raises(rules.Unsupported, match="skipped job reports a passing check"):
        rules.check_names({"jobs": {"dco": {"if": "github.event_name == 'pull_request'"}}})


def _checks(ruleset: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = _rule(ruleset, "required_status_checks")["parameters"][
        "required_status_checks"
    ]
    return found


@pytest.mark.parametrize("integration", [None, 1, "15368", True])
def test_every_required_check_comes_from_github_actions(
    rules: ModuleType, integration: Any
) -> None:
    ruleset = _ruleset()
    check = _checks(ruleset)[-1]
    if integration is None:
        del check["integration_id"]
    else:
        check["integration_id"] = integration
    found = rules.problems(ruleset, _ci())
    assert found == [
        "required check `dco` must have `integration_id` 15368 (GitHub Actions), "
        "or any app could report it"
    ]


def test_the_bypass_is_for_pull_requests_only(rules: ModuleType) -> None:
    ruleset = _ruleset()
    ruleset["bypass_actors"][0]["bypass_mode"] = "always"
    found = rules.problems(ruleset, _ci(), {"@ayanasser"})
    assert any("`bypass_mode` must be `pull_request`" in p for p in found)
    assert any("single owner" in p for p in found), "an `always` bypass does not count"


def test_a_sole_code_owner_needs_the_pull_request_bypass(rules: ModuleType) -> None:
    ruleset = _ruleset()
    ruleset["bypass_actors"] = []
    for owners in ({"@ayanasser"}, None, set()):
        (found,) = rules.problems(ruleset, _ci(), owners)
        assert "cannot approve their own pull request" in found
    # A team, or several people, can review each other's pull requests.
    assert rules.problems(ruleset, _ci(), {"@opsedia-ai/maintainers"}) == []
    assert rules.problems(ruleset, _ci(), {"@a", "@b"}) == []
    review = _rule(ruleset, "pull_request")["parameters"]
    review["require_code_owner_review"] = False
    found = rules.problems(ruleset, _ci(), {"@ayanasser"})
    assert found == ["`pull_request.require_code_owner_review` must be true"]


def test_code_owners_are_read_from_codeowners(rules: ModuleType) -> None:
    text = "# a comment\n*  @Ayanasser  # trailing\n/ee/ @org/Team ops@example.com\n\n"
    assert rules.code_owners(text) == {"@ayanasser", "@org/team", "ops@example.com"}
    owners = rules.code_owners((ROOT / ".github" / "CODEOWNERS").read_text("utf-8"))
    assert owners == {"@ayanasser"}


def test_a_renamed_or_added_job_is_a_mismatch(rules: ModuleType) -> None:
    ci = _ci()
    ci["jobs"]["docs"] = ci["jobs"].pop("console")
    found = rules.problems(_ruleset(), ci)
    assert "required check `console` is not reported by any CI job (renamed?)" in found
    assert "CI check `docs` is not required by the ruleset" in found


def test_a_ruleset_without_dco_or_with_a_loose_review_fails(rules: ModuleType) -> None:
    ci = _ci()
    del ci["jobs"]["dco"]
    ruleset = _ruleset()
    checks = _rule(ruleset, "required_status_checks")["parameters"]["required_status_checks"]
    checks[:] = [c for c in checks if c["context"] != "dco"]
    assert "CI has no `dco` job (ADR-0065)" in rules.problems(ruleset, ci)

    loose = copy.deepcopy(_ruleset())
    review = _rule(loose, "pull_request")["parameters"]
    review |= {"required_approving_review_count": 0, "require_code_owner_review": False}
    review["allowed_merge_methods"] = ["merge"]
    loose["enforcement"] = "evaluate"
    loose["conditions"]["ref_name"]["include"] = ["refs/heads/dev"]
    loose["rules"] = [r for r in loose["rules"] if r["type"] != "non_fast_forward"]
    loose["rules"].append({"type": "deletion"})
    loose["name"] = 'main"; rm'
    found = rules.problems(loose, _ci())
    for expected in (
        "`pull_request` needs at least one approving review",
        "`pull_request.require_code_owner_review` must be true",
        "`allowed_merge_methods` cannot include `merge` with linear history",
        "`enforcement` must be `active`",
        "`conditions.ref_name.include` must contain `~DEFAULT_BRANCH`",
        "rule `non_fast_forward` is missing",
        "rule `deletion` appears more than once",
    ):
        assert expected in found
    assert any(p.startswith("`name` must be") for p in found)
    assert rules.problems([], _ci()) == ["the ruleset is not a JSON object"]


def test_an_unreadable_ruleset_fails(rules: ModuleType, tmp_path: Path) -> None:
    bad = tmp_path / "main.json"
    bad.write_text("{not json", "utf-8")
    assert rules.main([str(bad), str(CI)]) == 1
    assert rules.main([str(tmp_path / "missing.json"), str(CI)]) == 1


# --- apply_rulesets.sh, against a fake `gh` ------------------------------------------------


def _fake_gh(tmp_path: Path, ids: str) -> tuple[dict[str, str], Path]:
    """A `gh` on PATH that records its arguments and lists ``ids`` as existing rulesets."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh.log"
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        f'echo "$* [cwd=$(pwd -P)]" >> "{log}"\n'
        'case "$*" in\n'
        '  "repo view"*) echo Opsedia-ai/Opseval ;;\n'
        f'  *--paginate*) printf "{ids}" ;;\n'
        '  *) echo "ruleset 1: active" ;;\n'
        "esac\n",
        "utf-8",
    )
    gh.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}, log


def _apply(
    env: dict[str, str], *args: str, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(APPLY), *args], env=env, cwd=cwd, capture_output=True, text=True, check=False
    )


@needs_bash
def test_a_dry_run_validates_prints_and_never_calls_github(tmp_path: Path) -> None:
    env, log = _fake_gh(tmp_path, "")
    for args in ((), ("--dry-run",)):
        out = _apply(env, *args)
        assert out.returncode == 0, out.stderr
        assert "Dry run: nothing is sent" in out.stdout
        assert '"required_status_checks"' in out.stdout
    assert not log.exists(), "a dry run must not call gh"
    assert _apply(env, "--nope").returncode == 2


@needs_bash
@pytest.mark.parametrize(
    ("ids", "expected"),
    [
        ("", "-X POST repos/Opsedia-ai/Opseval/rulesets "),
        ("42\\n", "-X PUT repos/o/r/rulesets/42 "),
    ],
)
def test_apply_creates_or_updates_by_name(tmp_path: Path, ids: str, expected: str) -> None:
    env, log = _fake_gh(tmp_path, ids)
    args = ("--apply",) if not ids else ("--apply", "--repo", "o/r")
    out = _apply(env, *args, cwd=tmp_path)  # run from outside the checkout
    assert out.returncode == 0, out.stderr
    calls = log.read_text("utf-8")
    assert expected in calls and f"--input {RULESET}" in calls
    if not ids:
        # The repository of the script's own checkout, never of the current directory.
        (view,) = [line for line in calls.splitlines() if line.startswith("repo view")]
        assert view.endswith(f"[cwd={ROOT.resolve()}]")
    else:
        assert "repo view" not in calls, "--repo is used as given"


@needs_bash
def test_apply_refuses_two_rulesets_with_the_same_name(tmp_path: Path) -> None:
    env, log = _fake_gh(tmp_path, "1\\n2\\n")
    out = _apply(env, "--apply", "--repo", "o/r")
    assert out.returncode == 1 and "remove the extra ones" in out.stderr
    assert "-X" not in log.read_text("utf-8")


# --- the pre-push hook ---------------------------------------------------------------------

SHA = "1" * 40
ZERO = "0" * 40


UPSTREAM = "git@github.com:Opsedia-ai/Opseval.git"


def _hook(stdin: str, url: str = UPSTREAM, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", str(HOOK), "origin", url],
        input=stdin,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_hook_is_executable_and_installed_by_make_hooks() -> None:
    assert os.access(HOOK, os.X_OK)
    assert os.access(APPLY, os.X_OK)
    assert "git config --local core.hooksPath scripts/githooks" in (ROOT / "Makefile").read_text()


def test_the_hook_refuses_main_and_its_deletion() -> None:
    for stdin in (
        f"refs/heads/work {SHA} refs/heads/main {ZERO}\n",
        f"(delete) {ZERO} refs/heads/main {SHA}\n",
        f"refs/heads/a {SHA} refs/heads/a {ZERO}\nHEAD {SHA} refs/heads/main {SHA}\n",
    ):
        out = _hook(stdin, SKIP_PRE_PUSH_CHECKS="1")
        assert out.returncode == 1
        assert "refusing to push to main on origin" in out.stderr and "pull request" in out.stderr


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:Opsedia-ai/Opseval.git",
        "git@github.com:opsedia-ai/opseval",
        "ssh://git@github.com/Opsedia-ai/Opseval.git",
        "ssh://git@github.com/OPSEDIA-AI/OPSEVAL",
        "https://github.com/Opsedia-ai/Opseval.git",
        "https://github.com/Opsedia-AI/Opseval/",
        "https://x-access-token@github.com/Opsedia-ai/Opseval",
        "",  # no URL: the hook run by hand counts as upstream
    ],
)
def test_the_hook_refuses_main_of_the_upstream_repository(url: str) -> None:
    out = _hook(f"refs/heads/work {SHA} refs/heads/main {ZERO}\n", url, SKIP_PRE_PUSH_CHECKS="1")
    assert out.returncode == 1 and "refusing to push to main" in out.stderr


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:someone/Opseval.git",
        "https://github.com/someone/Opseval",
        "git@github.com:Opsedia-ai/Opseval-fork.git",
        "https://github.com/Opsedia-ai/Opseval.git.evil",
        "https://gitlab.com/Opsedia-ai/Opseval.git",
        "/srv/git/opseval.git",
    ],
)
def test_the_hook_lets_main_go_to_a_fork_after_the_checks(
    url: str, tmp_path: Path, repo: Repo
) -> None:
    for stdin in (
        f"refs/heads/work {SHA} refs/heads/main {ZERO}\n",
        f"(delete) {ZERO} refs/heads/main {SHA}\n",
    ):
        out = _hook(stdin, url, SKIP_PRE_PUSH_CHECKS="1")
        assert out.returncode == 0, out.stderr
    # Not refused, so the fast checks run (and fail here: no uv on this PATH).
    repo.commit("base")
    out = subprocess.run(
        ["/bin/sh", str(HOOK), "origin", url],
        input=f"refs/heads/work {SHA} refs/heads/main {ZERO}\n",
        cwd=repo.path,
        env={**os.environ, "PATH": str(_only(tmp_path, "git", "grep"))},
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 1 and "uv is not installed" in out.stderr
    assert "refusing" not in out.stderr


def _only(tmp_path: Path, *tools: str) -> Path:
    """A PATH directory holding only ``tools``."""
    bin_dir = tmp_path / "only"
    bin_dir.mkdir()
    for tool in tools:
        found = shutil.which(tool)
        assert found is not None
        (bin_dir / tool).symlink_to(found)
    return bin_dir


def test_the_hook_refuses_main_when_grep_cannot_run() -> None:
    """grep failing to run is not "no match": main stays refused (fail closed)."""
    stdin = f"refs/heads/work {SHA} refs/heads/main {ZERO}\n"
    out = _hook(stdin, "/srv/fork.git", PATH="/nonexistent")
    assert out.returncode == 1 and "refusing to push to main" in out.stderr


def test_the_hook_lets_a_branch_through() -> None:
    assert (
        _hook(
            f"refs/heads/work {SHA} refs/heads/work {ZERO}\n", SKIP_PRE_PUSH_CHECKS="1"
        ).returncode
        == 0
    )
    # A deletion runs no checks (PATH without uv would fail them).
    out = _hook(f"(delete) {ZERO} refs/heads/old {SHA}\n", PATH="/nonexistent")
    assert out.returncode == 0, out.stderr
    assert _hook("").returncode == 0


def test_the_hook_says_when_it_cannot_run_the_checks(tmp_path: Path, repo: Repo) -> None:
    repo.commit("base")
    git = shutil.which("git")
    assert git is not None
    bin_dir = tmp_path / "gitonly"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(git)
    out = subprocess.run(
        ["/bin/sh", str(HOOK), "origin", "x"],
        input=f"refs/heads/work {SHA} refs/heads/work {ZERO}\n",
        cwd=repo.path,
        env={**os.environ, "PATH": str(bin_dir)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 1 and "uv is not installed" in out.stderr


def test_a_real_push_goes_through_the_hook(tmp_path: Path, repo: Repo) -> None:
    """git passes the hook the URL it pushes to; a local remote is a fork, not upstream."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    repo.commit("feat: x", trailers=SIGNED)
    repo.git("config", "core.hooksPath", str(HOOK.parent))
    repo.git("remote", "add", "origin", str(remote))
    repo.git("push", "-q", "origin", "work:feat/x", env={"SKIP_PRE_PUSH_CHECKS": "1"})
    repo.git("push", "-q", "origin", "work:main", env={"SKIP_PRE_PUSH_CHECKS": "1"})
    branches = subprocess.run(
        ["git", "branch", "--list"], cwd=remote, capture_output=True, text=True, check=True
    ).stdout
    assert "feat/x" in branches and "main" in branches


# --- make hooks ----------------------------------------------------------------------------

needs_make = pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")


def _make_hooks(repo: Repo, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "-s", "-f", str(ROOT / "Makefile"), "hooks", *args],
        cwd=repo.path,
        capture_output=True,
        text=True,
        check=False,
    )


def _hooks_path(repo: Repo) -> str:
    out = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"], cwd=repo.path, capture_output=True, text=True
    )
    return out.stdout.strip()


@needs_make
def test_make_hooks_installs_and_reinstalls(repo: Repo) -> None:
    for _ in range(2):
        out = _make_hooks(repo)
        assert out.returncode == 0, out.stderr
        assert _hooks_path(repo) == "scripts/githooks"


@needs_make
def test_make_hooks_will_not_switch_off_the_clones_own_hooks(repo: Repo) -> None:
    hooks = Path(repo.git("rev-parse", "--git-path", "hooks"))
    hooks = hooks if hooks.is_absolute() else repo.path / hooks
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit.sample").write_text("#!/bin/sh\n", "utf-8")
    assert _make_hooks(repo).returncode == 0, "sample hooks are not hooks"
    repo.git("config", "--unset", "core.hooksPath")
    (hooks / "pre-commit").write_text("#!/bin/sh\n", "utf-8")
    out = _make_hooks(repo)
    assert out.returncode != 0 and "pre-commit" in out.stderr and "FORCE=1" in out.stderr
    assert _hooks_path(repo) == ""
    out = _make_hooks(repo, "FORCE=1")
    assert out.returncode == 0, out.stderr
    assert _hooks_path(repo) == "scripts/githooks"


@needs_make
def test_make_hooks_will_not_replace_another_hooks_path(repo: Repo) -> None:
    repo.git("config", "core.hooksPath", ".husky")
    out = _make_hooks(repo)
    assert out.returncode != 0 and "already .husky" in out.stderr
    assert _hooks_path(repo) == ".husky"
    assert _make_hooks(repo, "FORCE=1").returncode == 0
    assert _hooks_path(repo) == "scripts/githooks"
