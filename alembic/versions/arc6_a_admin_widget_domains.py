"""Arc 6 — Revision A: admin_widget_domains allowlist table.

Adds the ``admin_widget_domains`` table — the per-Admin domain allowlist
that the embeddable widget consults before accepting a chat request.
Born inside Arc 6 because the V2 SKU shape (Free / Pro / Enterprise) is
where the allowlist first earns its keep:

* **Free tier** signups (CAPTCHA-gated, no Stripe row) need a place to
  register the one or two domains they will embed the widget on.
* **Pro / Enterprise** Admins manage their allowlists from the admin
  console; Pro is rate-capped to a small N, Enterprise is uncapped (the
  numeric cap is the in-app §14 entitlement matrix's call, not the
  schema's — anchored to Architecture v1 §3.2 (Instance subsystem)).

FK shape — locked decision (recorded in
``arc6-out/D-arc6-admin-widget-domains-design-decisions.md``):

* ``admin_id VARCHAR(100) NOT NULL`` referencing ``admins(id)``
  ``ON DELETE CASCADE``. Born with V2 vocabulary so Arc 6 Commit 6's
  Tenant→Admin rename has zero touch on this table.
* No ``subscription_id`` column. Free tier has no Stripe row by design
  (CANONICAL §11.7 numeric lock), so tying allowlists to subscriptions
  would either require a synthetic "free subscription" row (rejected
  in Commit 1) or break the Free tier flow entirely.

Uniqueness:

* ``UNIQUE (admin_id, domain)`` — one Admin cannot register the same
  domain twice. Domains are stored lowercased and apex-normalized by
  the app layer; the schema enforces uniqueness on the stored form.
* No global ``UNIQUE (domain)`` — two different Admins can independently
  allowlist the same public hostname (e.g., a shared marketplace
  domain). The widget routes requests by ``admin_id`` so collisions are
  irrelevant at the request-resolution layer.

Lookup index:

* ``ix_admin_widget_domains_admin_id ON (admin_id)`` — supports the
  hot per-request lookup ``SELECT 1 FROM admin_widget_domains WHERE
  admin_id = :a AND domain = :d``.

Forward-only by design. ``downgrade()`` drops the table cleanly because
the table is born fresh in this revision and has no upstream data
producers before activation; if rollback is ever needed mid-flight
(unlikely), it is safe to run.

Revision: arc6_a_admin_widget_domains
Revises: arc5_c_admin_instance_subtractive
Create Date: 2026-05-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "arc6_a_admin_widget_domains"
down_revision = "arc5_c_admin_instance_subtractive"
branch_labels = None
depends_on = None


# -----------------------------------------------------------------------------
# Upgrade
# -----------------------------------------------------------------------------
def upgrade() -> None:
    op.create_table(
        "admin_widget_domains",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=False, start=1),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "admin_id",
            sa.String(100),
            sa.ForeignKey(
                "admins.id",
                name="fk_admin_widget_domains_admin_id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "domain",
            sa.String(253),  # RFC 1035 max hostname length
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "admin_id",
            "domain",
            name="uq_admin_widget_domains_admin_id_domain",
        ),
        # Belt-and-suspenders: the app layer stores lowercased + apex-normalized
        # domain strings; the CHECK pins that contract at the schema layer so
        # a buggy writer cannot quietly insert mixed-case rows that would
        # bypass the UNIQUE constraint.
        sa.CheckConstraint(
            "domain = lower(domain)",
            name="ck_admin_widget_domains_domain_lowercase",
        ),
        # Domain must be non-empty and not contain whitespace. Cheap sanity
        # backstop; the app layer does heavier validation.
        sa.CheckConstraint(
            "length(domain) > 0 AND domain !~ '[[:space:]]'",
            name="ck_admin_widget_domains_domain_shape",
        ),
    )

    op.create_index(
        "ix_admin_widget_domains_admin_id",
        "admin_widget_domains",
        ["admin_id"],
    )


# -----------------------------------------------------------------------------
# Downgrade — safe because the table is born in this revision
# -----------------------------------------------------------------------------
def downgrade() -> None:
    op.drop_index(
        "ix_admin_widget_domains_admin_id",
        table_name="admin_widget_domains",
    )
    op.drop_table("admin_widget_domains")
