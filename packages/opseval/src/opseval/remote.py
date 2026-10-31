# SPDX-License-Identifier: Apache-2.0
"""``opseval run --server``: pull a labelled catalogue, run locally, report the run (ADR-0058 E2).

The client half, over the server's HTTP API with a project key (ADR-0050). It needs neither
the database nor the ``server`` extra, so the CLI image and a plain ``pip install opseval``
can report to a server.

The rules, in the order a run meets them:

- **The server URL** is allowlisted, with TLS unless loopback, before the key is sent
  anywhere. A refused host is exit 3. Errors name the scheme and host only.
- **Pull** writes the label into a new, empty folder with ``catalogue pull``'s own rules
  (:mod:`opseval.catalogue_files`). Any failure is a :class:`~opseval.errors.ConfigError`
  (exit 3): nothing could be evaluated.
- **Upload** sends the finished run folder. It is retried on what may pass (a connection
  error, a timeout, 408, 425, 429, any 5xx, 409 ``RUN_IMPORTING``), never on a refusal, and
  it never raises: the outcome is a ``result.sinks`` entry named
  :data:`~opseval.models.upload.SERVER_SINK`, written to the run's ``result.json`` and JUnit,
  and the exit code stays the local verdict's.

The key is sent only in the ``Authorization`` header and never appears in an error, a log
line or a file.
"""

from __future__ import annotations

import base64
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from pydantic import SecretStr, ValidationError

from opseval.catalogue_files import StoredFile, write_label
from opseval.config import CiEnvironment, Settings, check_url_allowed
from opseval.errors import ConfigError
from opseval.models.results import SinkOutcome, SuiteResult
from opseval.models.upload import (
    SERVER_SINK,
    AudioUpload,
    CatalogueExport,
    CiInfo,
    PulledCatalogue,
    RunUpload,
    UploadOut,
)
from opseval.reporting.junit import render_junit
from opseval.storage import LEGACY_LAYOUT, FileStore

API = "/api/v1"
RETRY_STATUSES = frozenset({408, 425, 429})
"""Answers below 500 an upload is tried again on; every 5xx is tried again too, and a 409
``RUN_IMPORTING`` (another try of this run is still being stored). Any other refusal (401,
403, 404, 409, 413, 422) is final: trying again would only be refused the same way."""
RETRY_CODES = frozenset({"RUN_IMPORTING"})


def retryable(response: httpx.Response) -> bool:
    """Whether an answer may pass on another try."""
    status = response.status_code
    if 500 <= status < 600 or status in RETRY_STATUSES:
        return True
    if status == 409:
        try:
            body = response.json()
        except ValueError:
            return False
        return isinstance(body, dict) and body.get("code") in RETRY_CODES
    return False


BACKOFF_SECONDS = 2.0
"""The first wait between upload tries; each later one doubles, capped at :data:`MAX_WAIT`."""
MAX_WAIT = 30.0


@dataclass(frozen=True)
class Server:
    """Where ``run --server`` reports to, checked."""

    url: str
    """The base URL, without a trailing slash."""
    project: str
    task: str
    label: str
    public_key: str
    secret_key: SecretStr

    @property
    def origin(self) -> str:
        """``scheme://host``: all an error ever says about the server."""
        return origin(self.url)

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.public_key}:{self.secret_key.get_secret_value()}"}

    def params(self) -> dict[str, str]:
        return {"project": self.project, "task": self.task}


def origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.hostname or ''}"


def server_from(settings: Settings, url: str, project: str, task: str, label: str) -> Server:
    """The server to report to, or :class:`ConfigError` (exit 3) before anything is sent: the
    host allowlisted with TLS unless loopback, and both halves of the key set."""
    check_url_allowed(url, settings.allowlist(), secure_schemes=("https",))
    parsed = urlparse(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError(
            f"--server {origin(url)}: give the server's base URL only (no user-info, query or "
            "fragment); the key goes in OPSEVAL_PUBLIC_KEY and OPSEVAL_SECRET_KEY"
        )
    public = (settings.public_key or "").strip()
    secret = settings.secret_key
    if not public or secret is None or not secret.get_secret_value().strip():
        raise ConfigError(
            "--server needs a project API key: set OPSEVAL_PUBLIC_KEY and OPSEVAL_SECRET_KEY "
            "(a key with runs:read to pull and runs:start to upload, ADR-0050)"
        )
    if any(c.isspace() or c == ":" for c in public):
        raise ConfigError("OPSEVAL_PUBLIC_KEY is not a public key (pk-oe-…)")
    if not label.strip():
        raise ConfigError("--label must name a catalogue label")
    return Server(
        url=url.rstrip("/"),
        project=project,
        task=task,
        label=label,
        public_key=public,
        secret_key=SecretStr(secret.get_secret_value().strip()),
    )


def make_client(server: Server, settings: Settings) -> httpx.Client:
    """The HTTP client both requests use. Redirects are not followed: a redirect would carry
    the key to wherever it points, past the allowlist."""
    return httpx.Client(
        base_url=server.url,
        timeout=settings.server_timeout_seconds,
        follow_redirects=False,
        headers={"User-Agent": "opseval-cli"},
    )


def _refusal(response: httpx.Response) -> str:
    """``HTTP 403 FORBIDDEN: …`` from an API error body, cut short; never the request."""
    code = message = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        code = str(body.get("code", ""))[:64]
        message = str(body.get("message", ""))[:300]
    text = f"HTTP {response.status_code}"
    if code:
        text += f" {code}"
    if message:
        text += f": {message}"
    return text


def _unreachable(server: Server, exc: httpx.HTTPError) -> str:
    # The exception's own text can name the URL; its type is enough and names nothing else.
    return f"{server.origin} could not be reached ({type(exc).__name__})"


# --- pull ------------------------------------------------------------------------------------


def pull(client: httpx.Client, server: Server, dest: Path) -> PulledCatalogue:
    """Write the label's files into ``dest`` (new or empty) and return what was pulled. Any
    failure is a :class:`ConfigError`: the run cannot start (exit 3)."""
    where = f"{server.project}/{server.task} under {server.label!r} from {server.origin}"
    try:
        response = client.get(
            f"{API}/catalogue/export",
            params={**server.params(), "label": server.label},
            headers=server.headers(),
        )
    except httpx.HTTPError as exc:
        raise ConfigError(f"pulling {where}: {_unreachable(server, exc)}") from None
    if response.status_code != 200:
        raise ConfigError(f"pulling {where}: {_refusal(response)}")
    try:
        export = CatalogueExport.model_validate_json(response.content)
    except ValidationError as exc:
        first = exc.errors()[0].get("msg", "invalid") if exc.errors() else "invalid"
        raise ConfigError(
            f"pulling {where}: the answer is not a catalogue export ({first})"
        ) from None
    if (export.label, export.task) != (server.label, server.task):
        raise ConfigError(f"pulling {where}: the server answered with another task or label")
    write_label(
        dest,
        [StoredFile(f.kind, f.name, f.path, f.source) for f in export.files],
        server.label,
        f"{server.project}/{server.task}",
    )
    return export.pulled()


# --- CI metadata -----------------------------------------------------------------------------


def _strip_userinfo(value: str | None) -> str | None:
    """A repository URL without ``user:token@``: a Jenkins ``GIT_URL`` can carry one."""
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme and parsed.hostname:
        host = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
        return parsed._replace(netloc=host, query="", fragment="").geturl()
    return value if "@" not in value.split("/", 1)[0] else value.split("@", 1)[1]


def _true(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def ci_metadata(env: CiEnvironment | None = None) -> CiInfo | None:
    """Where this CI job runs, from its system's variables; ``None`` outside CI. A value that
    does not pass :class:`CiInfo`'s checks (a URL that is not http(s), a malformed commit) is
    left out, never sent."""
    e = env if env is not None else CiEnvironment()
    fields: dict[str, str | None]
    if _true(e.github_actions):
        server = (e.github_server_url or "https://github.com").rstrip("/")
        url = None
        if e.github_repository and e.github_run_id:
            url = f"{server}/{e.github_repository}/actions/runs/{e.github_run_id}"
            if e.github_run_attempt and e.github_run_attempt.isdigit():
                url += f"/attempts/{e.github_run_attempt}"
        provider = "github"
        fields = {
            "repository": e.github_repository,
            "ref": e.github_ref,
            "sha": e.github_sha,
            "run_url": url,
        }
    elif _true(e.gitlab_ci):
        provider = "gitlab"
        fields = {
            "repository": e.ci_project_path,
            "ref": e.ci_commit_ref_name,
            "sha": e.ci_commit_sha,
            "run_url": e.ci_job_url,
        }
    elif _true(e.tf_build):
        url = None
        if e.system_collectionuri and e.system_teamproject and e.build_buildid:
            base = e.system_collectionuri.rstrip("/")
            url = (
                f"{base}/{quote(e.system_teamproject)}/_build/results?"
                f"buildId={quote(e.build_buildid)}"
            )
        provider = "azure"
        fields = {
            "repository": e.build_repository_name,
            "ref": e.build_sourcebranch,
            "sha": e.build_sourceversion,
            "run_url": url,
        }
    elif _true(e.circleci):
        repo = None
        if e.circle_project_username and e.circle_project_reponame:
            repo = f"{e.circle_project_username}/{e.circle_project_reponame}"
        provider = "circleci"
        fields = {
            "repository": repo,
            "ref": e.circle_branch or e.circle_tag,
            "sha": e.circle_sha1,
            "run_url": e.circle_build_url,
        }
    elif e.bitbucket_build_number:
        url = None
        if e.bitbucket_git_http_origin:
            url = f"{e.bitbucket_git_http_origin.rstrip('/')}/pipelines/results/"
            url += quote(e.bitbucket_build_number)
        provider = "bitbucket"
        fields = {
            "repository": e.bitbucket_repo_full_name,
            "ref": e.bitbucket_branch or e.bitbucket_tag,
            "sha": e.bitbucket_commit,
            "run_url": url,
        }
    elif e.jenkins_url:
        provider = "jenkins"
        fields = {
            "repository": _strip_userinfo(e.git_url),
            "ref": e.git_branch,
            "sha": e.git_commit,
            "run_url": e.build_url,
        }
    else:
        return None
    kept: dict[str, str] = {}
    for name, value in fields.items():
        if not value or not value.strip():
            continue
        try:
            CiInfo.model_validate({"provider": provider, name: value.strip()})
        except ValidationError:
            continue
        kept[name] = value.strip()
    return CiInfo.model_validate({"provider": provider, **kept})


# --- upload ----------------------------------------------------------------------------------


def bundle(
    store: FileStore,
    run_id: str,
    *,
    ci: CiInfo | None,
    catalogue: PulledCatalogue | None,
) -> RunUpload:
    """The finished run folder as the server takes it, read through :class:`FileStore` (so
    it is checked as every reader checks it)."""
    store.check_layout(run_id)
    manifest = store.load_manifest(run_id)
    layout = str(manifest.get("layout", LEGACY_LAYOUT))
    manifest = {**manifest, "layout": layout}
    snapshot = store.load_snapshot(run_id)
    records = store.load_records(run_id)
    audio: list[AudioUpload] = []
    files = store.audio(run_id)
    seen: set[str] = set()
    for record in records:
        for frame in record.frames:
            ref = frame.audio
            if ref is None or ref.path is None or ref.pruned or ref.path in seen:
                continue
            if files.size(ref.path) is None:
                continue  # pruned or missing: the record says so, the reader reports it
            seen.add(ref.path)
            data = files.read(ref.path)
            audio.append(
                AudioUpload(
                    path=ref.path,
                    sha256=hashlib.sha256(data).hexdigest(),
                    data=base64.b64encode(data).decode("ascii"),
                )
            )
    result = store.load_result(run_id) if store.has_result(run_id) else None
    return RunUpload(
        layout=layout,
        run_id=run_id,
        manifest=manifest,
        profile=snapshot[0] if snapshot is not None else None,
        scenarios=list(snapshot[1].values()) if snapshot is not None else [],
        records=records,
        progress=store.load_progress(run_id),
        cancel_requested=store.cancel_requested(run_id),
        result=result,
        error=None if result is not None else store.load_error(run_id),
        report=store.load_report(run_id),
        junit=store.load_junit(run_id),
        audio=audio,
        audio_marker=store.audio_marker_text(run_id),
        ci=ci,
        catalogue=catalogue,
    )


def encode(upload: RunUpload) -> bytes:
    return upload.model_dump_json(by_alias=True).encode("utf-8")


def _wait(response: httpx.Response | None, attempt: int) -> float:
    wait: float = min(BACKOFF_SECONDS * (2**attempt), MAX_WAIT)
    if response is not None:
        after = str(response.headers.get("retry-after", ""))
        if after.isdigit():
            wait = min(float(after), MAX_WAIT)
    return wait


def upload(
    client: httpx.Client,
    server: Server,
    run_id: str,
    body: bytes,
    *,
    retries: int,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = lambda _m: None,
) -> SinkOutcome:
    """Send the run; the outcome, never an exception. Idempotent by run id: a second upload
    of the same run is answered ``unchanged``, so a try whose answer was lost is safe."""
    path = f"{API}/runs/{quote(run_id, safe='')}/upload"
    last = ""
    tries = retries + 1
    for attempt in range(tries):
        response: httpx.Response | None = None
        try:
            response = client.put(
                path,
                params=server.params(),
                content=body,
                headers={**server.headers(), "Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            last = _unreachable(server, exc)
        else:
            if response.status_code in (200, 201):
                try:
                    out = UploadOut.model_validate_json(response.content)
                except ValidationError:
                    last = f"HTTP {response.status_code}, but the answer is not an upload receipt"
                    break
                said = "already there, unchanged" if out.outcome == "unchanged" else "stored"
                return SinkOutcome(
                    sink=SERVER_SINK,
                    published=True,
                    detail=f"run {run_id} reported to {server.origin} "
                    f"({server.project}/{server.task}): {said}",
                )
            last = _refusal(response)
            if not retryable(response):
                break
        if attempt + 1 < tries:
            wait = _wait(response, attempt)
            log(f"upload to {server.origin}: {last}; trying again in {wait:.0f} s")
            sleep(wait)
    else:
        last += f" (after {tries} tries)"
    return SinkOutcome(
        sink=SERVER_SINK,
        published=False,
        detail=f"run {run_id} NOT reported to {server.origin}: {last}. The run folder is "
        "kept; `opseval runs import` can send it later",
    )


def record(store: FileStore, result: SuiteResult, outcome: SinkOutcome) -> SuiteResult:
    """The result with the upload's outcome among its sinks, written back with its JUnit, so
    a failed upload is in ``result.json`` (and every summary read from it), not only in the
    job log. The verdict and exit code are untouched."""
    kept = [s for s in result.sinks if s.sink != SERVER_SINK]
    updated = result.model_copy(update={"sinks": [*kept, outcome]})
    try:
        store.save_junit(result.run_id, render_junit(updated))
        store.save_result(updated)
    except OSError:
        pass  # the summary still names the outcome; the verdict on disk is unchanged
    return updated


def report_run(
    client: httpx.Client,
    server: Server,
    store: FileStore,
    result: SuiteResult,
    *,
    ci: CiInfo | None,
    catalogue: PulledCatalogue | None,
    retries: int,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = lambda _m: None,
) -> SuiteResult:
    """Upload the finished run and record the outcome. Never raises for the server's sake:
    whatever goes wrong, the local verdict stands and the failure is named."""
    try:
        body = encode(bundle(store, result.run_id, ci=ci, catalogue=catalogue))
    except (ConfigError, ValidationError, ValueError, OSError) as exc:
        outcome = SinkOutcome(
            sink=SERVER_SINK,
            published=False,
            detail=f"run {result.run_id} NOT reported to {server.origin}: the run folder could "
            f"not be read for upload ({type(exc).__name__})",
        )
    else:
        outcome = upload(client, server, result.run_id, body, retries=retries, sleep=sleep, log=log)
    return record(store, result, outcome)


__all__ = [
    "API",
    "RETRY_STATUSES",
    "retryable",
    "Server",
    "bundle",
    "ci_metadata",
    "encode",
    "make_client",
    "origin",
    "pull",
    "record",
    "report_run",
    "server_from",
    "upload",
]
