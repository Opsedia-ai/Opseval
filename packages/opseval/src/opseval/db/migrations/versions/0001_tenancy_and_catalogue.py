# SPDX-License-Identifier: Apache-2.0
"""Tenancy and the versioned catalogue (ADR-0047).

Revision ID: 0001
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def _created() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        _created(),
        sa.PrimaryKeyConstraint("id", name="pk_organizations"),
        sa.UniqueConstraint("slug", name="uq_organizations_slug"),
    )
    op.create_table(
        "projects",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        _created(),
        sa.PrimaryKeyConstraint("id", name="pk_projects"),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_projects_organization_id_organizations",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("organization_id", "slug", name="uq_projects_organization_id"),
    )
    op.create_index("ix_projects_organization_id", "projects", ["organization_id"])
    op.create_table(
        "catalogue_items",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        _created(),
        sa.PrimaryKeyConstraint("id", name="pk_catalogue_items"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_catalogue_items_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("project_id", "kind", "name", name="uq_catalogue_items_project_id"),
        sa.CheckConstraint(
            "kind IN ('scenario', 'suite', 'profile')", name="ck_catalogue_items_kind"
        ),
    )
    op.create_index("ix_catalogue_items_project_id", "catalogue_items", ["project_id"])
    op.create_table(
        "catalogue_versions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("item_id", UUID, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("path", sa.String(500), nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("content", postgresql.JSONB, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("message", sa.Text, nullable=True),
        sa.Column("created_by", sa.String(200), nullable=True),
        _created(),
        sa.PrimaryKeyConstraint("id", name="pk_catalogue_versions"),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["catalogue_items.id"],
            name="fk_catalogue_versions_item_id_catalogue_items",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("item_id", "version", name="uq_catalogue_versions_item_id"),
    )
    op.create_index("ix_catalogue_versions_item_id", "catalogue_versions", ["item_id"])
    op.create_table(
        "catalogue_labels",
        sa.Column("item_id", UUID, nullable=False),
        sa.Column("label", sa.String(36), nullable=False),
        sa.Column("version_id", UUID, nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("item_id", "label", name="pk_catalogue_labels"),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["catalogue_items.id"],
            name="fk_catalogue_labels_item_id_catalogue_items",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["catalogue_versions.id"],
            name="fk_catalogue_labels_version_id_catalogue_versions",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_catalogue_labels_version_id", "catalogue_labels", ["version_id"])


def downgrade() -> None:
    op.drop_table("catalogue_labels")
    op.drop_table("catalogue_versions")
    op.drop_table("catalogue_items")
    op.drop_table("projects")
    op.drop_table("organizations")
