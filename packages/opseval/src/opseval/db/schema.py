# SPDX-License-Identifier: Apache-2.0
"""The server-mode schema (ADR-0047): tenancy and the versioned catalogue.

Organisation → project, as in Langfuse; every project-scoped row carries ``project_id``. A
catalogue item (a scenario, a suite or a profile) has immutable, numbered versions, and labels
that point at one version each, the way Langfuse versions prompts: ``production`` is what a run
fetches when it names no label, and ``latest`` is never stored, it is always the newest version.

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


class CatalogueItem(Base):
    """One scenario, suite or profile, by the name it is known by in the project."""

    __tablename__ = "catalogue_items"
    __table_args__ = (
        UniqueConstraint("project_id", "kind", "name"),
        CheckConstraint("kind IN ('scenario', 'suite', 'profile')", name="kind"),
    )

    id: Mapped[uuid.UUID] = _id()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
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
