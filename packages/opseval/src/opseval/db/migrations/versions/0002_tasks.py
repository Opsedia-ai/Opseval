# SPDX-License-Identifier: Apache-2.0
"""Tasks inside a project (ADR-0049).

Creates ``tasks``, gives every existing project a ``default`` task (kind ``chat``, modality
``text``: the migration never guesses more), moves every catalogue item into its project's
``default`` task, links that task's profile when the project has exactly one, then makes
``task_id`` required and moves the unique key from ``(project_id, kind, name)`` to
``(task_id, kind, name)``. A task's profile and default suite are keyed by ``(item, task)``,
so they can only name the task's own items, and a trigger checks their kind.

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


LINKS = {"profile_item_id": "profile", "default_suite_item_id": "suite"}
"""A task's links to its own items, and the kind each must have."""
LINK_KINDS = "opseval_task_link_kinds"
LINK_KINDS_FUNCTION = """
CREATE FUNCTION opseval_task_link_kinds() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM catalogue_items WHERE id = NEW.profile_item_id AND kind <> 'profile')
  THEN
    RAISE EXCEPTION '%: tasks.profile_item_id must name an item of kind profile',
      'ck_tasks_profile_item_kind'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'ck_tasks_profile_item_kind';
  END IF;
  IF EXISTS (
    SELECT 1 FROM catalogue_items WHERE id = NEW.default_suite_item_id AND kind <> 'suite'
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
task's links change once per push). An item's kind never changes, so tasks are enough."""


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
    # A task links only its own items, and only of the right kind. The key is (item, task),
    # so another task's item cannot be named; NO ACTION, because Postgres before 15 cannot
    # null one column of a composite key, and an item is only ever deleted with its task.
    op.create_unique_constraint("uq_catalogue_items_id", "catalogue_items", ["id", "task_id"])
    for column in LINKS:
        op.create_foreign_key(
            f"fk_tasks_{column}_catalogue_items",
            "tasks",
            "catalogue_items",
            [column, "id"],
            ["id", "task_id"],
        )
    op.execute(LINK_KINDS_FUNCTION)
    op.execute(
        f"CREATE TRIGGER {LINK_KINDS} BEFORE INSERT OR UPDATE OF "
        f"{', '.join(LINKS)} ON tasks FOR EACH ROW EXECUTE FUNCTION {LINK_KINDS}()"
    )


LOST = """
SELECT o.slug || '/' || p.slug, t.slug, t.kind, t.modality, t.region,
       t.archived_at IS NOT NULL, t.default_suite_item_id IS NOT NULL
FROM tasks AS t
JOIN projects AS p ON p.id = t.project_id
JOIN organizations AS o ON o.id = p.organization_id
WHERE t.slug <> 'default' OR t.kind <> 'chat' OR t.modality <> 'text'
   OR t.region IS NOT NULL OR t.archived_at IS NOT NULL OR t.default_suite_item_id IS NOT NULL
ORDER BY 1, 2
"""


def _lost(row: Sequence[Any]) -> str:
    project, slug, kind, modality, region, archived, suite = row
    if slug != "default":
        return f"{project}: task {slug!r} and its catalogue"
    settings = [f"kind {kind}"] if kind != "chat" else []
    settings += [f"modality {modality}"] if modality != "text" else []
    settings += [f"region {region}"] if region is not None else []
    settings += ["archived"] if archived else []
    settings += ["a default suite"] if suite else []
    return f"{project}: the default task's {', '.join(settings)}"


def downgrade() -> None:
    # Reversible only while every task is a `default` one with the settings the upgrade gave
    # it: two tasks' items could share a name the old key forbids, and the old schema has no
    # place for a kind, modality, region, archive or default suite. Refused, never dropped.
    bind = op.get_bind()
    lost = [_lost(row) for row in bind.execute(sa.text(LOST))]
    if lost:
        listed = "\n".join(f"  - {line}" for line in lost)
        raise RuntimeError(
            f"the schema before tasks cannot hold these, so nothing was downgraded:\n{listed}"
        )
    op.drop_constraint("fk_catalogue_items_task_id_tasks", "catalogue_items", type_="foreignkey")
    op.drop_table("tasks")  # its links to catalogue_items and its trigger go with it
    op.execute(f"DROP FUNCTION {LINK_KINDS}()")
    op.drop_constraint("uq_catalogue_items_id", "catalogue_items", type_="unique")
    op.drop_constraint("uq_catalogue_items_task_id", "catalogue_items", type_="unique")
    op.create_unique_constraint(
        "uq_catalogue_items_project_id", "catalogue_items", ["project_id", "kind", "name"]
    )
    op.drop_column("catalogue_items", "task_id")
