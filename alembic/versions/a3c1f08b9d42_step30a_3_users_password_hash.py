"""Step 30a.3: users.password_hash column for password authentication.

Revision ID: a3c1f08b9d42
Revises: dfea1a04e037
Create Date: 2026-05-16

Why this migration exists
-------------------------

Step 30a.3 lands password authentication as the single daily-login
primitive across every tier (Individual / Team / Company), with
magic-link demoted to a recovery-and-invite-acceptance bootstrap.

The partner's 2026-05-16 judgment call sharpened this: password is
**mandatory at every signup**, not opt-in. The mechanic that enforces
that is the Option-B welcome-email flow:

  1. The buyer completes Stripe Checkout.
  2. The `checkout.session.completed` webhook handler mints the User
     row (password_hash=NULL) and the Subscription row inside a single
     transaction, commits, then immediately mints a `set_password`-class
     magic-link token and emails a welcome message:
         "Welcome to VantageMind. Click the link below to finish
          setting up your account by choosing a password."
  3. The user clicks the welcome link, lands on the marketing site's
     `/auth/set-password?token=...` page, types a password, the page
     POSTs to `POST /api/v1/auth/set-password`, the route validates
     the token, calls `AuthService.set_password` which argon2id-hashes
     and writes here, then mints the same `luciel_session` cookie the
     magic-link redeem route already mints today.
  4. The user lands on `/app` cookied. Daily login from there forward
     is `POST /api/v1/auth/login` with email + password -- never an
     inbox round-trip.

This column is the storage substrate for that flow.

Column design
-------------

* `password_hash VARCHAR(255) NULL`.

  - **Why nullable.** A User row exists for the window between
    webhook commit (User row minted) and welcome-link consume
    (password set). That window is design-intended -- the welcome
    email is the "password mandatory" enforcement surface, not a
    NOT NULL constraint at the schema layer. Putting NOT NULL here
    would force us to either (a) require the webhook to know the
    password (impossible, the buyer has not chosen one yet) or (b)
    write a sentinel value (a footgun: a typo in the verify path
    against any sentinel would compare-equal on every login).

  - **Why VARCHAR(255).** argon2-cffi's PasswordHasher serialises
    parameters + salt + 32-byte digest into a single string of
    shape:
        $argon2id$v=19$m=65536,t=3,p=4$<22-char-salt>$<43-char-digest>
    which is ~96 chars at the library defaults. 255 gives generous
    headroom for higher cost params (m=131072 or t=4) without a
    schema migration. We do not need TEXT here -- the column is
    bounded by the hash format.

  - **Why no index.** We never look up users by hash. We look up by
    email (already indexed via the LOWER(email) expression index in
    the original users-table migration) and then verify_password
    against the looked-up row.

  - **Why no CHECK constraint on format.** Format validation lives in
    `AuthService.set_password` (argon2-cffi's `PasswordHasher.hash`
    returns a guaranteed-valid string; we trust it). A DB-side CHECK
    on the `$argon2id$` prefix would couple the schema to the choice
    of hash library; if we ever swap to a different algorithm
    (post-quantum, etc.) we want to do so without an Alembic round.

Pattern E -- additive only
--------------------------

This migration is purely additive. It does not touch any existing
column, index, or constraint. The existing magic-link auth path is
unchanged; the existing User read paths (cookied resolver in the
Step 31.2 middleware, the billing webhook's `_resolve_or_create_user`)
are unchanged. The new column is read only by `AuthService.verify_password`
and written only by `AuthService.set_password`, both landing in the
same Step 30a.3 commit.

Rollback note
-------------

Downgrade drops the column. If a downgrade is ever needed in prod,
any password_hash values written between upgrade and downgrade are
LOST -- there is no upstream copy. This is acceptable because the
magic-link fallback path is unchanged and any user can recover via
`POST /api/v1/auth/forgot-password` even with their password_hash
nulled. The downgrade is documented as data-lossy below but is
otherwise routine.

Cross-refs
----------

CANONICAL_RECAP section 12 Step 30a.3 row (the design home).
ARCHITECTURE section 3.2 ("Password / SSO / MFA auth" bullet, line
391, sharpened 2026-05-16 with the mandatory-at-signup framing).
DRIFTS section 3 `D-magic-link-only-auth-no-password-fallback-2026-05-16`
(the open drift this migration begins closing).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "a3c1f08b9d42"
down_revision = "dfea1a04e037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add `users.password_hash` as a nullable VARCHAR(255).

    No backfill -- existing User rows are left with NULL, and any
    user whose row predates this column simply walks through the
    `/forgot-password` flow on their next login attempt to set one.
    The `AuthService.verify_password` path treats NULL as a clean
    "no password set" signal (the route surfaces a 401 with a
    machine-readable error code so the frontend can offer the
    forgot-password CTA inline).
    """
    op.add_column(
        "users",
        sa.Column(
            "password_hash",
            sa.String(length=255),
            nullable=True,
            comment=(
                "Step 30a.3: argon2id password digest. Nullable; "
                "set via POST /api/v1/auth/set-password."
            ),
        ),
    )


def downgrade() -> None:
    """Drop `users.password_hash`.

    Data-lossy: any password hashes written between upgrade and
    downgrade are gone. Acceptable because the magic-link path
    remains a complete recovery surface.
    """
    op.drop_column("users", "password_hash")
