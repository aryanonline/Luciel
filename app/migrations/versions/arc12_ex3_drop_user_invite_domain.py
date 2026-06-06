"""Arc 12 EX3 — drop user_invites.domain_id (NOT NULL legacy column).

Revision ID: arc12_ex3_drop_user_invite_domain
Revises: arc12_ex3_drop_identity_claim_domain
Create Date: 2026-05-29

Single-table cleanup: removes the legacy ``domain_id`` String(100) NOT
NULL column from ``user_invites``. v2 scopes an invite by ``admin_id``
(plus instance where relevant); the domain half is residue from the
pre-Arc-12 (tenant_id, domain_id) scope shape.

Constraint / index decisions
----------------------------

Pre-state (Step 30a.4 create migration `e7b2c9d4a18f_step30a_4_user_invites_table`
and any subsequent arcs):

  * No index or unique constraint references ``user_invites.domain_id``
    directly. The hot-path indexes (`ix_user_invites_tenant_status_pending`,
    `ix_user_invites_invited_email_lower`,
    `uq_user_invites_tenant_email_pending`) are all scoped by
    ``admin_id`` and/or ``LOWER(invited_email)`` — none of them needs
    to be rebuilt for this drop.

  * No live RLS policy references the column (EX2 swept invite RLS).

Therefore the upgrade is a single ``op.drop_column`` after a guarded
no-op block that documents the search for residue. ``IF EXISTS`` keeps
the migration idempotent across environments.

Downgrade
---------

Re-adds ``domain_id`` as NULLABLE. The original column was NOT NULL but
no backfill source exists post-drop; downgrade is a structural restore
only.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "arc12_ex3_drop_user_invite_domain"
down_revision = "arc12_ex3_drop_identity_claim_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Defensive: drop any single-column index that may have been added
    # downstream of the Step 30a.4 create migration. The Step 30a.4
    # migration ships no such index, but IF EXISTS keeps the migration
    # idempotent across environments where one might have been added
    # by hand.
    op.execute(
        "DROP INDEX IF EXISTS public.ix_user_invites_domain_id"
    )

    # Drop the column. CASCADE is unnecessary — no index/constraint in
    # the create migration references domain_id.
    op.drop_column("user_invites", "domain_id")


def downgrade() -> None:
    # Re-add as NULLABLE. Structural restore only — no backfill source
    # for the dropped values.
    op.add_column(
        "user_invites",
        sa.Column(
            "domain_id",
            sa.String(length=100),
            nullable=True,
        ),
    )
