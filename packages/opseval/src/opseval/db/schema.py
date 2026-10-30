# SPDX-License-Identifier: Apache-2.0
"""The server-mode schema (ADR-0047): tenancy, the versioned catalogue, and runs (ADR-0051).

Organisation → project → task. Organisation and project are Langfuse's; every project-scoped
row carries ``project_id``. A task (ADR-0049) is one evaluation target inside a project: one
agent endpoint (its profile) and one modality, with its own catalogue. A catalogue item (a
scenario, a suite or a profile) belongs to one task and has immutable, numbered versions, and
labels that point at one version each, the way Langfuse versions prompts: ``production`` is
what a run fetches when it names no label, and ``latest`` is the catalogue as last pushed.

Migrations in ``migrations/versions`` create exactly this; a test compares the two, so they
cannot drift. The one piece a foreign key cannot say, the kind of a task's linked items, is a
trigger: :func:`install_link_kinds` creates it, from ``metadata.create_all`` (an event on the
``tasks`` table) and from migration ``0003`` alike.

Deleting rows: a task's links to its items are ``NO ACTION`` keys, so an item a task links (its
profile or default suite) cannot be deleted on its own; clear the link first. Nothing in the
store deletes an item today: items go only with their task or project, which removes both rows
in one statement.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Connection,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

KINDS = ("scenario", "suite", "profile", "dataset")
"""What a catalogue item can be: the files a catalogue folder holds (datasets: ADR-0060)."""
TASK_KINDS = ("chat", "agentic", "voice", "batch")
"""What a task's agent does (ADR-0049)."""
MODALITIES = ("text", "audio_file", "audio_stream", "telephony", "webrtc")
"""How Opseval talks to a task's agent (ADR-0049)."""


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


LINK_KINDS = "opseval_task_link_kinds"
"""The trigger (and its function) that checks the kind of a task's linked items."""
LINK_COLUMNS = {"profile_item_id": "profile", "default_suite_item_id": "suite"}
"""A task's links to its own items, and the kind each must have."""
_LINK_KINDS_FUNCTION = """
CREATE FUNCTION {schema}.opseval_task_link_kinds() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM {schema}.catalogue_items WHERE id = NEW.profile_item_id AND kind <> 'profile'
  ) THEN
    RAISE EXCEPTION '%: tasks.profile_item_id must name an item of kind profile',
      'ck_tasks_profile_item_kind'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'ck_tasks_profile_item_kind';
  END IF;
  IF EXISTS (
    SELECT 1 FROM {schema}.catalogue_items WHERE id = NEW.default_suite_item_id AND kind <> 'suite'
  ) THEN
    RAISE EXCEPTION '%: tasks.default_suite_item_id must name an item of kind suite',
      'ck_tasks_default_suite_item_kind'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'ck_tasks_default_suite_item_kind';
  END IF;
  RETURN NEW;
END
$$
"""
"""The kind half of the links: a foreign key cannot say it, and a trigger is cheap here (a
task's links change once per push). An item's kind never changes, so tasks are enough.

The function's own ``search_path`` is the catalogue only, and every table is named with the
schema the tables were created in: a caller's ``search_path`` can never point it at a look-alike
``catalogue_items``. As it stands this is what migration ``0003`` installs; changing it needs a
new migration, never an edit here alone."""


def _schema(connection: Connection) -> str:
    """The schema the tables are being created in, quoted for SQL."""
    schema = connection.scalar(text("SELECT current_schema()"))
    if not schema:
        raise RuntimeError("no current schema (an empty search_path): cannot create the tables")
    return str(connection.dialect.identifier_preparer.quote_identifier(schema))


def install_link_kinds(connection: Connection) -> None:
    """Create the kind trigger on ``tasks`` in the current schema."""
    schema = _schema(connection)
    connection.execute(text(_LINK_KINDS_FUNCTION.format(schema=schema)))
    connection.execute(
        text(
            f"CREATE TRIGGER {LINK_KINDS} BEFORE INSERT OR UPDATE OF {', '.join(LINK_COLUMNS)} "
            f"ON {schema}.tasks FOR EACH ROW EXECUTE FUNCTION {schema}.{LINK_KINDS}()"
        )
    )


def drop_link_kinds(connection: Connection) -> None:
    """Remove what :func:`install_link_kinds` created."""
    schema = _schema(connection)
    connection.execute(text(f"DROP TRIGGER IF EXISTS {LINK_KINDS} ON {schema}.tasks"))
    connection.execute(text(f"DROP FUNCTION IF EXISTS {schema}.{LINK_KINDS}()"))


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = _id()
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = _created()


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("organization_id", "slug"),
        # The target of project_memberships' (project, organization) key (ADR-0050), so a
        # membership can never name a project of another organisation.
        UniqueConstraint("id", "organization_id"),
    )

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = _created()


class Task(Base):
    """One evaluation target inside a project (ADR-0049): an agent endpoint and a modality,
    with that target's scenarios, suites, runs and labels. Never hard-deleted while a run
    points at it; ``archived_at`` hides it instead."""

    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("project_id", "slug"),
        # The target of catalogue_items' (task_id, project_id) key, so an item can never sit
        # in one project's row and another project's task.
        UniqueConstraint("id", "project_id"),
        CheckConstraint(_one_of("kind", TASK_KINDS), name="kind"),
        CheckConstraint(_one_of("modality", MODALITIES), name="modality"),
        # A task links only its own items: (item, this task) must be an item of this task.
        # NO ACTION rather than SET NULL: Postgres before 15 cannot null one column of a
        # composite key, and nothing deletes an item except with its task (the cascade
        # removes both rows in one statement, which NO ACTION checks only at its end).
        # The item's kind is checked by a trigger (install_link_kinds, attached below).
        ForeignKeyConstraint(
            ["profile_item_id", "id"],
            ["catalogue_items.id", "catalogue_items.task_id"],
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["default_suite_item_id", "id"],
            ["catalogue_items.id", "catalogue_items.task_id"],
            use_alter=True,
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    modality: Mapped[str] = mapped_column(String(16), nullable=False)
    profile_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    """The task's profile (an item of this task, of kind ``profile``); set by a push,
    ``None`` until the task has a catalogue."""
    default_suite_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    """The suite (an item of this task, of kind ``suite``) a run uses when it names none."""
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """Where the task's data may live (matters once audio is stored, ADR-0053)."""
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()


def _after_create(_target: object, connection: Connection, **_kw: Any) -> None:
    install_link_kinds(connection)


def _before_drop(_target: object, connection: Connection, **_kw: Any) -> None:
    drop_link_kinds(connection)


event.listen(Task.__table__, "after_create", _after_create)
event.listen(Task.__table__, "before_drop", _before_drop)


ITEM_NAME_MAX = 200
"""``catalogue_items.name``: a push checks every item against it before writing."""
ITEM_PATH_MAX = 500
"""``catalogue_versions.path``."""


class CatalogueItem(Base):
    """One scenario, suite, profile or dataset (ADR-0060), by the name it is known by in its
    task. A dataset's name is its path (``datasets/orders.csv``)."""

    __tablename__ = "catalogue_items"
    __table_args__ = (
        UniqueConstraint("task_id", "kind", "name"),
        # The target of the tasks' (item, task) links above.
        UniqueConstraint("id", "task_id"),
        CheckConstraint(_one_of("kind", KINDS), name="kind"),
        ForeignKeyConstraint(
            ["task_id", "project_id"], ["tasks.id", "tasks.project_id"], ondelete="CASCADE"
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    """Kept next to ``task_id`` for tenancy checks and indexes; the key above keeps the two
    consistent."""
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(ITEM_NAME_MAX), nullable=False)
    """A scenario's id, a suite's name, a profile's name, a dataset's path."""
    created_at: Mapped[datetime] = _created()


class CatalogueVersion(Base):
    """An immutable version: the file exactly as written, and where it lives in a folder."""

    __tablename__ = "catalogue_versions"
    __table_args__ = (UniqueConstraint("item_id", "version"),)

    id: Mapped[uuid.UUID] = _id()
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("catalogue_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(String(ITEM_PATH_MAX), nullable=False)
    """Where the file goes when the catalogue is written out (``scenarios/llm/x.yaml``), so
    a suite's ``include`` globs select the same files they did in the folder."""
    source: Mapped[str] = mapped_column(Text, nullable=False)
    """The YAML as written, comments included: what is validated, run and written back."""
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    """The same file parsed, for queries. ``source`` is the truth."""
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    """sha256 of ``path`` and ``source``: a push of an unchanged file adds no version."""
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = _created()


class CatalogueLabel(Base):
    """A label pointing at one version of an item (``production``, ``staging``, …)."""

    __tablename__ = "catalogue_labels"

    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("catalogue_items.id", ondelete="CASCADE"), primary_key=True
    )
    label: Mapped[str] = mapped_column(String(36), primary_key=True)
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("catalogue_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# --- runs and blobs (ADR-0051) ---------------------------------------------------------------

RUN_STATUSES = ("running", "finished", "harness_error", "interrupted", "deleting")
SCORE_STATUSES = ("passed", "failed", "infra_error")
VERDICT_STATUSES = ("passed", "failed", "inconclusive", "skipped_precondition", "blocked")
BLOB_KINDS = ("record", "result", "report", "junit", "audio", "transcript", "other")
BLOB_STATES = ("pending", "committed")
RUN_ID_MAX = 128
TEST_ID_MAX = 200


class Run(Base):
    """One run (ADR-0051): the id ``FileStore`` mints, its task, and what queries need. The
    run's own files (result, report, JUnit, records) are blobs; the result blob is the truth
    a reader sees, the columns exist for queries. Counts are null until it finished: an
    unfinished run is never drawn as zeros."""

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(_one_of("status", RUN_STATUSES), name="status"),
        # A run is in its task's project: (task, project) must be a task of that project.
        ForeignKeyConstraint(["task_id", "project_id"], ["tasks.id", "tasks.project_id"]),
        # The target of the child tables' (run, project, task) keys.
        UniqueConstraint("id", "project_id", "task_id"),
        # The dashboards pick a project's (or a task's) runs by date (ADR-0052, migration 0008).
        Index("ix_runs_project_started", "project_id", "task_id", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(RUN_ID_MAX), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    suite: Mapped[str] = mapped_column(String(ITEM_NAME_MAX), nullable=False, default="")
    layout: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gate_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    passed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unjudged: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flaky: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    progress: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """The runner's progress log (``progress.json`` in a folder)."""
    cancel: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """A request to stop (``cancel.json``): who asked and when."""
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """Why the run could not be carried out (``error.json``)."""
    profile_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The SUT profile as run, as written (offline re-scoring)."""
    importing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    """Set while ``opseval runs import`` writes the run, cleared in the transaction that
    finishes it: a run left ``importing`` was interrupted, and the next import replaces it."""
    result_blob_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("blobs.id", use_alter=True, ondelete="RESTRICT"), nullable=True
    )
    report_blob_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("blobs.id", use_alter=True, ondelete="RESTRICT"), nullable=True
    )
    junit_blob_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("blobs.id", use_alter=True, ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RunScenario(Base):
    """A scenario exactly as it ran (the snapshot ``opseval rescore`` reads)."""

    __tablename__ = "run_scenarios"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    scenario_id: Mapped[str] = mapped_column(String(TEST_ID_MAX), primary_key=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)


class Blob(Base):
    """A stored object (ADR-0051). Written behind a ``pending`` row: the row first, then the
    object, then ``committed`` in the transaction that writes the row referring to it, so a
    crash leaves a pending row retention finds, never a row pointing at nothing."""

    __tablename__ = "blobs"
    __table_args__ = (
        CheckConstraint(_one_of("kind", BLOB_KINDS), name="kind"),
        CheckConstraint(_one_of("state", BLOB_STATES), name="state"),
        CheckConstraint("bytes >= 0", name="bytes"),
        ForeignKeyConstraint(
            ["run_id", "project_id", "task_id"],
            ["runs.id", "runs.project_id", "runs.task_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("store", "key"),
    )

    id: Mapped[uuid.UUID] = _id()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[str] = mapped_column(String(RUN_ID_MAX), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(ITEM_PATH_MAX), nullable=False)
    """What the object is to its run, as a run folder would name it (``result.json``,
    ``records/a__attempt0.json``, ``audio/a/attempt0/001-agent.wav.enc``)."""
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    media_type: Mapped[str] = mapped_column(String(100), nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    store: Mapped[str] = mapped_column(String(16), nullable=False)
    """``file`` or ``s3``: which blob store holds it."""
    location: Mapped[str] = mapped_column(String(ITEM_PATH_MAX), nullable=False)
    """Where that store kept it (an absolute folder, or ``host/bucket/prefix``): a reader on
    another location reports the mismatch, never the object as simply missing."""
    key: Mapped[str] = mapped_column(String(ITEM_PATH_MAX), nullable=False)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    encryption: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    """``aes-256-gcm`` (sealed by Opseval: audio, always), ``sse:AES256`` (by the store), or
    ``none``."""
    key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    legal_hold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _created()


class Attempt(Base):
    """One attempt's record (ADR-0051): the record itself is a blob, the row what queries
    need. ``passed`` is the attempt verdict: null until scored, or when not judged."""

    __tablename__ = "attempts"
    __table_args__ = (
        UniqueConstraint("run_id", "test_id", "attempt_index"),
        ForeignKeyConstraint(
            ["run_id", "project_id", "task_id"],
            ["runs.id", "runs.project_id", "runs.task_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("attempt_index >= 0", name="attempt_index"),
    )

    id: Mapped[uuid.UUID] = _id()
    run_id: Mapped[str] = mapped_column(String(RUN_ID_MAX), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    test_id: Mapped[str] = mapped_column(String(TEST_ID_MAX), nullable=False)
    attempt_index: Mapped[int] = mapped_column(Integer, nullable=False)
    terminal_state: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trace_coverage: Mapped[str | None] = mapped_column(String(16), nullable=True)
    redaction_applied: Mapped[bool] = mapped_column(Boolean, nullable=False)
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    invalid_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """The persona's language as declared (the dashboards' filter, migration 0008)."""
    dialect: Mapped[str | None] = mapped_column(String(64), nullable=True)
    voice: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    """A spoken attempt (its record has a voice timeline). Null on rows written before 0008."""
    usage_recorded: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    """The record carries its usage events (``()`` included): false is "not measured"; null on
    rows written before 0008, read as not measured too."""
    record_blob_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )


class ScoreRow(Base):
    """One ``ScoreResult``. The check repeats the model's rule: a score is null exactly when
    the status is ``infra_error``, so a harness problem can never be stored as a 0."""

    __tablename__ = "scores"
    __table_args__ = (
        CheckConstraint(_one_of("status", SCORE_STATUSES), name="status"),
        CheckConstraint("(score IS NULL) = (status = 'infra_error')", name="infra_error_score"),
    )

    id: Mapped[uuid.UUID] = _id()
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    scorer_id: Mapped[str] = mapped_column(String(200), nullable=False)
    scorer_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    axis: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    vacuous: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(100), nullable=True)


class VerdictRow(Base):
    """One ``TestVerdict``: a scenario's folded verdict in a run."""

    __tablename__ = "verdicts"
    __table_args__ = (
        UniqueConstraint("run_id", "test_id"),
        CheckConstraint(_one_of("status", VERDICT_STATUSES), name="status"),
        ForeignKeyConstraint(
            ["run_id", "project_id", "task_id"],
            ["runs.id", "runs.project_id", "runs.task_id"],
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    run_id: Mapped[str] = mapped_column(String(RUN_ID_MAX), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    test_id: Mapped[str] = mapped_column(String(TEST_ID_MAX), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    skip_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    flaky: Mapped[bool] = mapped_column(Boolean, nullable=False)
    cut_short_of: Mapped[int | None] = mapped_column(Integer, nullable=True)
    criticality: Mapped[str] = mapped_column(String(16), nullable=False)
    pass_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The scenario's name as it ran (the verdict matrix); null on rows written before 0008."""


class AttemptMetric(Base):
    """A raw measurement (ADR-0051, ADR-0055): one row per turn for a per-turn metric
    (``turn`` set), one per attempt otherwise. Not measured is ``value`` null with a
    ``reason``, never 0; percentiles are computed from these rows, never stored."""

    __tablename__ = "attempt_metrics"
    __table_args__ = (
        CheckConstraint("value IS NOT NULL OR reason IS NOT NULL", name="unmeasured_reason"),
        Index("ix_attempt_metrics_name", "name", "attempt_id"),
    )

    id: Mapped[uuid.UUID] = _id()
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    turn: Mapped[int | None] = mapped_column(Integer, nullable=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class UsageEventRow(Base):
    """One LLM, TTS or STT call (ADR-0066): an attempt's (``attempt_id`` set) or the run's,
    outside any attempt. A count the provider did not report is null, never 0, and a cost is
    null exactly when its source is ``unknown``."""

    __tablename__ = "usage_events"
    __table_args__ = (
        CheckConstraint("(cost_usd IS NULL) = (cost_source = 'unknown')", name="cost_source"),
        ForeignKeyConstraint(
            ["run_id", "project_id", "task_id"],
            ["runs.id", "runs.project_id", "runs.task_id"],
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    run_id: Mapped[str] = mapped_column(String(RUN_ID_MAX), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(200), nullable=True)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    answered: Mapped[bool] = mapped_column(Boolean, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cached_input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    audio_in_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    audio_out_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    characters: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    rate_limited: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_source: Mapped[str] = mapped_column(String(32), nullable=False)
    price_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    rescore: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """The re-score (its stamp) that made this call; null for the run's own calls. The
    dashboards count only a run's latest re-score (migration 0008)."""


class RunUsageRow(Base):
    """A run's usage rows (``SuiteResult.usage``), one per ``(role, model)``, as written: the
    dashboards read them for runs written before per-call usage (migration 0008)."""

    __tablename__ = "run_usage"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "project_id", "task_id"],
            ["runs.id", "runs.project_id", "runs.task_id"],
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    run_id: Mapped[str] = mapped_column(String(RUN_ID_MAX), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    """The ``ModelUsage`` row as the result holds it."""


PRICE_UNITS = ("tokens", "characters", "seconds")
MATCH_KINDS = ("exact", "glob")
"""How a project's price row names its models: one name, or a glob with ``*`` (never a
regular expression: a pattern typed in a browser must not be able to hang the server)."""


class ModelPriceRow(Base):
    """A project's own price (ADR-0066 D2), over the built-in table shipped in
    ``pricing/builtin.yaml``. An edit adds a row; nothing rewrites or deletes one, so a run's
    cost can always be traced to the row in force when its calls were made."""

    __tablename__ = "model_prices"
    __table_args__ = (
        CheckConstraint(_one_of("unit", PRICE_UNITS), name="unit"),
        CheckConstraint(_one_of("match_kind", MATCH_KINDS), name="match_kind"),
        CheckConstraint(
            "coalesce(least(input, output, reasoning, cached_input, audio_in, audio_out, "
            "characters), 0) >= 0",
            name="not_negative",
        ),
        Index("ix_model_prices_project_effective", "project_id", "effective_from"),
    )

    id: Mapped[uuid.UUID] = _id()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    pattern: Mapped[str] = mapped_column(String(200), nullable=False)
    match_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    input: Mapped[float | None] = mapped_column(Float, nullable=True)
    output: Mapped[float | None] = mapped_column(Float, nullable=True)
    reasoning: Mapped[float | None] = mapped_column(Float, nullable=True)
    cached_input: Mapped[float | None] = mapped_column(Float, nullable=True)
    audio_in: Mapped[float | None] = mapped_column(Float, nullable=True)
    audio_out: Mapped[float | None] = mapped_column(Float, nullable=True)
    characters: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    """Who added it (a user's id or a key's public id), as the audit event says."""
    created_at: Mapped[datetime] = _created()


# --- accounts and access (ADR-0050) ----------------------------------------------------------

ROLES = ("viewer", "member", "admin", "owner")
"""Organisation roles, lowest first. A project membership may only lower one (in core)."""
SCOPES = ("runs:read", "runs:start", "catalogue:write", "catalogue:label", "label:protected")
"""What an API key may be granted. ``label:protected`` is never implied by another scope."""
ACTOR_KINDS = ("user", "key", "anonymous", "cli")
CREATOR_KINDS = ("user", "cli")
"""Who made an API key: a person (``created_by`` names them; their role caps the key), or
``opseval admin`` on the command line (a system key only, ``created_by`` null by design)."""


class User(Base):
    """A person. The email is stored case-folded; it is never the join key of a sign-in
    (``identities`` is). ``password_hash`` is argon2id, null for an OIDC-only user."""

    __tablename__ = "users"
    __table_args__ = (CheckConstraint("email = lower(email)", name="email_folded"),)

    id: Mapped[uuid.UUID] = _id()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()


class Identity(Base):
    """An OIDC identity, ``(issuer, subject)``, linked to one user."""

    __tablename__ = "identities"
    __table_args__ = (UniqueConstraint("issuer", "subject"),)

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    issuer: Mapped[str] = mapped_column(String(500), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = _created()


class UserSession(Base):
    """A signed-in browser. ``id`` is the sha256 of the cookie's token: the token itself is
    never stored. A session ends at ``expires_at`` (absolute) or when idle too long."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    csrf_secret: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = _created()
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    address: Mapped[str | None] = mapped_column(String(64), nullable=True)


class OrgMembership(Base):
    __tablename__ = "org_memberships"
    __table_args__ = (CheckConstraint(_one_of("role", ROLES), name="role"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = _created()


class ProjectMembership(Base):
    """A role for one project, lower than the user's organisation role. A raise is refused
    when written, and the effective role is the lower of the two whatever the row says."""

    __tablename__ = "project_memberships"
    __table_args__ = (
        CheckConstraint(_one_of("role", ROLES), name="role"),
        ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["org_memberships.organization_id", "org_memberships.user_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "organization_id"],
            ["projects.id", "projects.organization_id"],
            ondelete="CASCADE",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = _created()


class ApiKey(Base):
    """A project's API key: ``public_key`` (``pk-oe-…``) names it, the secret (``sk-oe-…``) is
    shown once and kept only as its sha256. A system key (Opseval's own automation) can never
    hold ``label:protected``: the database refuses it too."""

    __tablename__ = "api_keys"
    __table_args__ = (
        CheckConstraint(
            "scopes <@ ARRAY[" + ", ".join(repr(s) for s in SCOPES) + "]::varchar[]",
            name="scopes",
        ),
        CheckConstraint("NOT (system AND 'label:protected' = ANY(scopes))", name="system"),
        CheckConstraint(_one_of("creator_kind", CREATOR_KINDS), name="creator_kind"),
        # A person's key names them; a command-line key is a system key and names nobody.
        CheckConstraint(
            "(creator_kind = 'user' AND created_by IS NOT NULL) "
            "OR (creator_kind = 'cli' AND created_by IS NULL AND system)",
            name="creator",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    public_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    """The first characters of the secret, to tell keys apart in a list."""
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(32)), nullable=False)
    task_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )
    """The tasks it may touch; null means every task of the project."""
    system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    creator_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    """Whose role caps it, checked at each use. Deleting the person deletes their keys; a
    disabled person's, or one without a role, hold nothing."""
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProtectedLabel(Base):
    """A label an admin protected in a task (ADR-0050): only an admin or owner, or a key
    holding ``label:protected``, may move it. Kept by name, whether or not any item carries the
    label yet. ``production`` is protected without a row (``accounts.roles.DEFAULT_PROTECTED``)."""

    __tablename__ = "protected_labels"

    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    label: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = _created()


class AuditEvent(Base):
    """Who did what, where and when: every write and every sign-in (ADR-0050). The ids are
    plain columns, not keys, so an event outlives what it names. The viewer is ``ee/``."""

    __tablename__ = "audit_events"
    __table_args__ = (CheckConstraint(_one_of("actor_kind", ACTOR_KINDS), name="actor_kind"),)

    id: Mapped[uuid.UUID] = _id()
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(320), nullable=True)
    """A user's id, a key's public id, or the email a failed sign-in gave."""
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str | None] = mapped_column(String(500), nullable=True)
    address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
