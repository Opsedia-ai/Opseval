# SPDX-License-Identifier: Apache-2.0
"""What ``opseval run --server`` sends and receives (ADR-0058 E2).

Two exchanges with the server, both over its HTTP API with a project key:

- **Pull** a catalogue label: :class:`CatalogueExport`, the files exactly as stored.
- **Upload** the finished run: :class:`RunUpload`, the run folder's content as the run wrote
  it, with the catalogue versions that were pulled and where the CI job ran.

Every model is ``extra="forbid"``: the server refuses a field it does not know rather than
drop it, and a run is checked by the same models every reader uses.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from opseval.models.profile import SutProfile
from opseval.models.progress import RunProgress
from opseval.models.record import RunRecord
from opseval.models.results import SuiteResult
from opseval.models.scenario import Scenario

RUN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
"""A run id that names a run folder (``storage.RUN_NAME``)."""
SHA256_PATTERN = r"^[0-9a-f]{64}$"
SERVER_SINK = "opseval:server"
"""The ``result.sinks`` entry that says whether the run reached the server. It is added after
the upload, so two copies of one run differ only in it (see :func:`same_result`). Reserved:
the ``opseval:`` prefix is refused for a plugin sink (``plugins.check_sink_contract``), so a
plugin sink named ``server`` keeps its own outcome."""
RESERVED_SINK_PREFIX = "opseval:"

CiProvider = Literal["github", "gitlab", "jenkins", "azure", "circleci", "bitbucket"]
Short = Annotated[str, Field(min_length=1, max_length=300)]


def http_url(value: str) -> str:
    """``value`` when it is an absolute http(s) URL without user-info, else ``ValueError``:
    the console links to it, so ``javascript:`` or a URL carrying a password never gets in."""
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
        raise ValueError("an absolute http(s) URL, without user-info")
    if any(c.isspace() for c in value):
        raise ValueError("a URL has no white space")
    return value


class CiInfo(BaseModel):
    """Where a CI run came from, as the CI system's own variables said. Never a secret: a
    repository, a ref, a commit and a link to the job."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: CiProvider
    repository: Short | None = None
    ref: Short | None = None
    sha: Annotated[str, Field(pattern=r"^[0-9a-fA-F]{7,64}$")] | None = None
    run_url: Annotated[str, Field(max_length=2000)] | None = None

    @field_validator("run_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        return None if value is None else http_url(value)


class PulledItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["scenario", "suite", "profile", "dataset"]
    name: Annotated[str, Field(min_length=1, max_length=200)]
    version: Annotated[int, Field(ge=1)]


class PulledCatalogue(BaseModel):
    """The catalogue versions a CI run was evaluated with."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    project: Short
    task: Short
    label: Short
    items: list[PulledItem]


class ExportedFile(PulledItem):
    path: Annotated[str, Field(min_length=1, max_length=500)]
    source: str


class CatalogueExport(BaseModel):
    """A label's files, exactly as stored (``GET /api/v1/catalogue/export``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    project: Short
    task: Short
    label: Short
    files: list[ExportedFile]

    def pulled(self) -> PulledCatalogue:
        return PulledCatalogue(
            project=self.project,
            task=self.task,
            label=self.label,
            items=[PulledItem(kind=f.kind, name=f.name, version=f.version) for f in self.files],
        )


class AudioUpload(BaseModel):
    """One audio file of the run (sealed, ADR-0053), base64, with its sha256: the server
    refuses it when the bytes it decoded are not the bytes that were hashed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: Annotated[str, Field(min_length=1, max_length=500)]
    sha256: Annotated[str, Field(pattern=SHA256_PATTERN)]
    data: str


class RunUpload(BaseModel):
    """A finished run, as its folder holds it (``PUT /api/v1/runs/{run_id}/upload``).

    Re-scores are not sent: a run that has just finished has none, and ``opseval runs import``
    carries a folder's later re-scores."""

    model_config = ConfigDict(extra="forbid")

    layout: Annotated[str, Field(min_length=1, max_length=64)]
    """The run's layout; the server refuses one newer than its own before reading the rest."""
    run_id: Annotated[str, Field(pattern=RUN_ID_PATTERN)]
    manifest: dict[str, Any]
    profile: SutProfile | None = None
    scenarios: list[Scenario] = Field(default_factory=list)
    records: list[RunRecord] = Field(default_factory=list)
    progress: RunProgress | None = None
    cancel_requested: bool = False
    result: SuiteResult | None = None
    error: dict[str, Any] | None = None
    report: str | None = None
    junit: str | None = None
    audio: list[AudioUpload] = Field(default_factory=list)
    audio_marker: str | None = None
    ci: CiInfo | None = None
    catalogue: PulledCatalogue | None = None

    @model_validator(mode="after")
    def _consistent(self) -> RunUpload:
        if self.manifest.get("layout") != self.layout:
            raise ValueError("layout must be the manifest's layout")
        if (self.result is None) == (self.error is None):
            raise ValueError("a finished run has a result or an error, exactly one")
        if self.result is not None and self.result.run_id != self.run_id:
            raise ValueError("the result is another run's")
        if any(r.run_id != self.run_id for r in self.records):
            raise ValueError("a record is another run's")
        return self


class UploadOut(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    outcome: Literal["imported", "unchanged"]
    detail: str


def same_result(a: SuiteResult, b: SuiteResult) -> bool:
    """Two results of one run, compared without the :data:`SERVER_SINK` entry: the CLI adds
    it to its own copy once the upload is answered, so the copy on the server never has it
    and a re-upload (or ``runs import`` of the folder) is still the same run."""

    def plain(r: SuiteResult) -> dict[str, Any]:
        body = r.model_dump()
        body["sinks"] = [s for s in body["sinks"] if s["sink"] != SERVER_SINK]
        return body

    return plain(a) == plain(b)


__all__ = [
    "RUN_ID_PATTERN",
    "RESERVED_SINK_PREFIX",
    "SERVER_SINK",
    "AudioUpload",
    "CatalogueExport",
    "CiInfo",
    "CiProvider",
    "ExportedFile",
    "PulledCatalogue",
    "PulledItem",
    "RunUpload",
    "UploadOut",
    "http_url",
    "same_result",
]
