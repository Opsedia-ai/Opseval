# SPDX-License-Identifier: Apache-2.0
"""Dashboards and model prices (ADR-0052, ADR-0066 phase D2).

Adds what ``PgAnalytics`` queries and what the price table keeps:

- ``attempts.language``, ``dialect``, ``voice`` and ``usage_recorded``; ``verdicts.name``;
  ``usage_events.rescore`` (the re-score that made a call). Rows written before this migration
  keep them null: an unknown language, a voice flag and usage never recorded read as not
  measured, never as text or as "no calls". Re-import a run (``opseval runs import``) to fill
  them.
- ``run_usage``: a run's per-model usage rows as its result holds them (the totals of runs
  written before per-call usage).
- ``model_prices``: a project's own prices over the built-in table. An edit adds a row.
- Indexes for the dashboards: runs by project, task and start; metrics by name.

The downgrade drops them, and with them every project price.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()
PRICE_UNITS = ("tokens", "characters", "seconds")
MATCH_KINDS = ("exact", "glob")
"""Copied, not imported: a migration describes the schema as it was when it was written."""


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.add_column("attempts", sa.Column("language", sa.String(64), nullable=True))
    op.add_column("attempts", sa.Column("dialect", sa.String(64), nullable=True))
    op.add_column("attempts", sa.Column("voice", sa.Boolean(), nullable=True))
    op.add_column("attempts", sa.Column("usage_recorded", sa.Boolean(), nullable=True))
    op.add_column("verdicts", sa.Column("name", sa.Text(), nullable=True))
    op.add_column("usage_events", sa.Column("rescore", sa.String(64), nullable=True))
    op.create_index("ix_runs_project_started", "runs", ["project_id", "task_id", "started_at"])
    op.create_index("ix_attempt_metrics_name", "attempt_metrics", ["name", "attempt_id"])

    op.create_table(
        "run_usage",
        sa.Column("id", UUID, nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("task_id", UUID, nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("body", JSONB, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_run_usage"),
        sa.ForeignKeyConstraint(
            ["run_id", "project_id", "task_id"],
            ["runs.id", "runs.project_id", "runs.task_id"],
            name="fk_run_usage_run_id_runs",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_run_usage_run_id", "run_usage", ["run_id"])

    op.create_table(
        "model_prices",
        sa.Column("id", UUID, nullable=False),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("pattern", sa.String(200), nullable=False),
        sa.Column("match_kind", sa.String(8), nullable=False),
        sa.Column("unit", sa.String(16), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input", sa.Float(), nullable=True),
        sa.Column("output", sa.Float(), nullable=True),
        sa.Column("reasoning", sa.Float(), nullable=True),
        sa.Column("cached_input", sa.Float(), nullable=True),
        sa.Column("audio_in", sa.Float(), nullable=True),
        sa.Column("audio_out", sa.Float(), nullable=True),
        sa.Column("characters", sa.Float(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(320), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_model_prices"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_model_prices_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_one_of("unit", PRICE_UNITS), name="ck_model_prices_unit"),
        sa.CheckConstraint(_one_of("match_kind", MATCH_KINDS), name="ck_model_prices_match_kind"),
        sa.CheckConstraint(
            "coalesce(least(input, output, reasoning, cached_input, audio_in, audio_out, "
            "characters), 0) >= 0",
            name="ck_model_prices_not_negative",
        ),
    )
    op.create_index(
        "ix_model_prices_project_effective", "model_prices", ["project_id", "effective_from"]
    )


def downgrade() -> None:
    op.drop_index("ix_model_prices_project_effective", table_name="model_prices")
    op.drop_table("model_prices")
    op.drop_index("ix_run_usage_run_id", table_name="run_usage")
    op.drop_table("run_usage")
    op.drop_index("ix_attempt_metrics_name", table_name="attempt_metrics")
    op.drop_index("ix_runs_project_started", table_name="runs")
    op.drop_column("usage_events", "rescore")
    op.drop_column("verdicts", "name")
    for column in ("usage_recorded", "voice", "dialect", "language"):
        op.drop_column("attempts", column)
