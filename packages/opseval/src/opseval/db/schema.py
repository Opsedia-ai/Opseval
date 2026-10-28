# SPDX-License-Identifier: Apache-2.0
"""The server-mode schema (ADR-0047): tenancy and the versioned catalogue.

Organisation → project → task. Organisation and project are Langfuse's; every project-scoped
row carries ``project_id``. A task (ADR-0049) is one evaluation target inside a project: one
agent endpoint (its profile) and one modality, with its own catalogue. A catalogue item (a
scenario, a suite or a profile) belongs to one task and has immutable, numbered versions, and
labels that point at one version each, the way Langfuse versions prompts: ``production`` is
what a run fetches when it names no label, and ``latest`` is the catalogue as last pushed.

Migrations in ``migrations/versions`` create exactly this; a test compares the two, so they
cannot drift.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

KINDS = ("scenario", "suite", "profile")
"""What a catalogue item can be. The same three files a catalogue folder holds."""
TASK_KINDS = ("chat", "agentic", "voice", "batch")
"""What a task's agent does (ADR-0049)."""
MODALITIES = ("text", "audio_file", "audio_stream", "telephony", "webrtc")
"""How Opseval talks to a task's agent (ADR-0049)."""


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


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
    __table_args__ = (UniqueConstraint("organization_id", "slug"),)

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
        # The item's kind is checked by a trigger the migration installs.
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


class CatalogueItem(Base):
    """One scenario, suite or profile, by the name it is known by in its task."""

    __tablename__ = "catalogue_items"
    __table_args__ = (
        UniqueConstraint("task_id", "kind", "name"),
        # The target of the tasks' (item, task) links above.
        UniqueConstraint("id", "task_id"),
        CheckConstraint("kind IN ('scenario', 'suite', 'profile')", name="kind"),
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
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    """A scenario's id, a suite's name, a profile's name."""
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
    path: Mapped[str] = mapped_column(String(500), nullable=False)
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
