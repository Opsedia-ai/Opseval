# SPDX-License-Identifier: Apache-2.0
"""``opseval run --server`` (ADR-0058 E2): pull a label, run locally, report the run.

The one rule, both halves: a server that cannot be reached, refuses the key or refuses the run
never changes the exit code (the local verdict stands, the failure is named in result.sinks,
the console, the gate summary and JUnit), and a pull that fails is exit 3, since nothing could
be evaluated.

The first half runs against an ``httpx.MockTransport`` server; the second against a real
``create_app`` in accounts mode on Postgres (``conftest.pg_url``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from typer.testing import CliRunner

from opseval import remote
from opseval.catalogue_files import StoredFile, write_label
from opseval.cli import app
from opseval.config import SECRET_FIELDS, CiEnvironment, Settings, config_digest
from opseval.demo import catalogue_dir
from opseval.demo.refund_agent import run_agent
from opseval.errors import AllowedHostError, ConfigError
from opseval.models.results import SinkOutcome, SuiteResult
from opseval.models.upload import (
    SERVER_SINK,
    CatalogueExport,
    CiInfo,
    RunUpload,
    same_result,
)
from opseval.reporting.markdown import markdown_summary
from opseval.storage import FileStore

runner = CliRunner()
PUBLIC = "pk-oe-test0public"
SECRET = "sk-oe-SENTINEL-do-not-leak-7f3a"
SERVER = "http://127.0.0.1:8000"
PROJECT = "acme/bot"


# --- helpers ----------------------------------------------------------------------------------


def demo_files() -> list[dict[str, Any]]:
    cat = catalogue_dir()
    out: list[dict[str, Any]] = []
    for path in sorted(cat.rglob("*.yaml")):
        rel = path.relative_to(cat).as_posix()
        kind = "profile" if rel == "profile.yaml" else rel.split("/")[0].rstrip("s")
        out.append(
            {
                "kind": kind,
                "name": path.stem,
                "version": 1,
                "path": rel,
                "source": path.read_text("utf-8"),
            }
        )
    return out


def export_body(label: str = "production", task: str = "default") -> dict[str, Any]:
    return {"project": PROJECT, "task": task, "label": label, "files": demo_files()}


class FakeServer:
    """The two routes, answering as configured, recording what they were sent."""

    def __init__(self, upload: Callable[[httpx.Request], httpx.Response] | None = None) -> None:
        self.uploads: list[httpx.Request] = []
        self.pulls: list[httpx.Request] = []
        self.upload_answer = upload or (
            lambda r: httpx.Response(
                201, json={"run_id": "x", "outcome": "imported", "detail": "stored"}
            )
        )
        self.pull_answer: Callable[[httpx.Request], httpx.Response] = lambda r: httpx.Response(
            200, json=export_body(r.url.params["label"], r.url.params["task"])
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/catalogue/export":
            self.pulls.append(request)
            return self.pull_answer(request)
        if request.url.path.endswith("/upload"):
            self.uploads.append(request)
            return self.upload_answer(request)
        return httpx.Response(404, json={"code": "NOT_FOUND", "message": "not found"})

    def client(self, server: remote.Server, settings: Settings) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler), base_url=server.url)


@pytest.fixture(autouse=True)
def _no_ci_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests set the CI variables they mean; the runner's own (GITHUB_*, CI_*) are cleared,
    or a test run on GitHub Actions would read its own job as the CI being described."""
    for field in CiEnvironment.model_fields.values():
        if field.alias:
            monkeypatch.delenv(field.alias, raising=False)


@pytest.fixture
def agent() -> Iterator[Callable[[str | None], str]]:
    """The demo agent on its own loop and thread, so the CLI (which runs its own loop) can
    reach it from a synchronous test. Call it with a bug name (or None)."""
    stops: list[Callable[[], None]] = []

    def start(bug: str | None = None) -> str:
        ready = threading.Event()
        found: list[str] = []
        loop = asyncio.new_event_loop()
        stop = asyncio.Event()

        async def main() -> None:
            async with run_agent(bug=bug) as url:  # type: ignore[arg-type]
                found.append(url)
                ready.set()
                await stop.wait()

        thread = threading.Thread(target=lambda: loop.run_until_complete(main()), daemon=True)
        thread.start()
        assert ready.wait(10)

        def halt() -> None:
            loop.call_soon_threadsafe(stop.set)
            thread.join(10)
            loop.close()

        stops.append(halt)
        return found[0]

    yield start
    for halt in stops:
        halt()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A CI job's environment: the key, the allowlist, no .env, no CI system unless a test
    names one."""
    monkeypatch.chdir(tmp_path)
    for name in (
        "GITHUB_ACTIONS",
        "GITLAB_CI",
        "TF_BUILD",
        "CIRCLECI",
        "BITBUCKET_BUILD_NUMBER",
        "JENKINS_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPSEVAL_DOTENV", "false")
    monkeypatch.setenv("OPSEVAL_ALLOWED_HOSTS", "127.0.0.1")
    monkeypatch.setenv("OPSEVAL_PUBLIC_KEY", PUBLIC)
    monkeypatch.setenv("OPSEVAL_SECRET_KEY", SECRET)
    monkeypatch.setenv("OPSEVAL_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("OPSEVAL_SERVER_UPLOAD_RETRIES", "2")
    monkeypatch.setattr(remote, "BACKOFF_SECONDS", 0.0)
    return tmp_path


def run_cli(target: str, *extra: str, label: str = "production") -> Any:
    return runner.invoke(
        app,
        [
            "run",
            "suites/smoke.yaml",
            "--target",
            target,
            "--repeats",
            "1",
            "--server",
            SERVER,
            "--project",
            PROJECT,
            "--label",
            label,
            "--junit",
            "junit.xml",
            *extra,
        ],
    )


def saved_result(reports: Path) -> SuiteResult:
    [folder] = [p for p in reports.iterdir() if p.is_dir() and not p.name.startswith(".")]
    return SuiteResult.model_validate_json((folder / "result.json").read_text("utf-8"))


def server_sink(result: SuiteResult) -> SinkOutcome:
    [outcome] = [s for s in result.sinks if s.sink == SERVER_SINK]
    return outcome


def everything_written(root: Path) -> str:
    return "\n".join(p.read_text("utf-8", errors="replace") for p in root.rglob("*") if p.is_file())


# --- settings, the server URL and the key ---------------------------------------------------


class TestSettings:
    def test_the_secret_key_is_a_redacted_secret(self) -> None:
        assert "secret_key" in SECRET_FIELDS
        s = Settings(_env_file=None, public_key=PUBLIC, secret_key=SecretStr(SECRET))
        digest = config_digest(s)
        assert digest["secret_key"] == "***redacted***"
        assert SECRET not in json.dumps(digest, default=str)

    def server(self, url: str = SERVER, **kw: Any) -> remote.Server:
        values: dict[str, Any] = {
            "allowed_hosts": "127.0.0.1,opseval.example.com",
            "public_key": PUBLIC,
            "secret_key": SecretStr(SECRET),
            **kw,
        }
        return remote.server_from(
            Settings(_env_file=None, **values), url, PROJECT, "default", "production"
        )

    def test_an_allowlisted_https_server_or_loopback_is_accepted(self) -> None:
        assert self.server("https://opseval.example.com/").url == "https://opseval.example.com"
        assert self.server().origin == "http://127.0.0.1"

    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.example.net/x?token=abc",
            "http://opseval.example.com",  # plain http off loopback
        ],
    )
    def test_a_refused_host_is_named_by_scheme_and_host_only(self, url: str) -> None:
        with pytest.raises(AllowedHostError) as caught:
            self.server(url)
        assert "token=abc" not in str(caught.value) and "/x" not in str(caught.value)

    def test_user_info_in_the_url_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="base URL only") as caught:
            self.server("https://user:pw@opseval.example.com")
        assert "pw" not in str(caught.value).replace("OPSEVAL_", "")

    @pytest.mark.parametrize(
        "kw",
        [{"public_key": None}, {"secret_key": None}, {"secret_key": SecretStr("  ")}],
    )
    def test_both_halves_of_the_key_are_needed(self, kw: dict[str, Any]) -> None:
        with pytest.raises(ConfigError, match="OPSEVAL_PUBLIC_KEY and OPSEVAL_SECRET_KEY"):
            self.server(**kw)

    def test_the_key_is_never_in_the_servers_repr(self) -> None:
        assert SECRET not in repr(self.server())


# --- CI metadata ------------------------------------------------------------------------------


class TestCiMetadata:
    def test_outside_ci_there_is_none(self) -> None:
        assert remote.ci_metadata(CiEnvironment.model_construct()) is None

    @pytest.mark.parametrize(
        ("env", "want"),
        [
            (
                {
                    "GITHUB_ACTIONS": "true",
                    "GITHUB_SERVER_URL": "https://github.com",
                    "GITHUB_REPOSITORY": "acme/bot",
                    "GITHUB_REF": "refs/heads/main",
                    "GITHUB_SHA": "a" * 40,
                    "GITHUB_RUN_ID": "42",
                    "GITHUB_RUN_ATTEMPT": "2",
                },
                CiInfo(
                    provider="github",
                    repository="acme/bot",
                    ref="refs/heads/main",
                    sha="a" * 40,
                    run_url="https://github.com/acme/bot/actions/runs/42/attempts/2",
                ),
            ),
            (
                {
                    "GITLAB_CI": "true",
                    "CI_PROJECT_PATH": "acme/bot",
                    "CI_COMMIT_REF_NAME": "main",
                    "CI_COMMIT_SHA": "b" * 40,
                    "CI_JOB_URL": "https://gitlab.com/acme/bot/-/jobs/7",
                },
                CiInfo(
                    provider="gitlab",
                    repository="acme/bot",
                    ref="main",
                    sha="b" * 40,
                    run_url="https://gitlab.com/acme/bot/-/jobs/7",
                ),
            ),
            (
                {
                    "TF_BUILD": "True",
                    "BUILD_REPOSITORY_NAME": "bot",
                    "BUILD_SOURCEBRANCH": "refs/heads/main",
                    "BUILD_SOURCEVERSION": "c" * 40,
                    "SYSTEM_COLLECTIONURI": "https://dev.azure.com/acme/",
                    "SYSTEM_TEAMPROJECT": "Agent Bots",
                    "BUILD_BUILDID": "99",
                },
                CiInfo(
                    provider="azure",
                    repository="bot",
                    ref="refs/heads/main",
                    sha="c" * 40,
                    run_url="https://dev.azure.com/acme/Agent%20Bots/_build/results?buildId=99",
                ),
            ),
            (
                {
                    "CIRCLECI": "true",
                    "CIRCLE_PROJECT_USERNAME": "acme",
                    "CIRCLE_PROJECT_REPONAME": "bot",
                    "CIRCLE_TAG": "v1.2.0",
                    "CIRCLE_SHA1": "d" * 40,
                    "CIRCLE_BUILD_URL": "https://circleci.com/gh/acme/bot/5",
                },
                CiInfo(
                    provider="circleci",
                    repository="acme/bot",
                    ref="v1.2.0",
                    sha="d" * 40,
                    run_url="https://circleci.com/gh/acme/bot/5",
                ),
            ),
            (
                {
                    "BITBUCKET_BUILD_NUMBER": "12",
                    "BITBUCKET_REPO_FULL_NAME": "acme/bot",
                    "BITBUCKET_BRANCH": "main",
                    "BITBUCKET_COMMIT": "e" * 40,
                    "BITBUCKET_GIT_HTTP_ORIGIN": "http://bitbucket.org/acme/bot",
                },
                CiInfo(
                    provider="bitbucket",
                    repository="acme/bot",
                    ref="main",
                    sha="e" * 40,
                    run_url="http://bitbucket.org/acme/bot/pipelines/results/12",
                ),
            ),
            (
                {
                    "JENKINS_URL": "https://ci.acme.test/",
                    "GIT_URL": "https://jenkins:t0ken@git.acme.test/acme/bot.git",
                    "GIT_BRANCH": "origin/main",
                    "GIT_COMMIT": "f" * 40,
                    "BUILD_URL": "https://ci.acme.test/job/bot/3/",
                },
                CiInfo(
                    provider="jenkins",
                    repository="https://git.acme.test/acme/bot.git",
                    ref="origin/main",
                    sha="f" * 40,
                    run_url="https://ci.acme.test/job/bot/3/",
                ),
            ),
        ],
    )
    def test_each_ci_system_is_read(self, env: dict[str, str], want: CiInfo) -> None:
        assert remote.ci_metadata(CiEnvironment.model_validate(env)) == want

    def test_a_value_that_does_not_check_out_is_left_out(self) -> None:
        got = remote.ci_metadata(
            CiEnvironment.model_validate(
                {
                    "GITLAB_CI": "true",
                    "CI_COMMIT_SHA": "not-a-sha",
                    "CI_JOB_URL": "javascript:alert(1)",
                    "CI_PROJECT_PATH": "acme/bot",
                }
            )
        )
        assert got == CiInfo(provider="gitlab", repository="acme/bot")

    def test_the_server_refuses_a_link_that_is_not_http(self) -> None:
        for url in ("javascript:alert(1)", "https://u:p@x.test/", "ftp://x.test/"):
            with pytest.raises(ValidationError):
                CiInfo(provider="github", run_url=url)


# --- pull -------------------------------------------------------------------------------------


def a_server(**kw: Any) -> remote.Server:
    return remote.Server(
        url=SERVER,
        project=PROJECT,
        task="default",
        label="production",
        public_key=PUBLIC,
        secret_key=SecretStr(SECRET),
        **kw,
    )


class TestPull:
    def client(self, fake: FakeServer) -> httpx.Client:
        return fake.client(a_server(), Settings(_env_file=None))

    def test_the_label_is_written_as_catalogue_pull_writes_it(self, tmp_path: Path) -> None:
        fake = FakeServer()
        pulled = remote.pull(self.client(fake), a_server(), tmp_path / "cat")
        assert (tmp_path / "cat" / "suites" / "smoke.yaml").is_file()
        assert pulled.label == "production" and len(pulled.items) == len(demo_files())
        [request] = fake.pulls
        assert request.headers["authorization"] == f"Bearer {PUBLIC}:{SECRET}"
        assert dict(request.url.params) == {
            "project": PROJECT,
            "task": "default",
            "label": "production",
        }

    @pytest.mark.parametrize(
        ("answer", "said"),
        [
            (
                httpx.Response(401, json={"code": "INVALID_KEY", "message": "bad"}),
                "401 INVALID_KEY",
            ),
            (httpx.Response(404, json={"code": "NO_SUCH_LABEL", "message": "x"}), "NO_SUCH_LABEL"),
            (httpx.Response(200, json={"files": "nope"}), "not a catalogue export"),
            (httpx.Response(500, text="<html>"), "HTTP 500"),
        ],
    )
    def test_a_failed_pull_is_a_config_error(
        self, tmp_path: Path, answer: httpx.Response, said: str
    ) -> None:
        fake = FakeServer()
        fake.pull_answer = lambda r: answer
        with pytest.raises(ConfigError, match=said) as caught:
            remote.pull(self.client(fake), a_server(), tmp_path / "cat")
        assert SECRET not in str(caught.value)
        assert not (tmp_path / "cat").exists()

    def test_an_unreachable_server_names_its_host_only(self, tmp_path: Path) -> None:
        fake = FakeServer()

        def down(r: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused to http://127.0.0.1:8000/secret-path")

        fake.pull_answer = down
        with pytest.raises(ConfigError, match=r"http://127.0.0.1 could not be reached") as e:
            remote.pull(self.client(fake), a_server(), tmp_path / "cat")
        assert "secret-path" not in str(e.value)

    @pytest.mark.parametrize(
        "path", ["../escape.yaml", "/etc/passwd", "scenarios/x.sh", "suites\\..\\x.yaml"]
    )
    def test_a_stored_path_is_checked_again(self, tmp_path: Path, path: str) -> None:
        fake = FakeServer()
        body = export_body()
        body["files"].append({**body["files"][0], "name": "evil", "path": path})
        fake.pull_answer = lambda r: httpx.Response(200, json=body)
        with pytest.raises(ConfigError, match="not a catalogue file path"):
            remote.pull(self.client(fake), a_server(), tmp_path / "cat")
        assert not (tmp_path / "cat").exists(), "nothing is written before every path passed"

    def test_only_into_a_new_or_empty_folder(self, tmp_path: Path) -> None:
        (tmp_path / "cat").mkdir()
        (tmp_path / "cat" / "left-over.yaml").write_text("x")
        with pytest.raises(ConfigError, match="not an empty folder"):
            write_label(
                tmp_path / "cat", [StoredFile("profile", "p", "profile.yaml", "")], "l", "t"
            )

    def test_another_task_or_label_in_the_answer_is_refused(self, tmp_path: Path) -> None:
        fake = FakeServer()
        fake.pull_answer = lambda r: httpx.Response(200, json=export_body(label="staging"))
        with pytest.raises(ConfigError, match="another task or label"):
            remote.pull(self.client(fake), a_server(), tmp_path / "cat")


# --- upload, unit level -----------------------------------------------------------------------


class TestUploadRetries:
    def upload(self, fake: FakeServer, sleeps: list[float], retries: int = 2) -> SinkOutcome:
        server = a_server()
        return remote.upload(
            fake.client(server, Settings(_env_file=None)),
            server,
            "run-1",
            b"{}",
            retries=retries,
            sleep=sleeps.append,
        )

    def test_a_server_error_is_tried_again_with_growing_waits(self) -> None:
        answers = iter([503, 502, 201])

        def answer(r: httpx.Request) -> httpx.Response:
            code = next(answers)
            body = {"run_id": "run-1", "outcome": "imported", "detail": "ok"}
            return httpx.Response(code, json=body if code == 201 else {"code": "DOWN"})

        sleeps: list[float] = []
        out = self.upload(FakeServer(answer), sleeps)
        assert out.published and "stored" in out.detail
        assert sleeps == [remote.BACKOFF_SECONDS, remote.BACKOFF_SECONDS * 2]

    def test_retry_after_is_honoured_up_to_a_cap(self) -> None:
        answers = iter([429, 201])

        def answer(r: httpx.Request) -> httpx.Response:
            if next(answers) == 429:
                return httpx.Response(429, headers={"Retry-After": "999"})
            return httpx.Response(
                201, json={"run_id": "run-1", "outcome": "imported", "detail": "ok"}
            )

        sleeps: list[float] = []
        assert self.upload(FakeServer(answer), sleeps).published
        assert sleeps == [remote.MAX_WAIT]

    @pytest.mark.parametrize("code", [500, 501, 505, 520, 599])
    def test_every_5xx_is_tried_again(self, code: int) -> None:
        answers = iter([code, 201])

        def answer(r: httpx.Request) -> httpx.Response:
            if next(answers) == code:
                return httpx.Response(code, json={"code": "X"})
            return httpx.Response(
                201, json={"run_id": "run-1", "outcome": "imported", "detail": "ok"}
            )

        assert self.upload(FakeServer(answer), []).published

    def test_a_run_being_imported_is_waited_for(self) -> None:
        answers = iter(["busy", "done"])

        def answer(r: httpx.Request) -> httpx.Response:
            if next(answers) == "busy":
                return httpx.Response(
                    409,
                    json={"code": "RUN_IMPORTING", "message": "m"},
                    headers={"Retry-After": "5"},
                )
            return httpx.Response(
                200, json={"run_id": "run-1", "outcome": "unchanged", "detail": "ok"}
            )

        sleeps: list[float] = []
        assert self.upload(FakeServer(answer), sleeps).published
        assert sleeps == [5.0]

    @pytest.mark.parametrize("code", [401, 403, 404, 409, 413, 422])
    def test_a_refusal_is_final(self, code: int) -> None:
        fake = FakeServer(lambda r: httpx.Response(code, json={"code": "NO", "message": "m"}))
        sleeps: list[float] = []
        out = self.upload(fake, sleeps)
        assert not out.published and f"HTTP {code} NO: m" in out.detail
        assert len(fake.uploads) == 1 and sleeps == []

    def test_every_try_failing_says_how_many(self) -> None:
        def down(r: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        fake = FakeServer(down)
        out = self.upload(fake, [])
        assert not out.published and "after 3 tries" in out.detail
        assert "runs import" in out.detail and len(fake.uploads) == 3

    def test_already_there_is_a_success(self) -> None:
        fake = FakeServer(
            lambda r: httpx.Response(
                200, json={"run_id": "run-1", "outcome": "unchanged", "detail": "same"}
            )
        )
        out = self.upload(fake, [])
        assert out.published and "unchanged" in out.detail


# --- the CLI, against a fake server ---------------------------------------------------------


@pytest.mark.usefixtures("env")
class TestRunServerCli:
    def fake(self, monkeypatch: pytest.MonkeyPatch, fake: FakeServer) -> FakeServer:
        monkeypatch.setattr(remote, "make_client", fake.client)
        return fake

    def test_happy_path_pulls_runs_and_uploads(
        self,
        monkeypatch: pytest.MonkeyPatch,
        agent: Callable[[str | None], str],
        env: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("GITHUB_REPOSITORY", "acme/bot")
        monkeypatch.setenv("GITHUB_SHA", "a" * 40)
        monkeypatch.setenv("GITHUB_RUN_ID", "77")
        monkeypatch.setenv("GITHUB_REF", "refs/tags/v1")
        fake = self.fake(monkeypatch, FakeServer())
        caplog.set_level(logging.DEBUG)
        res = run_cli(agent(None))
        assert res.exit_code == 0, res.output
        assert "Pulled" in res.output and "published     opseval:server" in res.output
        [request] = fake.uploads
        sent = RunUpload.model_validate_json(request.content)
        assert sent.result is not None and sent.result.exit_code == 0
        assert (
            sent.ci is not None and sent.ci.run_url == "https://github.com/acme/bot/actions/runs/77"
        )
        assert sent.ci.ref == "refs/tags/v1" and sent.ci.sha == "a" * 40
        assert sent.catalogue is not None and sent.catalogue.label == "production"
        assert sent.profile is not None and {s.id for s in sent.scenarios}
        assert request.url.params["project"] == PROJECT
        result = saved_result(env / "reports")
        assert server_sink(result).published
        # The key is in the Authorization header and nowhere else.
        assert SECRET not in res.output and SECRET not in caplog.text
        assert SECRET not in everything_written(env)
        assert SECRET not in request.content.decode()

    @pytest.mark.parametrize(
        "failure",
        ["down", "500", "401", "413"],
    )
    @pytest.mark.parametrize(("bug", "code"), [(None, 0), ("skip-approval", 1)])
    def test_a_failed_upload_never_changes_the_exit_code(
        self,
        monkeypatch: pytest.MonkeyPatch,
        agent: Callable[[str | None], str],
        env: Path,
        failure: str,
        bug: str | None,
        code: int,
    ) -> None:
        def answer(r: httpx.Request) -> httpx.Response:
            if failure == "down":
                raise httpx.ConnectError("refused")
            status = int(failure)
            return httpx.Response(status, json={"code": f"E{status}", "message": "no"})

        self.fake(monkeypatch, FakeServer(answer))
        res = run_cli(agent(bug))
        assert res.exit_code == code, res.output
        assert "NOT PUBLISHED opseval:server" in res.output
        result = saved_result(env / "reports")
        assert result.exit_code == code
        outcome = server_sink(result)
        assert not outcome.published and "runs import" in outcome.detail
        # Named in the gate summary and in JUnit (the copy and the run's own).
        assert "**Not published** · `opseval:server`" in markdown_summary(result)
        for xml in (env / "junit.xml", next((env / "reports").iterdir()) / "junit.xml"):
            props = {
                p.get("name"): p.get("value") for p in ET.parse(xml).getroot().iter("property")
            }
            assert props["failed_sinks"] == SERVER_SINK
            assert props["exit_code"] == str(code)
        assert SECRET not in res.output and SECRET not in everything_written(env)

    @pytest.mark.parametrize(
        "answer",
        [
            lambda r: httpx.Response(401, json={"code": "INVALID_KEY", "message": "no"}),
            lambda r: httpx.Response(404, json={"code": "NOT_FOUND", "message": "not found"}),
        ],
    )
    def test_a_failed_pull_is_exit_3_and_nothing_runs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        env: Path,
        answer: Callable[[httpx.Request], httpx.Response],
    ) -> None:
        fake = FakeServer()
        fake.pull_answer = answer
        self.fake(monkeypatch, fake)
        res = run_cli("ws://127.0.0.1:9")
        assert res.exit_code == 3, res.output
        assert not (env / "reports").exists() or not any((env / "reports").iterdir())
        assert fake.uploads == []
        assert SECRET not in res.output

    def test_an_unreachable_server_is_exit_3(
        self, monkeypatch: pytest.MonkeyPatch, env: Path
    ) -> None:
        fake = FakeServer()

        def down(r: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        fake.pull_answer = down
        self.fake(monkeypatch, fake)
        assert run_cli("ws://127.0.0.1:9").exit_code == 3

    def test_a_server_not_allowlisted_is_exit_3_before_anything_is_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = self.fake(monkeypatch, FakeServer())
        res = runner.invoke(
            app,
            [
                "run",
                "suites/smoke.yaml",
                "-t",
                "ws://127.0.0.1:9",
                "--server",
                "https://opseval.example.com/path?x=1",
                "--project",
                PROJECT,
            ],
        )
        assert res.exit_code == 3 and "opseval.example.com" in res.output
        assert "x=1" not in res.output and fake.pulls == []

    @pytest.mark.parametrize(
        ("argv", "said"),
        [
            (["--server", SERVER], "--server needs --project"),
            (["--server", SERVER, "--project", PROJECT, "--database-url", "postgresql://x"], "or"),
            (["--label", "production", "-p", "builtin:openai-chat"], "--label needs --server"),
            ([], "--profile is required"),
        ],
    )
    def test_flag_combinations_are_checked(
        self, monkeypatch: pytest.MonkeyPatch, argv: list[str], said: str
    ) -> None:
        self.fake(monkeypatch, FakeServer())
        res = runner.invoke(app, ["run", "x.yaml", "-t", "ws://127.0.0.1:9", *argv])
        assert res.exit_code == 3 and said in res.output, res.output

    def test_the_labels_profile_decides(self, monkeypatch: pytest.MonkeyPatch, env: Path) -> None:
        self.fake(monkeypatch, FakeServer())
        res = run_cli("ws://127.0.0.1:9", "-p", "builtin:openai-chat")
        assert res.exit_code == 3 and "has its own profile" in res.output

    @pytest.mark.parametrize("path", ["../x.yaml", "/etc/hosts", "suites/missing.yaml"])
    def test_the_path_must_be_inside_the_pulled_catalogue(
        self, monkeypatch: pytest.MonkeyPatch, path: str
    ) -> None:
        fake = self.fake(monkeypatch, FakeServer())
        res = runner.invoke(
            app,
            ["run", path, "-t", "ws://127.0.0.1:9", "--server", SERVER, "--project", PROJECT],
        )
        assert res.exit_code == 3 and "catalogue" in res.output
        assert fake.uploads == []

    def test_a_run_that_could_not_be_carried_out_uploads_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = self.fake(monkeypatch, FakeServer())
        # The demo agent is not running: preflight finds it unreachable (exit 3).
        res = run_cli("ws://127.0.0.1:9")
        assert res.exit_code == 3
        assert "Nothing was reported" in res.output and fake.uploads == []


# --- comparing two copies of one run --------------------------------------------------------


def test_the_server_sink_is_not_part_of_the_run(tmp_path: Path) -> None:
    from opseval.models.results import Gate

    base = SuiteResult(
        opseval_version="0",
        run_id="r",
        suite="s",
        target="t",
        profile="p",
        started_at="2026-10-30T00:00:00Z",  # type: ignore[arg-type]
        ended_at="2026-10-30T00:00:01Z",  # type: ignore[arg-type]
        tests=[],
        gate=Gate(status="pass", summary="ok"),
        exit_code=0,
    )
    told = base.model_copy(
        update={"sinks": [SinkOutcome(sink=SERVER_SINK, published=False, detail="x")]}
    )
    other = base.model_copy(
        update={"sinks": [SinkOutcome(sink="webhook", published=True, detail="x")]}
    )
    assert same_result(base, told) and not same_result(base, other)
    assert not same_result(base, base.model_copy(update={"exit_code": 1}))


def test_a_plugin_sink_named_server_keeps_its_outcome(tmp_path: Path) -> None:
    from opseval.models.results import Gate
    from opseval.plugins import check_sink_contract
    from opseval.sinks import Sink

    class Server(Sink):
        name = "server"

        async def publish(self, result: SuiteResult) -> str:
            return "sent"

    class Reserved(Sink):
        name = SERVER_SINK

        async def publish(self, result: SuiteResult) -> str:
            return "sent"

    assert check_sink_contract(Server, source="plugin 'server'") is Server
    with pytest.raises(ConfigError, match="reserved"):
        check_sink_contract(Reserved, source="plugin 'evil'")
    plugin = SinkOutcome(sink="server", published=False, detail="the plugin failed")
    result = SuiteResult(
        opseval_version="0",
        run_id="r",
        suite="s",
        target="t",
        profile="p",
        started_at="2026-10-30T00:00:00Z",  # type: ignore[arg-type]
        ended_at="2026-10-30T00:00:01Z",  # type: ignore[arg-type]
        tests=[],
        gate=Gate(status="pass", summary="ok"),
        exit_code=0,
        sinks=[plugin],
    )
    store = FileStore(tmp_path)
    ours = SinkOutcome(sink=SERVER_SINK, published=True, detail="reported")
    recorded = remote.record(store, result, ours)
    assert recorded.sinks == [plugin, ours]
    again = remote.record(store, recorded, ours)
    assert again.sinks == [plugin, ours], "only the reserved entry is replaced"
    # Comparing two copies drops only the reserved entry, never the plugin's.
    assert same_result(result, recorded)
    assert not same_result(result, result.model_copy(update={"sinks": []}))


def test_every_import_refusal_has_one_answer() -> None:
    from opseval.db.run_import import Refusal
    from opseval.server.ci_api import REFUSALS

    assert set(REFUSALS) == {r.value for r in Refusal}
    assert REFUSALS[Refusal.IMPORTING.value] == (409, "RUN_IMPORTING")
    assert REFUSALS[Refusal.RUN_EXISTS.value] == (409, "RUN_EXISTS")
    assert REFUSALS[Refusal.RUN_ID_TAKEN.value] == (409, "RUN_ID_TAKEN")


# --- against a real server on Postgres --------------------------------------------------------


def _split(header: str) -> tuple[str, str]:
    public, secret = header.removeprefix("Bearer ").split(":", 1)
    return public, secret


@pytest.mark.usefixtures("pg_url")
class TestAgainstARealServer:
    """A ``create_app`` server in accounts mode (test_accounts_db's world: project acme/bot,
    task ``default`` served, task ``other`` too, the demo catalogue under ``staging``)."""

    @pytest.fixture
    async def world(self, pg_url: str, tmp_path: Path) -> Any:
        from opseval.accounts.setup import access_config
        from opseval.blobs import FileBlobStore
        from opseval.db.run_store import DbRunStore
        from opseval.server import create_app

        from .test_accounts_db import World, prepare, settings_for

        await prepare(pg_url, tmp_path)
        settings = settings_for(pg_url, tmp_path, upload_max_mb=1)
        store = DbRunStore.connect(
            settings,
            FileBlobStore(tmp_path / "blobs"),
            project=PROJECT,
            task="default",
            task_reads=True,
        )
        access = access_config(settings, store, project=PROJECT, task="default")
        app_ = create_app(settings=settings, store=store, access=access)
        try:
            yield World(app_, store, access.accounts, [], pg_url, tmp_path)
        finally:
            store.close()

    def use(self, monkeypatch: pytest.MonkeyPatch, world: Any, key: str) -> None:
        from fastapi.testclient import TestClient

        public, secret = _split(key)
        monkeypatch.setenv("OPSEVAL_PUBLIC_KEY", public)
        monkeypatch.setenv("OPSEVAL_SECRET_KEY", secret)
        monkeypatch.setattr(
            remote, "make_client", lambda s, _: TestClient(world.app, base_url=SERVER)
        )

    async def cli(self, *argv: str) -> Any:
        async with run_agent() as url:
            return await asyncio.to_thread(
                runner.invoke,
                app,
                [
                    "run",
                    "suites/smoke.yaml",
                    "-t",
                    url,
                    "--repeats",
                    "1",
                    "--server",
                    SERVER,
                    "--project",
                    PROJECT,
                    "--label",
                    "staging",
                    *argv,
                ],
            )

    async def test_pull_run_upload_and_read_back_identically(
        self, world: Any, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITLAB_CI", "true")
        monkeypatch.setenv("CI_PROJECT_PATH", "acme/bot")
        monkeypatch.setenv("CI_COMMIT_SHA", "b" * 40)
        monkeypatch.setenv("CI_COMMIT_REF_NAME", "main")
        monkeypatch.setenv("CI_JOB_URL", "https://gitlab.example.com/acme/bot/-/jobs/9")
        self.use(monkeypatch, world, world.key("member@acme.test", ["runs:read", "runs:start"]))
        res = await self.cli()
        assert res.exit_code == 0, res.output
        local = FileStore(env / "reports")
        [run_id] = local.run_ids()
        mine = local.load_result(run_id)
        assert server_sink(mine).published

        # Read back through the DbRunStore: the run as the folder holds it.
        db = world.store
        assert same_result(db.load_result(run_id), mine)
        assert [r.model_dump() for r in db.load_records(run_id)] == [
            r.model_dump() for r in local.load_records(run_id)
        ]
        held, there = db.load_snapshot(run_id), local.load_snapshot(run_id)
        assert held is not None and there is not None
        assert held[0] == there[0] and held[1] == there[1]
        assert db.load_report(run_id) == local.load_report(run_id)
        assert db.load_manifest(run_id) == local.load_manifest(run_id)
        origin = db.origin(run_id)
        assert origin is not None and origin.kind == "ci"
        assert origin.ci["provider"] == "gitlab" and origin.ci["sha"] == "b" * 40
        assert origin.ci["run_url"] == "https://gitlab.example.com/acme/bot/-/jobs/9"
        assert origin.ci["catalogue"]["label"] == "staging"
        assert {i["kind"] for i in origin.ci["catalogue"]["items"]} >= {"suite", "profile"}

        # The console lists it like any other database run, with its origin.
        owner = world.login("owner@acme.test")
        row = next(r for r in owner.get("/api/v1/runs").json() if r["id"] == run_id)
        assert row["origin"] == "ci" and row["ci"]["provider"] == "gitlab"
        assert row["ci"]["runUrl"] == "https://gitlab.example.com/acme/bot/-/jobs/9"
        assert row["ci"]["label"] == "staging"
        detail = owner.get(f"/api/v1/runs/{run_id}").json()
        assert detail["summary"]["origin"] == "ci"
        [event] = world.events("run.upload")
        assert event.detail["outcome"] == "imported" and event.actor_kind == "key"
        assert not world.events("PUT /runs/{run_id}/upload"), "audited once, specifically"

    async def test_a_re_upload_is_a_no_op(
        self, world: Any, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key = world.key("member@acme.test", ["runs:read", "runs:start"])
        self.use(monkeypatch, world, key)
        assert (await self.cli()).exit_code == 0
        local = FileStore(env / "reports")
        [run_id] = local.run_ids()
        server = remote.server_from(Settings(), SERVER, PROJECT, "default", "staging")
        from fastapi.testclient import TestClient

        client = TestClient(world.app, base_url=SERVER)
        body = remote.encode(remote.bundle(local, run_id, ci=None, catalogue=None))
        again = remote.upload(client, server, run_id, body, retries=0)
        assert again.published and "unchanged" in again.detail
        assert [e.detail["outcome"] for e in world.events("run.upload")] == [
            "imported",
            "unchanged",
        ]
        # The folder now carries the server's sink entry; it is still the same run.
        result = local.load_result(run_id)
        assert any(s.sink == SERVER_SINK for s in result.sinks)
        # A different run under the same id is refused, never written over.
        changed = remote.bundle(local, run_id, ci=None, catalogue=None)
        assert changed.result is not None
        changed = changed.model_copy(
            update={"result": changed.result.model_copy(update={"exit_code": 1})}
        )
        refused = remote.upload(client, server, run_id, remote.encode(changed), retries=0)
        assert not refused.published and "409 RUN_EXISTS" in refused.detail
        assert world.store.load_result(run_id).exit_code == 0

    async def test_runs_import_of_the_reported_folder_is_unchanged(
        self, world: Any, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opseval.db.run_import import import_run

        self.use(monkeypatch, world, world.key("member@acme.test", ["runs:read", "runs:start"]))
        assert (await self.cli()).exit_code == 0
        local = FileStore(env / "reports")
        [run_id] = local.run_ids()
        assert import_run(local, run_id, world.store).outcome == "unchanged"

    async def test_a_reader_key_can_pull_but_not_upload(
        self, world: Any, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.use(monkeypatch, world, world.key("member@acme.test", ["runs:read"]))
        res = await self.cli()
        assert res.exit_code == 0, res.output
        assert "403 FORBIDDEN" in res.output and "NOT PUBLISHED opseval:server" in res.output
        assert world.store.run_ids() == []

    async def test_a_key_scoped_to_another_task_cannot_upload_here(
        self, world: Any, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import uuid

        from sqlalchemy.ext.asyncio import AsyncSession

        from opseval.db.catalogue_store import get_task

        async def other(s: AsyncSession) -> uuid.UUID:
            return (await get_task(s, PROJECT, "other")).id

        other_id = world.store.run_in_session(other)
        scoped = world.key("member@acme.test", ["runs:read", "runs:start"], task_ids=[other_id])
        self.use(monkeypatch, world, scoped)
        res = await self.cli()  # the pull is refused: 404, nothing to learn
        assert res.exit_code == 3 and "404 NOT_FOUND" in res.output

        # A key for this task, sending its run to another task, is 404 too.
        good = world.key("member@acme.test", ["runs:read", "runs:start"])
        self.use(monkeypatch, world, good)
        assert (await self.cli()).exit_code == 0
        local = FileStore(env / "reports")
        [run_id] = local.run_ids()
        elsewhere = remote.server_from(Settings(), SERVER, PROJECT, "other", "staging")
        from fastapi.testclient import TestClient

        body = remote.encode(remote.bundle(local, run_id, ci=None, catalogue=None))
        out = remote.upload(
            TestClient(world.app, base_url=SERVER), elsewhere, run_id, body, retries=0
        )
        assert not out.published and "404 NOT_FOUND" in out.detail
        # And the scoped key, sending this run here, is 404.
        public, secret = _split(scoped)
        monkeypatch.setenv("OPSEVAL_PUBLIC_KEY", public)
        monkeypatch.setenv("OPSEVAL_SECRET_KEY", secret)
        here = remote.server_from(Settings(), SERVER, PROJECT, "default", "staging")
        out = remote.upload(TestClient(world.app, base_url=SERVER), here, run_id, body, retries=0)
        assert not out.published and "404" in out.detail

    async def test_refusals_are_named(
        self, world: Any, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi.testclient import TestClient

        key = world.key("member@acme.test", ["runs:read", "runs:start"])
        self.use(monkeypatch, world, key)
        assert (await self.cli()).exit_code == 0
        local = FileStore(env / "reports")
        [run_id] = local.run_ids()
        world.store.delete_run(run_id)
        server = remote.server_from(Settings(), SERVER, PROJECT, "default", "staging")
        client = TestClient(world.app, base_url=SERVER)
        upload = remote.bundle(local, run_id, ci=None, catalogue=None)

        def send(body: bytes | dict[str, Any]) -> httpx.Response:
            content = body if isinstance(body, bytes) else json.dumps(body).encode()
            return client.put(
                f"/api/v1/runs/{run_id}/upload",
                params=server.params(),
                content=content,
                headers={**server.headers(), "Content-Type": "application/json"},
            )

        plain = json.loads(remote.encode(upload))
        # A layout newer than the server's: 409 before the rest is parsed.
        newer = {**plain, "layout": "opseval-runs/999", "records": [{"brand": "new"}]}
        newer["manifest"] = {**plain["manifest"], "layout": "opseval-runs/999"}
        r = send(newer)
        assert r.status_code == 409 and r.json()["code"] == "UNKNOWN_LAYOUT"
        # An unknown field anywhere: 422 (extra="forbid"), the value never echoed.
        r = send({**plain, "surprise": "s3cr3t-value"})
        assert r.status_code == 422 and "s3cr3t-value" not in r.text
        # Another run's body under this id.
        r = send({**plain, "run_id": "someone-else"})
        assert r.status_code == 422
        # An audio file whose bytes are not the ones hashed.
        bad = {
            **plain,
            "audio": [
                {
                    "path": "audio/a.wav",
                    "sha256": hashlib.sha256(b"one").hexdigest(),
                    "data": base64.b64encode(b"two").decode(),
                }
            ],
        }
        r = send(bad)
        assert r.status_code == 422 and r.json()["code"] == "CHECKSUM_MISMATCH"
        r = send({**bad, "audio": [{**bad["audio"][0], "path": "../../etc/x"}]})
        assert r.status_code == 422 and r.json()["code"] == "BAD_AUDIO_PATH"
        # Larger than OPSEVAL_UPLOAD_MAX_MB (1 MB here): 413, said before reading on.
        big = {**plain, "report": "x" * (1024 * 1024 + 1)}
        r = send(big)
        assert r.status_code == 413 and r.json()["code"] == "UPLOAD_TOO_LARGE"
        out = remote.upload(client, server, run_id, json.dumps(big).encode(), retries=3)
        assert not out.published and "413" in out.detail
        assert world.store.run_ids() == [], "nothing refused was written"
        assert len(world.events("run.upload")) == 1, "only the CLI's own upload, before"

    async def test_audio_goes_through_the_blob_store_checksummed(
        self, world: Any, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi.testclient import TestClient

        from .test_db_runs import voice_run

        key = world.key("member@acme.test", ["runs:read", "runs:start"])
        self.use(monkeypatch, world, key)
        local = FileStore(env / "reports")
        record = voice_run(local, "r-ci-voice")
        local.save_error("r-ci-voice", {"code": "X", "message": "a voice run"})
        upload = remote.bundle(local, "r-ci-voice", ci=None, catalogue=None)
        assert len(upload.audio) == 2
        server = remote.server_from(Settings(), SERVER, PROJECT, "default", "staging")
        client = TestClient(world.app, base_url=SERVER)
        out = remote.upload(client, server, "r-ci-voice", remote.encode(upload), retries=0)
        assert out.published, out.detail
        held = world.store.audio("r-ci-voice")
        for frame in record.frames:
            assert frame.audio is not None and frame.audio.path is not None
            data = local.audio("r-ci-voice").read(frame.audio.path)
            assert held.read(frame.audio.path) == data
            [(sha,)] = world.sql(
                "SELECT sha256 FROM blobs WHERE run_id = :r AND name = :n AND state = 'committed'",
                r="r-ci-voice",
                n=frame.audio.path,
            )
            assert sha == hashlib.sha256(data).hexdigest()
        assert world.store.load_error("r-ci-voice") == {"code": "X", "message": "a voice run"}

    async def local_run(self, env: Path) -> tuple[FileStore, str]:
        from opseval.runner import RunOptions, launch

        local = FileStore(env / "reports")
        cat = catalogue_dir()
        settings = Settings(_env_file=None, allowed_hosts="127.0.0.1", reports_dir=str(local.root))
        async with run_agent() as url:
            options = RunOptions(target=url, profile=cat / "profile.yaml", store=local, repeats=1)
            result = await launch(cat / "suites" / "smoke.yaml", options, settings)
        return local, result.run_id

    def sender(self, world: Any, run_id: str, body: bytes) -> Callable[[], httpx.Response]:
        from fastapi.testclient import TestClient

        public, secret = _split(world.key("member@acme.test", ["runs:read", "runs:start"]))
        headers = {"Authorization": f"Bearer {public}:{secret}", "Content-Type": "application/json"}

        def send() -> httpx.Response:
            return TestClient(world.app, base_url=SERVER).put(
                f"/api/v1/runs/{run_id}/upload",
                params={"project": PROJECT, "task": "default"},
                content=body,
                headers=headers,
            )

        return send

    async def test_two_concurrent_uploads_never_remove_the_one_in_flight(
        self, world: Any, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import concurrent.futures

        local, run_id = await self.local_run(env)
        send = self.sender(
            world, run_id, remote.encode(remote.bundle(local, run_id, ci=None, catalogue=None))
        )
        store = world.store
        entered, release = threading.Event(), threading.Event()
        original = store.save_record

        def slow(record: Any) -> str:
            entered.set()
            assert release.wait(60)
            return original(record)

        monkeypatch.setattr(store, "save_record", slow)
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            first = pool.submit(send)
            assert await asyncio.to_thread(entered.wait, 60), "the first import is under way"
            second = await asyncio.to_thread(send)
            assert second.status_code == 409, second.text
            assert second.json()["code"] == "RUN_IMPORTING"
            assert second.headers["retry-after"] == "5"
            assert store.importing(run_id), "the import in flight is untouched"
            release.set()
            done = await asyncio.to_thread(first.result, 60)
        assert done.status_code == 201, done.text
        assert not store.importing(run_id) and store.has_result(run_id)
        again = await asyncio.to_thread(send)
        assert again.status_code == 200 and again.json()["outcome"] == "unchanged"

    async def test_an_import_cut_short_is_replaced(self, world: Any, env: Path) -> None:
        local, run_id = await self.local_run(env)
        # An importer that died after its manifest: the row is importing, nobody holds the lock.
        world.store.import_manifest(run_id, local.load_manifest(run_id))
        assert world.store.importing(run_id)
        body = remote.encode(remote.bundle(local, run_id, ci=None, catalogue=None))
        r = await asyncio.to_thread(self.sender(world, run_id, body))
        assert r.status_code == 201, r.text
        assert "cut short" in r.json()["detail"]
        assert world.store.has_result(run_id) and not world.store.importing(run_id)

    async def test_the_body_is_parsed_off_the_event_loop(
        self, world: Any, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opseval.server import ci_api

        local, run_id = await self.local_run(env)
        body = remote.encode(remote.bundle(local, run_id, ci=None, catalogue=None))
        seen: dict[str, bool] = {}
        for name in ("parse_upload", "decode_audio"):
            real = getattr(ci_api, name)

            def spy(*a: Any, _real: Any = real, _name: str = name) -> Any:
                try:
                    asyncio.get_running_loop()
                    seen[_name] = False  # on the event loop
                except RuntimeError:
                    seen[_name] = True
                return _real(*a)

            monkeypatch.setattr(ci_api, name, spy)
        r = await asyncio.to_thread(self.sender(world, run_id, body))
        assert r.status_code == 201, r.text
        assert seen == {"parse_upload": True, "decode_audio": True}

    async def test_an_audit_write_that_fails_is_logged_not_a_failure(
        self,
        world: Any,
        env: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        local, run_id = await self.local_run(env)
        body = remote.encode(remote.bundle(local, run_id, ci=None, catalogue=None))
        send = self.sender(world, run_id, body)

        def broken(*a: Any, **k: Any) -> None:
            raise RuntimeError("the audit table is gone")

        monkeypatch.setattr(world.accounts, "audit", broken)
        with caplog.at_level(logging.ERROR, logger="opseval.server"):
            r = await asyncio.to_thread(send)
        assert r.status_code == 201, r.text
        assert world.store.has_result(run_id)
        assert "audit event run.upload" in caplog.text and "was not written" in caplog.text

    async def test_the_check_constraint_names_are_the_models(self, world: Any) -> None:
        import re

        from sqlalchemy.dialects import postgresql
        from sqlalchemy.schema import CreateTable

        from opseval.db.schema import Run

        ddl = str(CreateTable(Run.__table__).compile(dialect=postgresql.dialect()))  # type: ignore[arg-type]
        model = set(re.findall(r"CONSTRAINT (ck_\w+) CHECK", ddl))
        held = {
            row[0]
            for row in world.sql(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'runs'::regclass "
                "AND contype = 'c'"
            )
        }
        # 0007's own constraint, named as the model names it (op.f: no second prefix). The
        # released 0005's ck_runs_status is left as it shipped (see the report).
        assert "ck_runs_origin" in model and "ck_runs_origin" in held
        assert "ck_runs_ck_runs_origin" not in held

    async def test_migration_0007_goes_down_and_up(self, pg_url: str, tmp_path: Path) -> None:
        from alembic import command
        from sqlalchemy import text

        from opseval.db import migrate
        from opseval.db.engine import make_engine

        from .test_accounts_db import settings_for

        engine = make_engine(settings_for(pg_url, tmp_path))
        try:
            await migrate.upgrade(engine)

            def down(conn: Any) -> None:
                command.downgrade(migrate.config(conn), "0006")  # type: ignore[no-untyped-call]

            async with engine.begin() as conn:
                await conn.run_sync(down)
            assert await migrate.current(engine) == "0006"
            async with engine.connect() as conn:
                cols = await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'runs' AND column_name IN ('origin', 'ci')"
                    )
                )
                assert list(cols) == []
            await migrate.upgrade(engine)
            assert await migrate.current(engine) == "0007"
        finally:
            await engine.dispose()


def test_export_model_round_trips() -> None:
    body = export_body()
    export = CatalogueExport.model_validate(body)
    assert export.pulled().items[0].version == 1


def test_shared_token_mode_has_no_upload(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from opseval.server import create_app

    settings = Settings(_env_file=None, reports_dir=str(tmp_path))
    c = TestClient(create_app(settings=settings, token="t"), base_url=SERVER)
    r = c.put(
        "/api/v1/runs/r1/upload",
        params={"project": PROJECT, "task": "default"},
        content=b"{}",
        headers={"Authorization": "Bearer t"},
    )
    assert r.status_code == 409 and r.json()["code"] == "ACCOUNTS_MODE_ONLY"
    r = c.get(
        "/api/v1/catalogue/export",
        params={"project": PROJECT, "task": "default"},
        headers={"Authorization": "Bearer t"},
    )
    assert r.status_code == 409 and r.json()["code"] == "ACCOUNTS_MODE_ONLY"
