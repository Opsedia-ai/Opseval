# SPDX-License-Identifier: Apache-2.0
"""Jira sink (FR-INT-05, ADR-0045), against a fake Jira (Cloud and Data Center search APIs).

The same promises as the GitHub sink: only real failures of critical scenarios, once per
scenario, no conversation content, bounded, and a failure to write is reported, never silent.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from opseval.config import Settings, config_digest
from opseval.errors import AllowedHostError, ConfigError
from opseval.models.results import VerdictStatus
from opseval.models.scenario import Criticality
from opseval.sinks import build_sinks, publish_all
from opseval.sinks.jira import JiraSink, scenario_label

from .test_github_issues import SECRET_TURN, failing
from .test_sinks import result

BASE = "https://acme.atlassian.net"


class FakeJira:
    def __init__(self, *, cloud: bool = True, fail_summaries: tuple[str, ...] = ()) -> None:
        self.cloud = cloud
        self.issues: list[dict[str, Any]] = []
        self.comments: list[tuple[str, str]] = []
        self.requests: list[httpx.Request] = []
        self.fail_summaries = fail_summaries

    def _open(self) -> list[dict[str, Any]]:
        return [
            {"key": i["key"], "fields": {"labels": i["fields"]["labels"]}}
            for i in self.issues
            if not i["done"] and "opseval" in i["fields"]["labels"]
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/rest/api/3/search/jql":
            assert self.cloud, "Data Center has no /search/jql"
            body = json.loads(request.content)
            assert "statusCategory != Done" in body["jql"]
            start = int(body.get("nextPageToken") or 0)
            page = self._open()[start : start + 100]
            nxt = start + 100
            more = nxt < len(self._open())
            data: dict[str, Any] = {"issues": page, "isLast": not more}
            if more:
                data["nextPageToken"] = str(nxt)
            return httpx.Response(200, json=data)
        if path == "/rest/api/2/search":
            assert not self.cloud, "Cloud's /rest/api/2/search is retired"
            start = int(request.url.params["startAt"])
            return httpx.Response(200, json={"issues": self._open()[start : start + 100]})
        if request.method == "POST" and path.endswith("/comment"):
            key = path.split("/")[-2]
            self.comments.append((key, json.loads(request.content)["body"]))
            return httpx.Response(201, json={"id": "1"})
        if request.method == "POST" and path == "/rest/api/2/issue":
            fields = json.loads(request.content)["fields"]
            if any(s in fields["summary"] for s in self.fail_summaries):
                return httpx.Response(400, json={"errors": {"labels": "cannot be set"}})
            key = f"AGENT-{100 + len(self.issues)}"
            self.issues.append({"key": key, "done": False, "fields": fields})
            return httpx.Response(201, json={"id": "1", "key": key})
        return httpx.Response(404)

    def sink(self, **kw: Any) -> JiraSink:
        if self.cloud:
            kw.setdefault("email", "bot@acme.example")
        return JiraSink(
            BASE,
            "AGENT",
            "tok",
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
            **kw,
        )

    def sent(self) -> str:
        return " ".join(r.content.decode() for r in self.requests)


@pytest.fixture(params=["cloud", "datacenter"])
def jira(request: pytest.FixtureRequest) -> FakeJira:
    return FakeJira(cloud=request.param == "cloud")


class TestWhatIsReported:
    async def test_only_failed_critical_and_blocker_scenarios(self, jira: FakeJira) -> None:
        res = result(
            failing("c-1"),
            failing("b-1", Criticality.BLOCKER),
            failing("n-1", Criticality.NORMAL),
            failing("i-1", status=VerdictStatus.INCONCLUSIVE),
            failing("s-1", status=VerdictStatus.SKIPPED_PRECONDITION),
        )
        detail = await jira.sink().publish(res)
        assert sorted(i["fields"]["summary"].split()[1] for i in jira.issues) == ["b-1", "c-1"]
        assert "opened AGENT-100, AGENT-101" in detail

    async def test_nothing_failed_means_no_call_at_all(self, jira: FakeJira) -> None:
        assert "nothing to report" in await jira.sink().publish(result())
        assert jira.requests == []

    async def test_no_conversation_content_and_no_url_leaves(self, jira: FakeJira) -> None:
        res = result(failing("c-1"), target="wss://user:pass@agent.internal/ws?token=abc")
        await jira.sink().publish(res)
        assert SECRET_TURN not in jira.sent(), "judge quotes stay in the report"
        for part in ("agent.internal", "user:pass", "token=abc", "wss://"):
            assert part not in jira.sent(), part
        assert "tools.forbidden_not_called" in jira.sent()

    async def test_scenario_text_is_inert(self, jira: FakeJira) -> None:
        name = "ping [~admin] {noformat}[x|https://e.vil]{noformat} {jira:macro}"
        await jira.sink().publish(result(failing("c-1", name=name)))
        fields = jira.issues[0]["fields"]
        assert "[~admin]" not in fields["summary"]
        desc = fields["description"]
        # Exactly the noformat spans the sink opened: the text could not close one early.
        assert "{noformat}[x|https://e.vil]{noformat}" not in desc
        assert "{noformat}ping [~admin] {no format}[x|https://e.vil]{no format}" in desc

    async def test_each_issue_carries_both_labels(self, jira: FakeJira) -> None:
        await jira.sink().publish(result(failing("c-1")))
        assert jira.issues[0]["fields"]["labels"] == ["opseval", scenario_label("smoke", "c-1")]
        assert jira.issues[0]["fields"]["issuetype"] == {"name": "Bug"}

    def test_scenario_labels_are_one_to_one_and_valid(self) -> None:
        assert scenario_label("a", "b/c") != scenario_label("a/b", "c")
        assert scenario_label("smoke test", "x") != scenario_label("smoke_test", "x")
        label = scenario_label("s p a c e", "id with spaces")
        assert " " not in label and len(label) <= 255


class TestOnceAndBounded:
    async def test_a_repeat_failure_comments_on_the_open_issue(self, jira: FakeJira) -> None:
        await jira.sink().publish(result(failing("c-1")))
        detail = await jira.sink().publish(result(failing("c-1"), run_id="20261006T000000Z-x"))
        assert len(jira.issues) == 1 and jira.comments[0][0] == "AGENT-100"
        assert "Failed again" in jira.comments[0][1] and "commented on AGENT-100" in detail

    async def test_a_second_failure_in_the_same_run_comments(self, jira: FakeJira) -> None:
        detail = await jira.sink().publish(result(failing("c-1"), failing("c-1")))
        assert len(jira.issues) == 1 and "commented on AGENT-100" in detail

    async def test_a_done_issue_is_not_reused(self, jira: FakeJira) -> None:
        await jira.sink().publish(result(failing("c-1")))
        jira.issues[0]["done"] = True
        await jira.sink().publish(result(failing("c-1")))
        assert len(jira.issues) == 2

    async def test_every_open_issue_is_read_across_pages(self, jira: FakeJira) -> None:
        for n in range(250):
            jira.issues.append(
                {
                    "key": f"OLD-{n}",
                    "done": False,
                    "fields": {"labels": ["opseval", scenario_label("smoke", f"old-{n}")]},
                }
            )
        jira.issues.append(
            {
                "key": "OLD-LAST",
                "done": False,
                "fields": {"labels": ["opseval", scenario_label("smoke", "c-1")]},
            }
        )
        detail = await jira.sink().publish(result(failing("c-1")))
        assert "commented on OLD-LAST" in detail

    async def test_too_many_open_issues_stops_rather_than_duplicates(self, jira: FakeJira) -> None:
        for n in range(2001):
            jira.issues.append(
                {"key": f"OLD-{n}", "done": False, "fields": {"labels": ["opseval"]}}
            )
        with pytest.raises(RuntimeError, match="more than 2000 open"):
            await jira.sink().publish(result(failing("c-1")))
        assert not any(i["key"].startswith("AGENT-") for i in jira.issues)

    async def test_at_most_max_issues_per_run(self, jira: FakeJira) -> None:
        res = result(*(failing(f"c-{i:02d}") for i in range(12)))
        detail = await jira.sink(max_issues=10).publish(res)
        assert len(jira.issues) == 10 and "2 more not reported (limit 10)" in detail


class TestFailuresAreReported:
    async def test_one_failure_does_not_stop_the_others_and_is_named(self) -> None:
        jira = FakeJira(fail_summaries=("c-2",))
        res = result(failing("c-1"), failing("c-2"), failing("c-3"))
        [outcome] = await publish_all([jira.sink()], res)
        assert outcome.published is False
        assert "opened AGENT-100, AGENT-101" in outcome.detail
        assert "c-2: RuntimeError: Jira answered HTTP 400" in outcome.detail

    async def test_an_unreadable_search_fails_closed(self) -> None:
        def broken(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"errorMessages": ["nope"]})

        sink = JiraSink(
            BASE,
            "AGENT",
            "tok",
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(broken)),
        )
        with pytest.raises(RuntimeError, match="no issue list"):
            await sink.publish(result(failing("c-1")))


class TestAuth:
    async def test_cloud_uses_email_and_api_token(self) -> None:
        jira = FakeJira(cloud=True)
        await jira.sink().publish(result(failing("c-1")))
        expected = base64.b64encode(b"bot@acme.example:tok").decode()
        assert {r.headers["authorization"] for r in jira.requests} == {f"Basic {expected}"}

    async def test_data_center_uses_a_bearer_token(self) -> None:
        jira = FakeJira(cloud=False)
        await jira.sink().publish(result(failing("c-1")))
        assert {r.headers["authorization"] for r in jira.requests} == {"Bearer tok"}


class TestConfiguration:
    def settings(self, **kw: Any) -> Settings:
        base: dict[str, Any] = {
            "allowed_hosts": "acme.atlassian.net",
            "jira_url": BASE,
            "jira_project": "AGENT",
            "jira_token": SecretStr("tok"),
        }
        return Settings(**{**base, **kw})  # type: ignore[arg-type]

    def test_a_complete_config_builds_the_sink(self) -> None:
        [sink] = build_sinks(self.settings())
        assert isinstance(sink, JiraSink)

    def test_the_token_is_a_secret(self) -> None:
        digest = config_digest(self.settings(jira_token=SecretStr("very-secret")))
        assert "very-secret" not in json.dumps(digest, default=str)

    @pytest.mark.parametrize(
        ("kw", "match"),
        [
            ({"jira_project": None}, "both OPSEVAL_JIRA_URL and OPSEVAL_JIRA_PROJECT"),
            ({"jira_url": None}, "both OPSEVAL_JIRA_URL and OPSEVAL_JIRA_PROJECT"),
            ({"jira_project": 'A" OR project = "B'}, "project key"),
            ({"jira_project": "agent"}, "project key"),
            ({"jira_token": None}, "OPSEVAL_JIRA_TOKEN"),
        ],
    )
    def test_an_incomplete_or_unsafe_config_is_refused(
        self, kw: dict[str, Any], match: str
    ) -> None:
        with pytest.raises(ConfigError, match=match):
            build_sinks(self.settings(**kw))

    def test_the_host_must_be_allowlisted_and_tls(self) -> None:
        with pytest.raises(AllowedHostError):
            build_sinks(self.settings(allowed_hosts="example.org"))
        with pytest.raises(AllowedHostError):
            build_sinks(self.settings(jira_url="http://acme.atlassian.net"))
