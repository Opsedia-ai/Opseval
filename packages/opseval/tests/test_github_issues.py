# SPDX-License-Identifier: Apache-2.0
"""GitHub Issues sink (FR-INT-05, ADR-0040), against a fake GitHub API.

It reports only real agent failures of critical scenarios, once per scenario, never with
conversation content, never flooding, and a failure to write is reported, never silent.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from opseval.config import Settings, config_digest
from opseval.errors import AllowedHostError, ConfigError
from opseval.models.results import (
    Axis,
    ScoreResult,
    ScoreStatus,
    TestVerdict,
    VerdictStatus,
)
from opseval.models.scenario import Criticality
from opseval.sinks import build_sinks, publish_all
from opseval.sinks.github_issues import GitHubIssuesSink, marker

from .test_sinks import attempt, result

F = VerdictStatus.FAILED
SECRET_TURN = "my card number is 4111 and I live at 12 Private Road"


class FakeGitHub:
    def __init__(self, *, fail_titles: tuple[str, ...] = ()) -> None:
        self.issues: list[dict[str, Any]] = []
        self.comments: list[tuple[int, str]] = []
        self.requests: list[httpx.Request] = []
        self.fail_titles = fail_titles

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["authorization"] == "Bearer tok"
        path = request.url.path
        if request.method == "GET" and path.endswith("/issues"):
            return httpx.Response(200, json=[i for i in self.issues if i["state"] == "open"])
        body = json.loads(request.content)
        if request.method == "POST" and path.endswith("/comments"):
            number = int(path.split("/")[-2])
            self.comments.append((number, body["body"]))
            return httpx.Response(201, json={"id": 1})
        if request.method == "POST" and path.endswith("/issues"):
            if any(t in body["title"] for t in self.fail_titles):
                return httpx.Response(500, json={"message": "boom"})
            issue = {"number": 100 + len(self.issues), "state": "open", **body}
            self.issues.append(issue)
            return httpx.Response(201, json=issue)
        return httpx.Response(404)

    def sink(self, **kw: Any) -> GitHubIssuesSink:
        return GitHubIssuesSink(
            "acme/agent",
            "tok",
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
            **kw,
        )

    def sent(self) -> str:
        return " ".join(r.content.decode() for r in self.requests)


def failing(
    test_id: str, criticality: Criticality = Criticality.CRITICAL, **kw: Any
) -> TestVerdict:
    judged = attempt().model_copy(
        update={
            "passed": False,
            "scores": [
                ScoreResult(
                    scorer_id="judge.goal_achievement",
                    axis=Axis.OUTPUT,
                    status=ScoreStatus.FAILED,
                    score=0.0,
                    threshold=1.0,
                    weight=1.0,
                    reason=f'quote: "{SECRET_TURN}"',
                )
            ],
        }
    )
    return TestVerdict(
        test_id=test_id,
        name=kw.pop("name", f"scenario {test_id}"),
        criticality=criticality,
        status=kw.pop("status", F),
        reason="failed checks: judge.goal_achievement, tools.forbidden_not_called",
        pass_rate=0.0,
        attempts=[judged],
        **kw,
    )


class TestWhatIsReported:
    async def test_only_failed_critical_and_blocker_scenarios(self) -> None:
        gh = FakeGitHub()
        res = result(
            failing("c-1"),
            failing("b-1", Criticality.BLOCKER),
            failing("n-1", Criticality.NORMAL),
            failing("i-1", status=VerdictStatus.INCONCLUSIVE),
            failing("s-1", status=VerdictStatus.SKIPPED_PRECONDITION),
        )
        detail = await gh.sink().publish(res)
        assert sorted(i["title"].split()[1] for i in gh.issues) == ["b-1", "c-1"]
        assert "opened #100, #101" in detail

    async def test_nothing_failed_means_no_call_at_all(self) -> None:
        gh = FakeGitHub()
        assert "nothing to report" in await gh.sink().publish(result())
        assert gh.requests == []

    async def test_no_conversation_content_leaves(self) -> None:
        gh = FakeGitHub()
        await gh.sink().publish(result(failing("c-1")))
        assert SECRET_TURN not in gh.sent(), "judge quotes stay in the report"
        assert "tools.forbidden_not_called" in gh.sent(), "check ids are kept intact"

    async def test_names_cannot_mention_people_or_break_the_marker(self) -> None:
        gh = FakeGitHub()
        res = result(failing("c-1", name="ping @security-team <b>"), suite="x --> y")
        await gh.sink().publish(res)
        issue = gh.issues[0]
        assert "@security-team" not in issue["title"] and "<b>" not in issue["title"]
        assert marker("x --> y", "c-1") == "<!-- opseval:x_--__y:c-1 -->"
        assert issue["body"].count("-->") == 1


class TestOnceAndBounded:
    async def test_a_repeat_failure_comments_on_the_open_issue(self) -> None:
        gh = FakeGitHub()
        await gh.sink().publish(result(failing("c-1")))
        detail = await gh.sink().publish(result(failing("c-1"), run_id="20261006T000000Z-def"))
        assert len(gh.issues) == 1 and gh.comments[0][0] == 100
        assert "Failed again" in gh.comments[0][1] and "commented on #100" in detail

    async def test_a_closed_issue_is_not_reused(self) -> None:
        gh = FakeGitHub()
        await gh.sink().publish(result(failing("c-1")))
        gh.issues[0]["state"] = "closed"
        await gh.sink().publish(result(failing("c-1")))
        assert len(gh.issues) == 2

    async def test_at_most_max_issues_per_run(self) -> None:
        gh = FakeGitHub()
        res = result(*(failing(f"c-{i:02d}") for i in range(12)))
        detail = await gh.sink(max_issues=10).publish(res)
        assert len(gh.issues) == 10 and "2 more not reported (limit 10)" in detail


class TestFailuresAreReported:
    async def test_one_failed_write_does_not_stop_the_rest_and_is_named(self) -> None:
        gh = FakeGitHub(fail_titles=("c-2",))
        res = result(failing("c-1"), failing("c-2"), failing("c-3"))
        outcomes = await publish_all([gh.sink()], res)
        assert len(gh.issues) == 2, "c-1 and c-3 were still opened"
        (outcome,) = outcomes
        assert outcome.published is False
        assert "opened #100, #101" in outcome.detail and "c-2" in outcome.detail


class TestConfiguration:
    def settings(self, **kw: Any) -> Settings:
        base: dict[str, Any] = {
            "github_issues_repo": "acme/agent",
            "github_token": SecretStr("ghp_secret"),
            "allowed_hosts": "api.github.com",
        }
        return Settings(**{**base, **kw})

    def test_it_is_built_when_configured(self) -> None:
        assert [s.name for s in build_sinks(self.settings())] == ["github-issues"]

    @pytest.mark.parametrize("repo", ["acme", "acme/agent/x", "a b/c"])
    def test_a_repo_that_is_not_owner_name_is_refused(self, repo: str) -> None:
        with pytest.raises(ConfigError, match="owner/name"):
            build_sinks(self.settings(github_issues_repo=repo))

    def test_it_needs_a_token(self) -> None:
        with pytest.raises(ConfigError, match="OPSEVAL_GITHUB_TOKEN"):
            build_sinks(self.settings(github_token=None))

    def test_the_api_host_must_be_allowlisted(self) -> None:
        with pytest.raises(AllowedHostError):
            build_sinks(self.settings(allowed_hosts=""))
        enterprise = self.settings(github_api_url="https://ghe.acme.example/api/v3")
        with pytest.raises(AllowedHostError):
            build_sinks(enterprise)

    def test_the_token_is_a_secret(self) -> None:
        digest = config_digest(self.settings())
        assert "ghp_secret" not in str(digest) and digest["github_token"] == "***redacted***"
