# SPDX-License-Identifier: Apache-2.0
"""Accounts and access (ADR-0050).

Creates ``users``, ``identities`` (OIDC ``(issuer, subject)``, never joined by email),
``sessions`` (only the hash of a session token is stored), ``org_memberships``,
``project_memberships`` (a role that may only lower the organisation role), ``api_keys`` (the
secret kept only as its hash; a system key can never hold ``label:protected``) and
``audit_events`` and ``protected_labels`` (a label protected in a task by name, whether or not
an item carries it yet). Adds a unique ``(id, organization_id)`` on ``projects`` for the
memberships' key, the only change to an existing table.

The downgrade drops the tables, and with them every account; it refuses, changing nothing,
while any user exists.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()
ROLES = ("viewer", "member", "admin", "owner")
SCOPES = ("runs:read", "runs:start", "catalogue:write", "catalogue:label", "label:protected")
ACTOR_KINDS = ("user", "key", "anonymous", "cli")
CREATOR_KINDS = ("user", "cli")
"""Copied, not imported: a migration describes the schema as it was when it was written."""


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _created(name: str = "created_at") -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_unique_constraint("uq_projects_id", "projects", ["id", "organization_id"])

    op.create_table(
        "users",
        sa.Column("id", UUID, nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        _created(),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.CheckConstraint("email = lower(email)", name="ck_users_email_folded"),
    )

    op.create_table(
        "identities",
        sa.Column("id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("issuer", sa.String(500), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        _created(),
        sa.PrimaryKeyConstraint("id", name="pk_identities"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_identities_user_id_users", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("issuer", "subject", name="uq_identities_issuer"),
    )
    op.create_index("ix_identities_user_id", "identities", ["user_id"])

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("csrf_secret", sa.String(64), nullable=False),
        _created(),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("address", sa.String(64), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_sessions_user_id_users", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    op.create_table(
        "org_memberships",
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        _created(),
        sa.PrimaryKeyConstraint("organization_id", "user_id", name="pk_org_memberships"),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_org_memberships_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_org_memberships_user_id_users", ondelete="CASCADE"
        ),
        sa.CheckConstraint(_one_of("role", ROLES), name="ck_org_memberships_role"),
    )
    op.create_index("ix_org_memberships_user_id", "org_memberships", ["user_id"])

    op.create_table(
        "project_memberships",
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        _created(),
        sa.PrimaryKeyConstraint("project_id", "user_id", name="pk_project_memberships"),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["org_memberships.organization_id", "org_memberships.user_id"],
            name="fk_project_memberships_organization_id_org_memberships",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "organization_id"],
            ["projects.id", "projects.organization_id"],
            name="fk_project_memberships_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_one_of("role", ROLES), name="ck_project_memberships_role"),
    )
    op.create_index("ix_project_memberships_user_id", "project_memberships", ["user_id"])

    op.create_table(
        "api_keys",
        sa.Column("id", UUID, nullable=False),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("public_key", sa.String(64), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("secret_prefix", sa.String(16), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.String(32)), nullable=False),
        sa.Column("task_ids", postgresql.ARRAY(UUID), nullable=True),
        sa.Column("system", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("creator_kind", sa.String(8), nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        _created(),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_api_keys"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_api_keys_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_api_keys_created_by_users", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("public_key", name="uq_api_keys_public_key"),
        sa.CheckConstraint(
            "scopes <@ ARRAY[" + ", ".join(repr(s) for s in SCOPES) + "]::varchar[]",
            name="ck_api_keys_scopes",
        ),
        sa.CheckConstraint(
            "NOT (system AND 'label:protected' = ANY(scopes))", name="ck_api_keys_system"
        ),
        sa.CheckConstraint(_one_of("creator_kind", CREATOR_KINDS), name="ck_api_keys_creator_kind"),
        sa.CheckConstraint(
            "(creator_kind = 'user' AND created_by IS NOT NULL) "
            "OR (creator_kind = 'cli' AND created_by IS NULL AND system)",
            name="ck_api_keys_creator",
        ),
    )
    op.create_index("ix_api_keys_project_id", "api_keys", ["project_id"])
    op.create_index("ix_api_keys_created_by", "api_keys", ["created_by"])

    op.create_table(
        "protected_labels",
        sa.Column("task_id", UUID, nullable=False),
        sa.Column("label", sa.String(36), nullable=False),
        _created(),
        sa.PrimaryKeyConstraint("task_id", "label", name="pk_protected_labels"),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_protected_labels_task_id_tasks", ondelete="CASCADE"
        ),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", UUID, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("organization_id", UUID, nullable=True),
        sa.Column("project_id", UUID, nullable=True),
        sa.Column("task_id", UUID, nullable=True),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(320), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target", sa.String(500), nullable=True),
        sa.Column("address", sa.String(64), nullable=True),
        sa.Column("detail", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
        sa.CheckConstraint(_one_of("actor_kind", ACTOR_KINDS), name="ck_audit_events_actor_kind"),
    )
    op.create_index("ix_audit_events_at", "audit_events", ["at"])
    op.create_index("ix_audit_events_organization_id", "audit_events", ["organization_id"])


def downgrade() -> None:
    count = op.get_bind().scalar(sa.text("SELECT count(*) FROM users"))
    if count:
        raise RuntimeError(
            f"{count} user(s) exist, which revision 0005 cannot hold; nothing was downgraded. "
            "Delete them first, or stay at 0006"
        )
    for table in (
        "protected_labels",
        "audit_events",
        "api_keys",
        "project_memberships",
        "org_memberships",
        "sessions",
        "identities",
        "users",
    ):
        op.drop_table(table)
    op.drop_constraint("uq_projects_id", "projects", type_="unique")
