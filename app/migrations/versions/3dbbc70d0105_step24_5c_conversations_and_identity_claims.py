"""step 24.5c: conversations + identity_claims + sessions.conversation_id

Revision ID: 3dbbc70d0105
Revises: a7c1f4e92b85
Create Date: 2026-05-11 23:00:00.000000

Step 24.5c -- Sub-branch 1 of the implementation arc (cross-channel
identity and conversation continuity). Lands the three primitives committed
by the design-lock pass in commit c98d752:

A. conversations table -- the durable cross-channel thread, scoped to one
   (tenant_id, domain_id). UUID PK, gen_random_uuid() server-default.
   Mirrors users / scope_assignments discipline from 24.5b: sequential
   IDs would leak per-scope conversation counts to anyone who can
   enumerate. tenant_id has FK to tenant_configs.tenant_id RESTRICT;
   domain_id intentionally has no FK (composite natural key in
   domain_configs, validated at service layer per the scope_assignments
   convention). last_activity_at is the recency cursor the identity
   resolver walks. active is the Invariant-3 soft-delete flag.

B. identity_claim_type enum + identity_claims table. Enum is a Postgres
   native type with values EMAIL / PHONE / SSO_SUBJECT. Table records the
   channel-specific identifier bound to a User within an issuing scope.
   user_id UUID FK -> users.id RESTRICT. tenant_id String(100) FK ->
   tenant_configs.tenant_id RESTRICT. domain_id String(100) no FK
   (composite-key convention). issuing_adapter is a free-form String
   so Step 34a's new adapters ('voice_gateway' etc.) don't need a schema
   migration to land. verified_at nullable -- v1 trust is adapter-
   asserted; end-user-driven verification lands with Step 34a + Step 31.
   active = Invariant 3 soft-delete.

   Uniqueness on (claim_type, claim_value, tenant_id, domain_id) is
   load-bearing: it is the contract that lets "the same number under
   two scopes" be two independent facts. The writer (service / adapter
   layer) is responsible for normalising claim_value before insert --
   LOWER() for email, E.164 for phone, opaque for sso_subject. The
   DB enforces the uniqueness after normalisation has happened.

   Two partial indexes filtered on active=true serve the hot paths:
   resolver lookup (tenant_id, domain_id, claim_type, claim_value) and
   inverse listing (user_id, tenant_id, domain_id).

C. sessions.conversation_id nullable UUID FK -> conversations.id
   ON DELETE SET NULL. Nullable is the design contract per ARCHITECTURE
   §3.2.11: a session that arrives with no continuity claim stays as a
   single-session conversation until and unless a future session links
   into it. SET NULL on parent delete preserves the session row's audit
   integrity even if a Conversation row is administratively pruned far
   in the future (Step 24.5c retention semantics aren't decided here --
   the column shape just allows for them). Existing session rows stay
   with conversation_id=NULL -- no backfill is performed because the
   v1 design contract is to let continuity emerge as new sessions
   arrive bound to a User via identity_claims, not to retroactively
   group historical traffic.

Downgrade reverses C -> B -> A in correct dependency order:
sessions.conversation_id FK + index + column first (depends on
conversations.id), then identity_claims indexes + table + enum (depends
on users.id and tenant_configs.tenant_id), then conversations indexes +
table (depends on tenant_configs.tenant_id). pgcrypto extension is
intentionally retained -- 24.5b's migration commits the same rule:
other migrations may depend on it and dropping is destructive.

Pre-flight expectations (to verify before applying in any environment
above local dev):
- Local Alembic chain: a7c1f4e92b85 (head) -> 3dbbc70d0105 (this rev).
- Prod Alembic chain: a7c1f4e92b85 (head). Confirm via ECS run-task
  `luciel-migrate alembic current` before any prod apply.
- RDS rollback snapshot taken via the runbook for the impl-arc final
  cut-over (the impl arc lands more than this sub-branch on main
  before any prod migrate; the staging-then-prod rollout is decided
  by the closing sub-branch).

Verified replayable against fresh DB on the sub-branch CI environment
via the migration's own structural shape (upgrade -> downgrade ->
upgrade idempotency is asserted by the test in tests/api/
test_step24_5c_models_shape.py per the AST-contract pattern Step 30d
established).

Cross-refs:
- ARCHITECTURE §3.2.11 (canonical spec for all three primitives).
- CANONICAL_RECAP §11 Q8 and §12 Step 24.5c row.
- DRIFTS.md D-step-24-5c-impl-backlog-2026-05-11 (the impl token this
  sub-branch starts to drain).
- alembic/versions/3ad39f9e6b55_add_users_scope_assignments_and_agent_.py
  (24.5b precedent for hand-written multi-phase migrations; pgcrypto
  extension already in place from that migration).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "3dbbc70d0105"
down_revision = "a7c1f4e92b85"
branch_labels = None
depends_on = None


# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------

# Postgres enum metadata for IdentityClaim.claim_type. Mirrors the
# CLAIM_TYPE_ENUM_NAME / CLAIM_TYPE_VALUES module-level constants in
# app/models/identity_claim.py so the migration and the model agree on
# type name + value list without duplication drift.
CLAIM_TYPE_ENUM_NAME = "identity_claim_type"
CLAIM_TYPE_VALUES = (
    "EMAIL",
    "PHONE",
    "SSO_SUBJECT",
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Phase A: conversations table
    # ------------------------------------------------------------------
    #
    # pgcrypto is already present from 24.5b's migration
    # (3ad39f9e6b55). gen_random_uuid() is therefore available without
    # an extension-create call here.

    op.create_table(
        "conversations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            sa.String(length=100),
            sa.ForeignKey(
                "tenant_configs.tenant_id",
                ondelete="RESTRICT",
                name="fk_conversations_tenant_id_tenant_configs",
            ),
            nullable=False,
        ),
        # domain_id intentionally has no FK -- domain_configs uses
        # (tenant_id, domain_id) as the natural composite key. Same
        # convention scope_assignments uses. Service layer validates
        # the pair against domain_configs at write time.
        sa.Column(
            "domain_id",
            sa.String(length=100),
            nullable=False,
        ),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        comment=(
            "Step 24.5c -- durable cross-channel conversation grouping. "
            "Session-linking via sessions.conversation_id, never session-"
            "merging. One Conversation lives in exactly one scope. "
            "See ARCHITECTURE §3.2.11."
        ),
    )

    # Single-column btree indexes on tenant_id / domain_id satisfy the
    # `index=True` declaration on the model columns; the composite index
    # below is the hot-path one for the identity resolver.
    op.create_index(
        "ix_conversations_tenant_id",
        "conversations",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_conversations_domain_id",
        "conversations",
        ["domain_id"],
        unique=False,
    )

    # Composite index serves "most-recent active conversation under this
    # scope" lookups, which is the resolver's join target once it has
    # found a matching User via identity_claims.
    op.create_index(
        "ix_conversations_tenant_domain_last_activity",
        "conversations",
        ["tenant_id", "domain_id", "last_activity_at"],
        unique=False,
    )

    # ------------------------------------------------------------------
    # Phase B: identity_claim_type enum + identity_claims table
    # ------------------------------------------------------------------

    # Native Postgres enum, created explicitly so downgrade() can drop
    # it cleanly. Mirrors 24.5b's scope_assignment_end_reason pattern.
    claim_type_enum = postgresql.ENUM(
        *CLAIM_TYPE_VALUES,
        name=CLAIM_TYPE_ENUM_NAME,
        create_type=False,  # we create it manually below
    )
    claim_type_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "identity_claims",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "users.id",
                ondelete="RESTRICT",
                name="fk_identity_claims_user_id_users",
            ),
            nullable=False,
        ),
        sa.Column(
            "claim_type",
            postgresql.ENUM(
                *CLAIM_TYPE_VALUES,
                name=CLAIM_TYPE_ENUM_NAME,
                create_type=False,  # already created above
            ),
            nullable=False,
        ),
        sa.Column(
            "claim_value",
            sa.String(length=320),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            sa.String(length=100),
            sa.ForeignKey(
                "tenant_configs.tenant_id",
                ondelete="RESTRICT",
                name="fk_identity_claims_tenant_id_tenant_configs",
            ),
            nullable=False,
        ),
        sa.Column(
            "domain_id",
            sa.String(length=100),
            nullable=False,
        ),
        sa.Column(
            "issuing_adapter",
            sa.String(length=100),
            nullable=False,
        ),
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Load-bearing uniqueness. Two facts about the same value under
        # two scopes are independent (Brokerage A's prospect's phone vs
        # Brokerage B's prospect's phone). Writer normalises claim_value
        # before insert: LOWER() for email, E.164 for phone, opaque for
        # sso_subject. The DB enforces uniqueness after normalisation.
        sa.UniqueConstraint(
            "claim_type",
            "claim_value",
            "tenant_id",
            "domain_id",
            name="uq_identity_claims_type_value_scope",
        ),
        comment=(
            "Step 24.5c -- channel-specific identifier bound to a User "
            "within a scope. Orthogonal to scope the same way Users are. "
            "v1 trust model is adapter-asserted; end-user-driven "
            "verification lands with Step 34a + Step 31. See "
            "ARCHITECTURE §3.2.11."
        ),
    )

    op.create_index(
        "ix_identity_claims_user_id",
        "identity_claims",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_identity_claims_tenant_id",
        "identity_claims",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_identity_claims_domain_id",
        "identity_claims",
        ["domain_id"],
        unique=False,
    )

    # Hot-path resolver index (active claims only). The unique constraint
    # above already gives a unique btree on the four-tuple; this partial
    # index keeps the resolver from scanning inactive claims.
    op.execute(
        "CREATE INDEX ix_identity_claims_active_resolver "
        "ON identity_claims (tenant_id, domain_id, claim_type, claim_value) "
        "WHERE active = true"
    )

    # Inverse-lookup index ("all active claims for this user under this
    # scope"). Step 31 dashboards territory; v1 doesn't use this path
    # but the index is cheap and the shape is correct.
    op.execute(
        "CREATE INDEX ix_identity_claims_user_tenant_domain_active "
        "ON identity_claims (user_id, tenant_id, domain_id) "
        "WHERE active = true"
    )

    # ------------------------------------------------------------------
    # Phase C: sessions.conversation_id nullable FK
    # ------------------------------------------------------------------

    # Nullable by design contract -- a session that arrives with no
    # continuity claim stays as a single-session conversation. SET NULL
    # on parent delete preserves the session row's audit integrity if a
    # Conversation row is ever administratively pruned (the v1 design
    # doesn't decide conversation retention; the column shape just
    # allows for it).
    op.add_column(
        "sessions",
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "conversations.id",
                ondelete="SET NULL",
                name="fk_sessions_conversation_id",
            ),
            nullable=True,
        ),
    )

    op.create_index(
        "ix_sessions_conversation_id",
        "sessions",
        ["conversation_id"],
        unique=False,
    )


def downgrade() -> None:
    """Reverse Phases C -> B -> A in correct dependency order.

    pgcrypto extension is intentionally NOT dropped -- 24.5b retains it
    for the same reason, and any other UUID column elsewhere in the
    schema depends on it. If a future cleanup migration determines
    pgcrypto is truly orphaned it can drop it explicitly.
    """

    # ------------------------------------------------------------------
    # Reverse Phase C: drop sessions.conversation_id
    # ------------------------------------------------------------------
    op.drop_index("ix_sessions_conversation_id", table_name="sessions")
    op.drop_constraint(
        "fk_sessions_conversation_id",
        "sessions",
        type_="foreignkey",
    )
    op.drop_column("sessions", "conversation_id")

    # ------------------------------------------------------------------
    # Reverse Phase B: drop identity_claims + enum
    # ------------------------------------------------------------------
    op.execute("DROP INDEX IF EXISTS ix_identity_claims_user_tenant_domain_active")
    op.execute("DROP INDEX IF EXISTS ix_identity_claims_active_resolver")
    op.drop_index("ix_identity_claims_domain_id", table_name="identity_claims")
    op.drop_index("ix_identity_claims_tenant_id", table_name="identity_claims")
    op.drop_index("ix_identity_claims_user_id", table_name="identity_claims")

    op.drop_table("identity_claims")

    claim_type_enum = postgresql.ENUM(
        *CLAIM_TYPE_VALUES,
        name=CLAIM_TYPE_ENUM_NAME,
        create_type=False,
    )
    claim_type_enum.drop(op.get_bind(), checkfirst=True)

    # ------------------------------------------------------------------
    # Reverse Phase A: drop conversations
    # ------------------------------------------------------------------
    op.drop_index(
        "ix_conversations_tenant_domain_last_activity",
        table_name="conversations",
    )
    op.drop_index("ix_conversations_domain_id", table_name="conversations")
    op.drop_index("ix_conversations_tenant_id", table_name="conversations")
    op.drop_table("conversations")

    # pgcrypto extension intentionally retained -- see docstring.
