"""Arc 12 EX3 — drop identity_claims.domain_id + composite/unique guards.

Revision ID: arc12_ex3_drop_identity_claim_domain
Revises: arc12_ex3_drop_knowledge_domain
Create Date: 2026-05-29

Single-table cleanup: removes the legacy ``domain_id`` String(100) NOT NULL
column from ``identity_claims``. v2 keys a claim by ``admin_id`` + its
natural identity (claim_type, claim_value); the domain half is residue
from the pre-Arc-12 (tenant_id, domain_id) scope shape.

Constraint / index decisions
----------------------------

Pre-state (after Arc 9.2 PR #101 stripped tenant_id):

  * ``uq_identity_claims_type_value_scope`` — UNIQUE on
    ``(claim_type, claim_value, admin_id, domain_id)``. This is the
    load-bearing duplicate-claim guard. Drop and RE-CREATE without
    ``domain_id`` so the uniqueness shape becomes
    ``(claim_type, claim_value, admin_id)`` — the v2 natural key.
    Two facts about the same value under two admins are still
    independent; the same fact asserted twice under the same admin
    is still a duplicate. NOT dropping this guarantee silently.

  * ``ix_identity_claims_active_resolver`` — partial INDEX on
    ``(admin_id, domain_id, claim_type, claim_value) WHERE active=true``.
    Resolver hot-path index. Drop and RE-CREATE without ``domain_id``
    on ``(admin_id, claim_type, claim_value) WHERE active=true`` — the
    v2 resolver query shape after the resolver bridge drops domain_id.

  * ``ix_identity_claims_user_tenant_domain_active`` — partial INDEX
    on ``(user_id, admin_id, domain_id) WHERE active=true``. Inverse
    lookup ("all active claims for this user under this scope"). Drop
    and RE-CREATE on ``(user_id, admin_id) WHERE active=true`` —
    still cheap, still the correct shape for the v2 user-scope read.

  * ``ix_identity_claims_domain_id`` — single-column INDEX on
    ``domain_id``. Drop outright. No v2 query reads claims by domain.

``IF EXISTS`` guards every drop so the migration is idempotent across
environments where prior arcs already cleaned up residue.

Downgrade
---------

Re-adds ``domain_id`` as NULLABLE (downgrade does not restore the
original NOT NULL — there is no backfill source for the dropped
values). Drops the v2 narrow forms and recreates the original
constraint + indexes on the four/three-column shape.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "arc12_ex3_drop_identity_claim_domain"
down_revision = "arc12_ex3_drop_knowledge_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop the unique constraint FIRST. Postgres refuses to drop
    #    the backing index while the owning constraint exists; the
    #    constraint drop releases it.
    op.execute(
        "ALTER TABLE public.identity_claims "
        "DROP CONSTRAINT IF EXISTS uq_identity_claims_type_value_scope"
    )

    # 2. Drop the composite / single-column indexes that reference
    #    domain_id. IF EXISTS keeps the migration idempotent.
    op.execute(
        "DROP INDEX IF EXISTS public.ix_identity_claims_active_resolver"
    )
    op.execute(
        "DROP INDEX IF EXISTS public.ix_identity_claims_user_tenant_domain_active"
    )
    op.execute(
        "DROP INDEX IF EXISTS public.ix_identity_claims_domain_id"
    )

    # 3. RE-CREATE the load-bearing uniqueness on the v2 natural key
    #    (claim_type, claim_value, admin_id). Duplicate-claim
    #    protection MUST survive this migration; only the scope shape
    #    narrows.
    op.create_unique_constraint(
        "uq_identity_claims_type_value_scope",
        "identity_claims",
        ["claim_type", "claim_value", "admin_id"],
    )

    # 4. RE-CREATE the resolver hot-path partial index on the v2
    #    shape (admin_id, claim_type, claim_value) WHERE active=true.
    #    The unique constraint above already provides a unique btree
    #    on this prefix; the partial index keeps inactive claims out
    #    of the resolver's scans.
    op.execute(
        "CREATE INDEX ix_identity_claims_active_resolver "
        "ON public.identity_claims "
        "(admin_id, claim_type, claim_value) "
        "WHERE active = true"
    )

    # 5. RE-CREATE the inverse-lookup partial index on (user_id,
    #    admin_id) WHERE active=true. Same intent, narrower shape.
    op.execute(
        "CREATE INDEX ix_identity_claims_user_tenant_domain_active "
        "ON public.identity_claims "
        "(user_id, admin_id) "
        "WHERE active = true"
    )

    # 6. Finally drop the column. CASCADE is unnecessary — all
    #    dependent indexes / constraints were removed in steps 1-2.
    op.drop_column("identity_claims", "domain_id")


def downgrade() -> None:
    # Re-add the column as NULLABLE. The original column was NOT
    # NULL but no backfill source exists post-drop; downgrade is a
    # structural restore only.
    op.add_column(
        "identity_claims",
        sa.Column(
            "domain_id",
            sa.String(length=100),
            nullable=True,
        ),
    )

    # Drop the v2 narrow forms so the original wide forms can be
    # recreated.
    op.execute(
        "ALTER TABLE public.identity_claims "
        "DROP CONSTRAINT IF EXISTS uq_identity_claims_type_value_scope"
    )
    op.execute(
        "DROP INDEX IF EXISTS public.ix_identity_claims_active_resolver"
    )
    op.execute(
        "DROP INDEX IF EXISTS public.ix_identity_claims_user_tenant_domain_active"
    )

    # Recreate the original wide unique constraint on
    # (claim_type, claim_value, admin_id, domain_id).
    op.create_unique_constraint(
        "uq_identity_claims_type_value_scope",
        "identity_claims",
        ["claim_type", "claim_value", "admin_id", "domain_id"],
    )

    # Recreate the original wide resolver hot-path partial index.
    op.execute(
        "CREATE INDEX ix_identity_claims_active_resolver "
        "ON public.identity_claims "
        "(admin_id, domain_id, claim_type, claim_value) "
        "WHERE active = true"
    )

    # Recreate the original wide inverse-lookup partial index.
    op.execute(
        "CREATE INDEX ix_identity_claims_user_tenant_domain_active "
        "ON public.identity_claims "
        "(user_id, admin_id, domain_id) "
        "WHERE active = true"
    )

    # Recreate the plain single-column index on domain_id.
    op.create_index(
        "ix_identity_claims_domain_id",
        "identity_claims",
        ["domain_id"],
        unique=False,
    )
