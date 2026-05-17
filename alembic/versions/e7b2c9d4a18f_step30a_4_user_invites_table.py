"""Step 30a.4: user_invites table for first-class invite lifecycle.

Revision ID: e7b2c9d4a18f
Revises: a3c1f08b9d42
Create Date: 2026-05-17

Why this migration exists
-------------------------

Step 30a.4 lands the invite-teammate primitive that the Team-tier
`/app/team` UI and (in Step 30a.5) the Company-tier `/app/company` UI
both call. Previously the invite path overloaded
`POST /admin/luciel-instances`'s `teammate_email` field (Step 30a.1
commit G), which (a) reached for the daily-login magic-link token class
for what is structurally a set_password event on the invitee side, and
(b) had no first-class row to hang invite state on (pending, accepted,
expired, revoked).

This migration creates the `user_invites` table -- the durable record
of "tenant T invited <email> to hold role R within (tenant T, domain
D); the corresponding capability JWT has jti J; the row expires at
time B; status is pending|accepted|expired|revoked".

Pairing with the JWT
--------------------

The set_password-class JWT minted via
`magic_link_service.mint_set_password_token(purpose="invite")` is a
thin capability handle. It carries `sub`, `email`, `tenant_id`, `typ`,
`purpose=invite`, `jti` -- but NOT `domain_id` or `role`. Those live
on this table, looked up by `token_jti` at redemption time. This keeps
the Step 30a.3 auth surface (which already shipped `purpose="invite"`
support on the `set_password` token class) untouched.

Closure-shape (alpha) on TTLs
-----------------------------

* JWT TTL stays 24h (settings.magic_link_ttl_hours, unchanged).
* Invite-row TTL is 7 days via `expires_at`.
* If the 24h token lapses before the 7-day invite window, the
  `/app/team` resend affordance rotates `token_jti` and re-mints a
  fresh token against the same invite row. The invite row is the
  source of truth on expiry; the JWT is replaceable.

This is the locked closure-shape from Step 30a.4 plan-formation
(2026-05-17). The alternative (bumping JWT TTL for `purpose="invite"`)
would have required a wider surface change on Step 30a.3 with regression
risk on the signup path; the alpha shape keeps the surface frozen.

Schema design
-------------

Columns mirror the project convention:

* `id`             UUID PK with `gen_random_uuid()` server default (the
                   pgcrypto extension is already present from the
                   Step 24.5b users-table migration).
* `tenant_id`      VARCHAR(100), FK `tenant_configs.tenant_id`
                   ON DELETE RESTRICT (mirrors Agent / ScopeAssignment).
* `domain_id`      VARCHAR(100), NOT NULL, no direct FK. Validated at
                   the InviteService layer against
                   `domain_configs(tenant_id, domain_id)`. Same pattern
                   as Agent.domain_id / ScopeAssignment.domain_id.
* `inviter_user_id` UUID, FK `users.id` ON DELETE RESTRICT.
* `invited_email`  VARCHAR(320) (RFC 5321 max + headroom, matches
                   `User.email`). Stored raw; dedupe via LOWER() index.
* `role`           VARCHAR(100), default 'teammate'.
* `token_jti`      VARCHAR(64), UNIQUE. The JWT's jti claim.
* `status`         native ENUM user_invite_status (pending|accepted|
                   expired|revoked), default 'pending'.
* `expires_at`     TIMESTAMPTZ NOT NULL. Computed at insert time by the
                   service layer (created_at + 7 days).
* `accepted_at`    TIMESTAMPTZ NULL.
* `accepted_user_id` UUID NULL, FK `users.id` ON DELETE RESTRICT.
* `revoked_at`     TIMESTAMPTZ NULL.
* `revoked_by_api_key_id` INTEGER NULL, FK `api_keys.id` ON DELETE
                   SET NULL.
* `created_at` / `updated_at` from TimestampMixin (TIMESTAMPTZ
                   NOT NULL with `now()` defaults).

Indexes
-------

* `ix_user_invites_tenant_status_pending` -- partial composite on
  (tenant_id, status) WHERE status='pending'. Backs the
  `GET /api/v1/admin/invites` listing.
* `ix_user_invites_invited_email_lower` -- expression index on
  LOWER(invited_email). Backs case-insensitive lookups.
* `uq_user_invites_tenant_email_pending` -- partial UNIQUE on
  (tenant_id, LOWER(invited_email)) WHERE status='pending'. The
  schema-layer guard against two pending invites for the same teammate
  under the same tenant. A second pending invite is a UX bug; a
  revoked-then-reinvited row is fine (the old row is not 'pending'
  anymore so the partial unique does not collide).
* `ix_user_invites_token_jti` -- the UNIQUE constraint on `token_jti`
  is itself a usable index for O(1) redemption lookup.

Pattern E -- additive only
--------------------------

This migration creates one new table, one new ENUM type, and three
indexes. It does not touch any existing column, index, constraint, or
table. The existing `teammate_email` overload on
`POST /api/v1/admin/luciel-instances` is preserved unchanged in the
same Step 30a.4 commit arc (its wrong-token-class bug is fixed in that
same arc; the route itself is logged as deprecated and scheduled for
removal at Step 30a.5 close).

Rollback note
-------------

Downgrade drops the table, the indexes, and the ENUM type in that
order. Any rows present at downgrade time are LOST. This is acceptable
for two reasons: (a) the only data path that writes here is the new
InviteService.create_invite call that lands in the same commit -- a
downgrade of this revision implies the InviteService is also being
reverted; (b) invites are recoverable by re-sending from the admin UI
(or the deprecated `teammate_email` overload, which remains functional).

Cross-refs
----------

CANONICAL_RECAP section 12 Step 30a.4 row (the design home).
ARCHITECTURE section 3.2.13 "Team-invite path" paragraph (the
implementation home, sharpened 2026-05-17 in the Step 30a.4 closing
commit).
DRIFTS section 3 `~~D-team-self-serve-incomplete-invite-ui-missing-2026-05-16~~`
(the parent drift closed by the Step 30a.4 closing commit) and
`D-step-30a-4-live-300-paid-evidence-pending-intro-fee-scaling-2026-05-17`
(the carve-out drift carrying the live-$300 wire leg to the very-end
Stripe-Prices sweep).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


# revision identifiers, used by Alembic.
revision = "e7b2c9d4a18f"
down_revision = "a3c1f08b9d42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create user_invites table, the user_invite_status enum, and indexes."""
    # ENUM first -- referenced by the table's status column.
    user_invite_status = sa.Enum(
        "pending",
        "accepted",
        "expired",
        "revoked",
        name="user_invite_status",
        create_constraint=True,
        validate_strings=True,
    )
    user_invite_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "user_invites",
        sa.Column(
            "id",
            PG_UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            sa.String(length=100),
            sa.ForeignKey("tenant_configs.tenant_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "domain_id",
            sa.String(length=100),
            nullable=False,
            comment=(
                "Service-layer validated against domain_configs"
                "(tenant_id, domain_id). No direct FK -- composite-key "
                "convention. Mirrors Agent.domain_id."
            ),
        ),
        sa.Column(
            "inviter_user_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "invited_email",
            sa.String(length=320),
            nullable=False,
            comment=(
                "Raw email as the inviter typed it. Case-insensitive "
                "dedupe via the LOWER(invited_email) expression index."
            ),
        ),
        sa.Column(
            "role",
            sa.String(length=100),
            nullable=False,
            server_default=sa.text("'teammate'"),
        ),
        sa.Column(
            "token_jti",
            sa.String(length=64),
            nullable=False,
            comment=(
                "JWT jti of the currently-outstanding token. Rotated on "
                "resend so a single invite row can outlive multiple "
                "24h-TTL token mints across its 7-day expiry window."
            ),
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "accepted",
                "expired",
                "revoked",
                name="user_invite_status",
                create_constraint=False,  # already created above
                validate_strings=True,
            ),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment=(
                "Computed by InviteService.create_invite as "
                "created_at + 7 days at insert time. Independent of "
                "the 24h JWT TTL."
            ),
        ),
        sa.Column(
            "accepted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "accepted_user_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "revoked_by_api_key_id",
            sa.Integer(),
            sa.ForeignKey("api_keys.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # TimestampMixin columns -- the ORM provides defaults via the
        # mixin, the schema mirrors via server_default for safety on
        # any non-ORM insert path (test fixtures, manual repairs).
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "token_jti",
            name="uq_user_invites_token_jti",
        ),
        comment=(
            "Step 30a.4 -- first-class invite lifecycle. Token is a "
            "thin capability handle; this row is the structured payload. "
            "Closure-shape (alpha): invite row is source of truth on "
            "expiry (7 days); token TTL stays 24h with resend rotation."
        ),
    )

    # Hot-path listing index for the admin UI (pending only).
    op.create_index(
        "ix_user_invites_tenant_status_pending",
        "user_invites",
        ["tenant_id", "status"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    # Case-insensitive expression index for email lookups.
    op.create_index(
        "ix_user_invites_invited_email_lower",
        "user_invites",
        [sa.text("LOWER(invited_email)")],
    )

    # Partial UNIQUE -- the duplicate-pending-invite guard.
    op.create_index(
        "uq_user_invites_tenant_email_pending",
        "user_invites",
        ["tenant_id", sa.text("LOWER(invited_email)")],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    # tenant_id and inviter_user_id are already indexed as the leading
    # columns of named composite indexes / FK side-effects above; we do
    # not add separate single-column indexes for them to keep the index
    # set lean.


def downgrade() -> None:
    """Drop user_invites, its indexes, and the user_invite_status enum.

    Data-lossy: any invites present at downgrade time are gone. The
    deprecated `teammate_email` overload on
    `POST /api/v1/admin/luciel-instances` remains a complete fallback
    surface for re-creating any lost invites.
    """
    op.drop_index(
        "uq_user_invites_tenant_email_pending",
        table_name="user_invites",
    )
    op.drop_index(
        "ix_user_invites_invited_email_lower",
        table_name="user_invites",
    )
    op.drop_index(
        "ix_user_invites_tenant_status_pending",
        table_name="user_invites",
    )
    op.drop_table("user_invites")

    # Drop the enum type last (must be after the table that referenced it).
    sa.Enum(name="user_invite_status").drop(op.get_bind(), checkfirst=True)
