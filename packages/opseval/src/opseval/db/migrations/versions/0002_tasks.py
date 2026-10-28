# SPDX-License-Identifier: Apache-2.0
"""Tasks inside a project (ADR-0049).

Creates ``tasks``, gives every existing project a ``default`` task (kind ``chat``, modality
``text``: the migration never guesses more), moves every catalogue item into its project's
``default`` task, links that task's profile when the project has exactly one, then makes
``task_id`` required and moves the unique key from ``(project_id, kind, name)`` to
``(task_id, kind, name)``.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
TASK_KINDS = ("chat", "agentic", "voice", "batch")
MODALITIES = ("text", "audio_file", "audio_stream", "telephony", "webrtc")
"""Copied, not imported: a migration describes the schema as it was when it was written."""


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("modality", sa.String(16), nullable=False),
        sa.Column("profile_item_id", UUID, nullable=True),
        sa.Column("default_suite_item_id", UUID, nullable=True),
        sa.Column("region", sa.String(64), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_tasks_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["profile_item_id"],
            ["catalogue_items.id"],
            name="fk_tasks_profile_item_id_catalogue_items",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["default_suite_item_id"],
            ["catalogue_items.id"],
            name="fk_tasks_default_suite_item_id_catalogue_items",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("project_id", "slug", name="uq_tasks_project_id"),
        sa.UniqueConstraint("id", "project_id", name="uq_tasks_id"),
        sa.CheckConstraint(_one_of("kind", TASK_KINDS), name="ck_tasks_kind"),
        sa.CheckConstraint(_one_of("modality", MODALITIES), name="ck_tasks_modality"),
    )
    op.create_index("ix_tasks_project_id", "tasks", ["project_id"])

    # One `default` task per existing project. Ids are made here rather than by
    # gen_random_uuid(), so the migration needs nothing beyond what 0001 needed.
    bind = op.get_bind()
    projects = [row[0] for row in bind.execute(sa.text("SELECT id FROM projects"))]
    tasks = sa.table(
        "tasks",
        sa.column("id", UUID),
        sa.column("project_id", UUID),
        sa.column("slug", sa.String),
        sa.column("name", sa.String),
        sa.column("kind", sa.String),
        sa.column("modality", sa.String),
    )
    if projects:
        op.bulk_insert(
            tasks,
            [
                {
                    "id": uuid.uuid4(),
                    "project_id": project,
                    "slug": "default",
                    "name": "default",
                    "kind": "chat",
                    "modality": "text",
                }
                for project in projects
            ],
        )

    op.add_column("catalogue_items", sa.Column("task_id", UUID, nullable=True))
    op.execute(
        "UPDATE catalogue_items AS i SET task_id = t.id FROM tasks AS t "
        "WHERE t.project_id = i.project_id AND t.slug = 'default'"
    )
    # The task's profile, only when there is exactly one to choose: never a guess.
    op.execute(
        "UPDATE tasks AS t SET profile_item_id = p.id FROM ("
        "  SELECT task_id, (array_agg(id))[1] AS id FROM catalogue_items"
        "  WHERE kind = 'profile' GROUP BY task_id HAVING count(*) = 1"
        ") AS p WHERE p.task_id = t.id"
    )
    op.alter_column("catalogue_items", "task_id", nullable=False)
    op.drop_constraint("uq_catalogue_items_project_id", "catalogue_items", type_="unique")
    op.create_unique_constraint(
        "uq_catalogue_items_task_id", "catalogue_items", ["task_id", "kind", "name"]
    )
    op.create_foreign_key(
        "fk_catalogue_items_task_id_tasks",
        "catalogue_items",
        "tasks",
        ["task_id", "project_id"],
        ["id", "project_id"],
        ondelete="CASCADE",
    )


LOST = """
SELECT o.slug || '/' || p.slug, t.slug, t.name, t.kind, t.modality, t.region,
       t.archived_at IS NOT NULL, t.default_suite_item_id IS NOT NULL,
       t.profile_item_id IS NOT NULL AND t.profile_item_id IS DISTINCT FROM (
         SELECT (array_agg(i.id))[1] FROM catalogue_items AS i
         WHERE i.project_id = t.project_id AND i.kind = 'profile' HAVING count(*) = 1
       )
FROM tasks AS t
JOIN projects AS p ON p.id = t.project_id
JOIN organizations AS o ON o.id = p.organization_id
ORDER BY 1, 2
"""
"""Every task, with what of it the schema before tasks cannot hold. The profile link counts
only when an upgrade would not make it again (the project's one profile)."""


def _lost(row: Sequence[Any]) -> str | None:
    project, slug, name, kind, modality, region, archived, suite, profile = row
    if slug != "default":
        return f"{project}: task {slug!r} and its catalogue"
    settings = [f"name {name!r}"] if name != "default" else []
    settings += [f"kind {kind}"] if kind != "chat" else []
    settings += [f"modality {modality}"] if modality != "text" else []
    settings += [f"region {region}"] if region is not None else []
    settings += ["archived"] if archived else []
    settings += ["a default suite"] if suite else []
    settings += ["its profile link"] if profile else []
    return f"{project}: the default task's {', '.join(settings)}" if settings else None


def downgrade() -> None:
    # Reversible only while every task is a `default` one as the upgrade made it: a second
    # task's items could share a name the old key forbids, and the old schema has no place for
    # a task's name, kind, modality, region, archive, default suite or a profile link the
    # upgrade would not make again. Refused, listing them, before anything is dropped.
    # (This guard was added after 0002 was released. Editing a released downgrade is safe: it
    # runs from the installed package and never changes what 0002's upgrade produced. A
    # released upgrade is never edited.)
    lost = [line for row in op.get_bind().execute(sa.text(LOST)) if (line := _lost(row))]
    if lost:
        listed = "\n".join(f"  - {line}" for line in lost)
        raise RuntimeError(
            f"the schema before tasks cannot hold these, so nothing was downgraded:\n{listed}"
        )
    op.drop_constraint("fk_catalogue_items_task_id_tasks", "catalogue_items", type_="foreignkey")
    op.drop_constraint("uq_catalogue_items_task_id", "catalogue_items", type_="unique")
    op.create_unique_constraint(
        "uq_catalogue_items_project_id", "catalogue_items", ["project_id", "kind", "name"]
    )
    op.drop_column("catalogue_items", "task_id")
    op.drop_table("tasks")
