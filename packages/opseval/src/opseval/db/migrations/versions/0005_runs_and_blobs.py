# SPDX-License-Identifier: Apache-2.0
"""Runs and blobs in the database (ADR-0051).

Creates ``runs`` (each in a task of its project), ``run_scenarios`` (the snapshot a re-score
reads), ``blobs`` (objects in the blob store, written behind a ``pending`` row), ``attempts``,
``scores`` (a score is null exactly when the status is ``infra_error``), ``verdicts``,
``attempt_metrics`` (not measured is a null value with a reason, never 0) and ``usage_events``
(ADR-0066). Only new tables: nothing that exists changes, so the upgrade takes no lock on a busy
table.

The downgrade drops them, and with them every run stored in the database; it refuses, changing
nothing, while any run exists.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()
RUN_STATUSES = ("running", "finished", "harness_error", "interrupted", "deleting")
SCORE_STATUSES = ("passed", "failed", "infra_error")
VERDICT_STATUSES = ("passed", "failed", "inconclusive", "skipped_precondition", "blocked")
BLOB_KINDS = ("record", "result", "report", "junit", "audio", "transcript", "other")
BLOB_STATES = ("pending", "committed")
"""Copied, not imported: a migration describes the schema as it was when it was written."""


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _created(name: str = "created_at") -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


RUN_KEY = (["run_id", "project_id", "task_id"], ["runs.id", "runs.project_id", "runs.task_id"])


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.String(128), nullable=False),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("task_id", UUID, nullable=False),
        sa.Column("suite", sa.String(200), nullable=False),
        sa.Column("layout", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("gate_state", sa.String(16), nullable=True),
        sa.Column("passed", sa.Integer(), nullable=True),
        sa.Column("failed", sa.Integer(), nullable=True),
        sa.Column("unjudged", sa.Integer(), nullable=True),
        sa.Column("flaky", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("manifest", JSONB, nullable=False),
        sa.Column("progress", JSONB, nullable=True),
        sa.Column("cancel", JSONB, nullable=True),
        sa.Column("error", JSONB, nullable=True),
        sa.Column("profile_snapshot", sa.Text(), nullable=True),
        sa.Column("importing", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("result_blob_id", UUID, nullable=True),
        sa.Column("report_blob_id", UUID, nullable=True),
        sa.Column("junit_blob_id", UUID, nullable=True),
        _created(),
        _created("updated_at"),
        sa.PrimaryKeyConstraint("id", name="pk_runs"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="fk_runs_project_id_projects", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "project_id"],
            ["tasks.id", "tasks.project_id"],
            name="fk_runs_task_id_tasks",
        ),
        sa.UniqueConstraint("id", "project_id", "task_id", name="uq_runs_id"),
        sa.CheckConstraint(_one_of("status", RUN_STATUSES), name="ck_runs_status"),
    )
    op.create_index("ix_runs_project_id", "runs", ["project_id"])
    op.create_index("ix_runs_task_id", "runs", ["task_id"])

    op.create_table(
        "run_scenarios",
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("scenario_id", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "scenario_id", name="pk_run_scenarios"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name="fk_run_scenarios_run_id_runs", ondelete="CASCADE"
        ),
    )

    op.create_table(
        "blobs",
        sa.Column("id", UUID, nullable=False),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("task_id", UUID, nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("store", sa.String(16), nullable=False),
        sa.Column("location", sa.String(500), nullable=False),
        sa.Column("key", sa.String(500), nullable=False),
        sa.Column("region", sa.String(64), nullable=True),
        sa.Column("encryption", sa.String(32), nullable=False),
        sa.Column("key_id", sa.String(64), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("legal_hold", sa.Boolean(), nullable=False),
        _created(),
        sa.PrimaryKeyConstraint("id", name="pk_blobs"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_blobs_project_id_projects",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(*RUN_KEY, name="fk_blobs_run_id_runs", ondelete="RESTRICT"),
        sa.UniqueConstraint("store", "key", name="uq_blobs_store"),
        sa.CheckConstraint(_one_of("kind", BLOB_KINDS), name="ck_blobs_kind"),
        sa.CheckConstraint(_one_of("state", BLOB_STATES), name="ck_blobs_state"),
        sa.CheckConstraint("bytes >= 0", name="ck_blobs_bytes"),
    )
    op.create_index("ix_blobs_project_id", "blobs", ["project_id"])
    op.create_index("ix_blobs_run_id", "blobs", ["run_id"])
    for column in ("result_blob_id", "report_blob_id", "junit_blob_id"):
        op.create_foreign_key(
            f"fk_runs_{column}_blobs", "runs", "blobs", [column], ["id"], ondelete="RESTRICT"
        )

    op.create_table(
        "attempts",
        sa.Column("id", UUID, nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("task_id", UUID, nullable=False),
        sa.Column("test_id", sa.String(200), nullable=False),
        sa.Column("attempt_index", sa.Integer(), nullable=False),
        sa.Column("terminal_state", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trace_coverage", sa.String(16), nullable=True),
        sa.Column("redaction_applied", sa.Boolean(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("invalid_reason", sa.Text(), nullable=True),
        sa.Column("record_blob_id", UUID, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_attempts"),
        sa.ForeignKeyConstraint(*RUN_KEY, name="fk_attempts_run_id_runs", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["record_blob_id"],
            ["blobs.id"],
            name="fk_attempts_record_blob_id_blobs",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("run_id", "test_id", "attempt_index", name="uq_attempts_run_id"),
        sa.CheckConstraint("attempt_index >= 0", name="ck_attempts_attempt_index"),
    )
    op.create_index("ix_attempts_run_id", "attempts", ["run_id"])

    op.create_table(
        "scores",
        sa.Column("id", UUID, nullable=False),
        sa.Column("attempt_id", UUID, nullable=False),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("scorer_id", sa.String(200), nullable=False),
        sa.Column("scorer_version", sa.String(64), nullable=True),
        sa.Column("axis", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("degraded", sa.Boolean(), nullable=False),
        sa.Column("vacuous", sa.Boolean(), nullable=False),
        sa.Column("reason_code", sa.String(100), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_scores"),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempts.id"],
            name="fk_scores_attempt_id_attempts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_scores_project_id_projects",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(_one_of("status", SCORE_STATUSES), name="ck_scores_status"),
        sa.CheckConstraint(
            "(score IS NULL) = (status = 'infra_error')", name="ck_scores_infra_error_score"
        ),
    )
    op.create_index("ix_scores_attempt_id", "scores", ["attempt_id"])
    op.create_index("ix_scores_project_id", "scores", ["project_id"])

    op.create_table(
        "verdicts",
        sa.Column("id", UUID, nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("task_id", UUID, nullable=False),
        sa.Column("test_id", sa.String(200), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("skip_kind", sa.String(32), nullable=True),
        sa.Column("flaky", sa.Boolean(), nullable=False),
        sa.Column("cut_short_of", sa.Integer(), nullable=True),
        sa.Column("criticality", sa.String(16), nullable=False),
        sa.Column("pass_rate", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_verdicts"),
        sa.ForeignKeyConstraint(*RUN_KEY, name="fk_verdicts_run_id_runs", ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "test_id", name="uq_verdicts_run_id"),
        sa.CheckConstraint(_one_of("status", VERDICT_STATUSES), name="ck_verdicts_status"),
    )
    op.create_index("ix_verdicts_run_id", "verdicts", ["run_id"])

    op.create_table(
        "attempt_metrics",
        sa.Column("id", UUID, nullable=False),
        sa.Column("attempt_id", UUID, nullable=False),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("turn", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(32), nullable=True),
        sa.Column("degraded", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_attempt_metrics"),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempts.id"],
            name="fk_attempt_metrics_attempt_id_attempts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_attempt_metrics_project_id_projects",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "value IS NOT NULL OR reason IS NOT NULL", name="ck_attempt_metrics_unmeasured_reason"
        ),
    )
    op.create_index("ix_attempt_metrics_attempt_id", "attempt_metrics", ["attempt_id"])
    op.create_index("ix_attempt_metrics_project_id", "attempt_metrics", ["project_id"])

    op.create_table(
        "usage_events",
        sa.Column("id", UUID, nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("task_id", UUID, nullable=False),
        sa.Column("attempt_id", UUID, nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(200), nullable=True),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("answered", sa.Boolean(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("reasoning_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cached_input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("audio_in_seconds", sa.Float(), nullable=True),
        sa.Column("audio_out_seconds", sa.Float(), nullable=True),
        sa.Column("characters", sa.BigInteger(), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("rate_limited", sa.Integer(), nullable=False),
        sa.Column("retries", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("cost_source", sa.String(32), nullable=False),
        sa.Column("price_from", sa.Date(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_usage_events"),
        sa.ForeignKeyConstraint(*RUN_KEY, name="fk_usage_events_run_id_runs", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempts.id"],
            name="fk_usage_events_attempt_id_attempts",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "(cost_usd IS NULL) = (cost_source = 'unknown')", name="ck_usage_events_cost_source"
        ),
    )
    op.create_index("ix_usage_events_run_id", "usage_events", ["run_id"])
    op.create_index("ix_usage_events_attempt_id", "usage_events", ["attempt_id"])


def downgrade() -> None:
    count = op.get_bind().scalar(sa.text("SELECT count(*) FROM runs"))
    if count:
        raise RuntimeError(
            f"{count} run(s) are stored, which revision 0004 cannot hold; nothing was "
            "downgraded. Delete them first, or stay at 0005"
        )
    for column in ("result_blob_id", "report_blob_id", "junit_blob_id"):
        op.drop_constraint(f"fk_runs_{column}_blobs", "runs", type_="foreignkey")
    for table in (
        "usage_events",
        "attempt_metrics",
        "verdicts",
        "scores",
        "attempts",
        "blobs",
        "run_scenarios",
        "runs",
    ):
        op.drop_table(table)
