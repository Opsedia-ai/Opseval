# SPDX-License-Identifier: Apache-2.0
"""Jira sink (FR-INT-05, ADR-0045), against a fake Jira (Cloud and Data Center search APIs).

The same promises as the GitHub sink: only real failures of critical scenarios, once per
scenario, no conversation content, bounded, and a failure to write is reported, never silent.
"""

from __future__ import annotations

import base64
import json
import re
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
    def __init__(
        self,
        *,
        cloud: bool = True,
        fail_summaries: tuple[str, ...] = (),
        page_cap: int = 100,
        strip_labels: bool = False,
        drop_token: bool = False,
        no_key: bool = False,
        readback: int = 200,
        omit_is_last: bool = False,
    ) -> None:
        self.readback = readback  # status of the label read-back after a create
        self.omit_is_last = omit_is_last  # a Cloud answer without the isLast field
        self.cloud = cloud
        self.page_cap = page_cap  # Data Center's admin cap on maxResults
        self.strip_labels = strip_labels  # an automation rule removing labels after create
        self.drop_token = drop_token  # Cloud says "not last" but gives no token
        self.no_key = no_key  # a 201 whose body has no key
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
            assert body["jql"].endswith("ORDER BY key ASC"), "a fixed order for paging"
            data: dict[str, Any] = {"issues": page}
            if not self.omit_is_last:
                data["isLast"] = not more
            if more and not self.drop_token:
                data["nextPageToken"] = str(nxt)
            return httpx.Response(200, json=data)
        if path == "/rest/api/2/search":
            assert not self.cloud, "Cloud's /rest/api/2/search is retired"
            start = int(request.url.params["startAt"])
            size = min(int(request.url.params["maxResults"]), self.page_cap)
            everything = self._open()
            return httpx.Response(
                200,
                json={
                    "startAt": start,
                    "maxResults": size,
                    "total": len(everything),
                    "issues": everything[start : start + size],
                },
            )
        if request.method == "GET" and path.startswith("/rest/api/2/issue/"):
            key = path.rsplit("/", 1)[-1]
            if self.readback != 200:
                return httpx.Response(self.readback, json={"errorMessages": ["not yet"]})
            [issue] = [i for i in self.issues if i["key"] == key]
            return httpx.Response(
                200, json={"key": key, "fields": {"labels": issue["fields"]["labels"]}}
            )
        if request.method == "POST" and path.endswith("/comment"):
            key = path.split("/")[-2]
            self.comments.append((key, json.loads(request.content)["body"]))
            return httpx.Response(201, json={"id": "1"})
        if request.method == "POST" and path == "/rest/api/2/issue":
            fields = json.loads(request.content)["fields"]
            if any(s in fields["summary"] for s in self.fail_summaries):
                return httpx.Response(400, json={"errors": {"labels": "cannot be set"}})
            key = f"AGENT-{100 + len(self.issues)}"
            if self.strip_labels:
                fields = {**fields, "labels": []}
            self.issues.append({"key": key, "done": False, "fields": fields})
            return httpx.Response(201, json={"id": "1"} if self.no_key else {"id": "1", "key": key})
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
        with pytest.raises(RuntimeError, match="more than 20 pages"):
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


class TestReviewOf64:
    async def test_a_capped_data_center_page_is_not_the_last(self) -> None:
        """Data Center caps maxResults by an admin setting: 50 back is not "the end"."""
        jira = FakeJira(cloud=False, page_cap=50)
        for n in range(120):
            jira.issues.append(
                {"key": f"OLD-{n}", "done": False, "fields": {"labels": ["opseval"]}}
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

    async def test_cloud_not_last_without_a_token_fails_closed(self) -> None:
        jira = FakeJira(cloud=True, drop_token=True)
        for n in range(150):
            jira.issues.append(
                {"key": f"OLD-{n}", "done": False, "fields": {"labels": ["opseval"]}}
            )
        with pytest.raises(RuntimeError, match="no page token"):
            await jira.sink().publish(result(failing("c-1")))
        assert not any(i["key"].startswith("AGENT-") for i in jira.issues)

    async def test_labels_removed_after_create_stop_the_sink(self, jira: FakeJira) -> None:
        jira.strip_labels = True
        [outcome] = await publish_all([jira.sink()], result(failing("c-1"), failing("c-2")))
        assert outcome.published is False
        assert "opened AGENT-100" in outcome.detail, "the issue it did open is still named"
        assert "lost its 'opseval' labels" in outcome.detail
        assert len(jira.issues) == 1, "no further issue is opened"

    async def test_a_created_issue_without_a_key_stops_rather_than_duplicates(
        self, jira: FakeJira
    ) -> None:
        jira.no_key = True
        [outcome] = await publish_all([jira.sink()], result(failing("c-1"), failing("c-1")))
        assert outcome.published is False and "had no key" in outcome.detail
        assert len(jira.issues) == 1

    async def test_a_blank_email_means_data_center(self) -> None:
        jira = FakeJira(cloud=False)
        await jira.sink(email="  ").publish(result(failing("c-1")))
        assert {r.headers["authorization"] for r in jira.requests} == {"Bearer tok"}

    def test_a_blank_email_setting_builds_a_data_center_sink(self) -> None:
        [sink] = build_sinks(
            Settings(
                allowed_hosts="acme.atlassian.net",
                jira_url=BASE,
                jira_project="AGENT",
                jira_token=SecretStr("tok"),
                jira_email="",
            )  # type: ignore[call-arg]
        )
        assert isinstance(sink, JiraSink) and sink._cloud is False

    @pytest.mark.parametrize("closer", ["{NOFORMAT}", "{NoFormat}", "{ noformat}", "{noformat:x}"])
    async def test_no_spelling_of_the_closing_tag_survives(self, closer: str) -> None:
        jira = FakeJira()
        await jira.sink().publish(result(failing("c-1", name=f"a {closer}[x|https://e.vil]")))
        plain = FakeJira()
        await plain.sink().publish(result(failing("c-1", name="a b")))
        desc = jira.issues[0]["fields"]["description"]
        tags = re.compile(r"\{\s*noformat", re.IGNORECASE)
        # Exactly the tags the sink itself wrote: the name added none that could close one.
        assert len(tags.findall(desc)) == len(
            tags.findall(plain.issues[0]["fields"]["description"])
        )
        assert "[x|https://e.vil]" in desc

    def test_both_sinks_share_one_rule_for_what_is_reported(self) -> None:
        from opseval.sinks import github_issues, issues, jira

        assert github_issues.failed_serious is issues.failed_serious
        assert issubclass(jira.JiraSink, issues.IssueSink)
        assert issubclass(github_issues.GitHubIssuesSink, issues.IssueSink)


class TestReviewOf66:
    @pytest.mark.parametrize("status", [403, 404, 500])
    async def test_a_failed_read_back_still_names_the_issue_and_stops(
        self, jira: FakeJira, status: int
    ) -> None:
        jira.readback = status
        [outcome] = await publish_all([jira.sink()], result(failing("c-1"), failing("c-2")))
        assert outcome.published is False
        assert "opened AGENT-100" in outcome.detail, "the issue it created is reported"
        assert "labels could not be read back" in outcome.detail
        assert len(jira.issues) == 1, "no further issue is opened"

    async def test_a_last_page_without_is_last_is_still_the_last(self) -> None:
        jira = FakeJira(cloud=True, omit_is_last=True)
        jira.issues.append(
            {
                "key": "OLD-1",
                "done": False,
                "fields": {"labels": ["opseval", scenario_label("smoke", "c-1")]},
            }
        )
        detail = await jira.sink().publish(result(failing("c-1")))
        assert "commented on OLD-1" in detail

    def test_the_search_has_a_fixed_order(self) -> None:
        sink = FakeJira().sink()
        assert sink._jql().endswith("ORDER BY key ASC")
