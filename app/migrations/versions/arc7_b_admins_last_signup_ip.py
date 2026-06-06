"""Arc 7 -- Revision B: admins.last_signup_ip column for 1-per-IP soft gate.

Adds a nullable ``last_signup_ip`` column (Postgres ``INET``) to
``admins`` so the Free signup route can soft-reject a second sign-up
from the same IP inside a rolling 24h window. The new column is
written ONLY by the Free-signup mint path
(``app/api/v1/billing.py:signup_free``); paid Stripe Checkout
flows (Pro / Enterprise) deliberately leave it NULL because the
Stripe payment surface is the abuse boundary on those paths.

Why this is a separate abuse surface from Arc 7 Commit 4's
tier-aware RPM gate (``api_rate_limit_rpm``):

* Commit 4 caps per-minute REQUEST volume on an authenticated
  admin/api-key. That closes the runtime abuse surface.
* THIS gate caps Free-tier ACCOUNT CREATION from a single IP --
  a different surface entirely (signup fraud / multi-account
  abuse), reached BEFORE any admin exists to rate-limit.

Doctrine choices:

* **INET type** (not VARCHAR). Postgres ``INET`` stores IPv4 and
  IPv6 in their natural shape (no string-format drift, no leading
  zeros, no upper/lower-case differences in IPv6 hex chunks),
  supports indexable equality + subnet queries, and uses 7 bytes
  for IPv4 / 19 bytes for IPv6 instead of variable-length text.

* **Nullable** because: (a) historical admins (every row created
  before this migration) have no captured IP; (b) paid checkout
  flows do not write it; (c) ``request.client.host`` is itself
  Optional in the route layer, and we fail-open on a missing IP
  (the captcha is the harder gate when IP is unavailable).

* **Index ``ix_admins_last_signup_ip``** (partial, ``WHERE
  last_signup_ip IS NOT NULL AND active = true``) makes the gate
  lookup cheap -- a count of active Free admins whose IP matches
  the incoming request inside a 24h window. Partial because the
  vast majority of historical rows are NULL and don't need to be
  indexed.

* **No CHECK constraint on the value itself.** The INET type is
  the constraint -- Postgres rejects malformed addresses at
  insert/update time with a structured error.

Reversibility: drop is straightforward (drop index, drop column).
No data salvage because the column is forward-write-only.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "arc7_b_admins_last_signup_ip"
down_revision = "arc7_a_retire_billing_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add the column. INET dialect-specific type from
    # sqlalchemy.dialects.postgresql -- not a generic SA type because
    # the gate's value comes from network ergonomics that don't
    # translate to other dialects, and this app is Postgres-only.
    op.add_column(
        "admins",
        sa.Column(
            "last_signup_ip",
            postgresql.INET(),
            nullable=True,
        ),
    )

    # Partial index for the gate-lookup hot path. The Free-signup
    # gate runs a single query per request:
    #
    #   SELECT COUNT(*) FROM admins
    #   WHERE last_signup_ip = $1
    #     AND active = true
    #     AND created_at >= NOW() - INTERVAL '24 hours';
    #
    # The partial predicate (`WHERE last_signup_ip IS NOT NULL AND
    # active = true`) prunes the bulk of historical NULL rows so the
    # index is small and the planner can use it without a recheck.
    op.create_index(
        "ix_admins_last_signup_ip",
        "admins",
        ["last_signup_ip"],
        unique=False,
        postgresql_where=sa.text(
            "last_signup_ip IS NOT NULL AND active = true"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_admins_last_signup_ip", table_name="admins")
    op.drop_column("admins", "last_signup_ip")
