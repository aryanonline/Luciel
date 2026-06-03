"""Arc 17 B — secret_cleanup_outbox table.

Revision ID: arc17_b_secret_cleanup_outbox
Revises: arc17_a_connection_domain_agnostic_renames
Create Date: 2026-06-03

Why this migration exists
-------------------------
Task 4 of the Arc 17 connections completion: when a connection carrying
a non-null ``credential_ref`` is revoked by the lifecycle cascade
(instance delete / account closure), the secret stored behind that
pointer must be cleaned up. The cleanup is decoupled from the request
path via a transactional OUTBOX: the cascade INSERTs one row here in
the SAME transaction as the revocation + audit; a Celery worker drains
the outbox and calls ``SecretStore.delete`` (AWS deletion is
DEPLOY-GATED behind ``connections_live_secrets_enabled``).

Schema
------
* ``id``             — Integer PK.
* ``admin_id``       — String(100) non-null, indexed. NOT an FK: the
                       connection / instance may be hard-purged before
                       this row is drained; the cleanup must survive.
* ``instance_id``    — Integer nullable, indexed (forensics).
* ``connection_id``  — Integer nullable (forensics).
* ``credential_ref`` — String(255) non-null. The secret NAME/ARN
                       pointer — NEVER the value (Locked Decision #18).
* ``status``         — String(16): pending | done | failed.
* ``attempts``       — Integer, retry counter.
* ``last_error``     — Text nullable, last failure detail.
* ``enqueued_at``    — timestamptz non-null.
* ``processed_at``   — timestamptz nullable.

No RLS
------
The drain worker runs under the BYPASSRLS ops role (cross-tenant nightly
sweep, same posture as ``instance_retention``); it must read pending
rows across all admins. Tenant isolation for the connection itself is
enforced upstream on ``instance_connections``; this outbox holds only an
inert secret pointer, never tenant data or a secret value. A partial
index on ``status='pending'`` backs the drain scan.

Rollback contract
-----------------
``downgrade`` drops the table. Data-safe: dropping a not-yet-drained
outbox only leaves secrets un-cleaned (an operator can sweep the secret
store manually); it never destroys tenant data.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "arc17_b_secret_cleanup_outbox"
down_revision = "arc17_a_connection_domain_agnostic_renames"
branch_labels = None
depends_on = None


_TABLE = "secret_cleanup_outbox"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "admin_id",
            sa.String(100),
            nullable=False,
            index=True,
            comment=(
                "Owning Admin (forensics). NOT an FK: the connection / "
                "instance may be hard-purged before this row is drained."
            ),
        ),
        sa.Column("instance_id", sa.Integer(), nullable=True, index=True),
        sa.Column("connection_id", sa.Integer(), nullable=True),
        sa.Column(
            "credential_ref",
            sa.String(255),
            nullable=False,
            comment=(
                "Secret NAME/ARN pointer — NEVER the value "
                "(Locked Decision #18)."
            ),
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="pending",
            comment="pending | done | failed.",
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "enqueued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    # Drain scan support: the worker selects pending rows oldest-first.
    op.create_index(
        "ix_secret_cleanup_outbox_pending",
        _TABLE,
        ["enqueued_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_secret_cleanup_outbox_pending", table_name=_TABLE)
    op.drop_table(_TABLE)
