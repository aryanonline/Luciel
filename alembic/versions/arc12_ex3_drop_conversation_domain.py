"""Arc 12 EX3 — drop conversations.domain_id + dependent indexes.

Revision ID: arc12_ex3_drop_conversation_domain
Revises: arc12_ex3_drop_session_agent_domain
Create Date: 2026-05-29

Single-table cleanup: removes the legacy ``domain_id`` String(100) column
from ``conversations``. The column was introduced in 24.5c (alembic
3dbbc70d0105) when the Conversation primitive scoped to (tenant_id,
domain_id). Arc 9.2 PR #101 dropped ``tenant_id`` and Arc-5 Revision C
collapsed the v2 scoping model to (admin_id, luciel_instance_id) — but
conversations never gained luciel_instance_id, so v2 scope for this
table is simply (admin_id). ``domain_id`` carries no remaining
contractual value: it is not read by any service-layer code path
(the chat-widget identity resolver passes admin_id + domain_id into
``_mint_conversation`` but the value only ever sets this column), it
is not joined on, it is not in the audit chain (admin_audit_logs is
untouched here), and it is not referenced by any live RLS policy.

Indexes affected
----------------

The 24.5c migration created two indexes that touch ``domain_id``:

  1. ``ix_conversations_domain_id`` — single-column btree from the
     ``index=True`` declaration on the Mapped column.
  2. ``ix_conversations_tenant_domain_last_activity`` — composite
     ``(tenant_id, domain_id, last_activity_at)`` for the identity
     resolver's "most recent active conversation in scope" lookup.

Arc 9.2 PR #101's auto-detect dropped (2) when it walked indexes
containing ``tenant_id``. So in production DB state today, only (1)
still exists. The model's ``__table_args__`` still declares (2)
with ``admin_id`` swapped for ``tenant_id`` — that declaration was
purely metadata drift (no migration ever recreated the index against
``admin_id``). This migration uses ``DROP INDEX IF EXISTS`` so it
is idempotent against either state.

The remaining scoping index on ``conversations`` is
``ix_conversations_admin_id`` (added by Arc 9.2 PR #96), which serves
admin-scoped lookups directly. We do NOT recreate a composite
``(admin_id, last_activity_at)`` index — there is no measured hot
path that needs it (the cross-session retriever joins via
sessions.conversation_id, which is its own index). If the resolver
later shows pressure on "most recent active conversation under an
admin", a dedicated migration can add the composite at that point.

Downgrade
---------

Re-adds ``domain_id`` as NULLABLE (the original column was NOT NULL,
but downgrade cannot fabricate values for rows minted post-drop — we
document the nullable choice in the column comment). Recreates
``ix_conversations_domain_id`` on the restored column. Does NOT
recreate ``ix_conversations_tenant_domain_last_activity`` because
that index was already absent from prod DB before this migration ran.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "arc12_ex3_drop_conversation_domain"
down_revision = "arc12_ex3_drop_session_agent_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop dependent indexes FIRST so the column drop has no
    #    DependentObjectsStillExist hazard. IF EXISTS keeps the
    #    migration idempotent across environments where Arc 9.2
    #    PR #101's auto-detect already removed the composite.
    op.execute(
        "DROP INDEX IF EXISTS public.ix_conversations_tenant_domain_last_activity"
    )
    op.execute(
        "DROP INDEX IF EXISTS public.ix_conversations_domain_id"
    )

    # 2. Drop the column. CASCADE is unnecessary — no FKs target it
    #    (domain_id is a composite-natural-key half, never had an FK
    #    of its own per the 24.5c convention).
    op.drop_column("conversations", "domain_id")


def downgrade() -> None:
    # Re-add NULLABLE (the original NOT NULL cannot be honoured for
    # rows minted while the column was absent — see module docstring).
    op.add_column(
        "conversations",
        sa.Column(
            "domain_id",
            sa.String(length=100),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_conversations_domain_id",
        "conversations",
        ["domain_id"],
        unique=False,
    )
