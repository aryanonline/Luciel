"""Arc 6 — Revision B: add users.email_verified column.

Adds the ``email_verified`` boolean to the ``users`` table. This column
is the schema-level record of "this email address has been proven
reachable by its owner" — distinct from "the user has set a password"
(``password_hash IS NOT NULL``) and from "the user is active"
(``active=true``).

Why now (Arc 6, not earlier):

Arc 6 Commit 8 introduces the unified-signup flow where a Free-tier
account is born with NO Stripe row and NO password. The user's first
interaction with their account is the welcome email's set-password
magic link. **Consuming that link is itself proof that the address is
reachable** — the link landed in the inbox, the human clicked it, and
the human is now setting a password. Treating that single act as
*both* "email verified" *and* "password set" eliminates the redundant
"please click the verification link" mail (the welcome email IS the
verification mail in this design).

Decision D-arc6-c8-email-verified-column-2026-05-23 (recorded in
``arc6-out/D-arc6-c8-unified-signup-design-decisions.md``):

* **ADD a schema column** rather than inferring verification from
  ``password_hash IS NOT NULL``. Reasoning:
  * The two facts are independent in the long run. A Pro buyer
    completes Stripe Checkout (proves email = the address on the
    payment method) BEFORE setting a password (the welcome-email
    arrives only after the webhook fires). So between webhook and
    set-password, ``email_verified=true`` while ``password_hash IS
    NULL``. Inferring verification from password would mis-classify
    these users as unverified during that window.
  * Future flows (SSO, magic-link login, OAuth) may verify email
    WITHOUT setting a password. The column lets these flows be
    additive — they flip ``email_verified=true`` and never touch
    ``password_hash``.
  * PIPEDA / GDPR honesty: "verified" is a customer-facing fact
    (it appears in the account UI, in the platform_admin console,
    and in audit rows). It should be a first-class column, not a
    computed predicate.

* **Default ``false``** rather than backfilling existing users to
  ``true``. Reasoning: this revision lands BEFORE Commit 8's
  unified-signup code, so existing rows (pre-Arc-6 paid users from
  Step 30a / Arc 5) have email status that was *implicitly* verified
  by Stripe Checkout but not *recorded* anywhere. Leaving them at
  ``false`` is the schema-honest default; a follow-up backfill (if
  ever needed) can ``UPDATE users SET email_verified=true WHERE
  EXISTS (SELECT 1 FROM subscriptions s JOIN admins a ON ...)``.
  The unified-signup flow flips the bit at set-password time, so
  any user who logs in post-Commit-8 will have it set correctly on
  their next password reset.

Column shape:

* ``email_verified BOOLEAN NOT NULL DEFAULT false``
* No index. The column is read at request boundaries on a per-User
  basis (already keyed by ``id``); there is no "find all verified
  users" hot query in the system today.

Revision: arc6_b_users_email_verified
Revises: arc6_a_admin_widget_domains
Create Date: 2026-05-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "arc6_b_users_email_verified"
down_revision = "arc6_a_admin_widget_domains"
branch_labels = None
depends_on = None


# -----------------------------------------------------------------------------
# Upgrade
# -----------------------------------------------------------------------------
def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "email_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


# -----------------------------------------------------------------------------
# Downgrade
# -----------------------------------------------------------------------------
def downgrade() -> None:
    op.drop_column("users", "email_verified")
