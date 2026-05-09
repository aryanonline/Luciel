"""Step 30b commit (b): widget columns on api_keys.

Revision ID: a7c1f4e92b85
Revises: d8e2c4b1a0f3
Create Date: 2026-05-09

Why this migration exists
-------------------------

Step 30b ships an embeddable chat widget. Public-site visitors land on
a customer page (e.g. a brokerage marketing site), an inline script
boots a Preact bundle, and that bundle calls the Luciel backend to
stream a reply. The browser cannot carry a tenant admin API key
without leaking it on view-source, so a new credential class is
needed: a *public embed key* that is safe to ship in HTML, scoped
narrow enough that exposure is bounded.

Decision (locked in commit (b) of step-30b-chat-widget):

    Embed keys live on the existing ``api_keys`` table, not a
    separate ``embed_keys`` table.

Rationale: the entire key-resolution path -- hash-lookup, scope
policy check, audit-row emission on auth events, Pattern E
deactivation, rotation runbook -- is already wired against
``api_keys``. A parallel embed_keys table would either fork that
plumbing (two of every audit event) or reimplement it (drift risk
the moment one side gains a feature the other doesn't). Adding a
``key_kind`` discriminator and three widget-only nullable columns
keeps one resolution path and one audit surface.

What the four new columns do
----------------------------

1. ``key_kind VARCHAR(20) NOT NULL DEFAULT 'admin'``
   Discriminates the credential class. v1 enumerates two values:

     - ``'admin'``  -- existing server-to-server keys; full
       permissions array honored; no origin check; no per-minute
       cap beyond ``rate_limit`` (per-day).
     - ``'embed'`` -- public widget keys; permissions MUST be
       exactly ``[\"chat\"]`` at issuance; ``allowed_origins`` MUST
       be non-empty; ``rate_limit_per_minute`` MUST be set;
       cannot mint sessions for any tool path until Step 30c lands.

   Existing rows backfill to ``'admin'`` by the column default,
   preserving current behavior for every issued key.

2. ``allowed_origins TEXT[] NULL``
   Origin allowlist consumed by the widget request middleware.
   Rejection compares the request ``Origin`` header against this
   array exactly (scheme + host + port). NULL is the admin-key
   case where no origin check applies. An empty array on an
   embed-kind row is invalid and rejected at issuance.

3. ``rate_limit_per_minute INT NULL``
   Per-minute ceiling enforced before the SSE stream opens.
   ``rate_limit`` (existing column, per-day) still applies; the
   per-minute cap is the burst guard against a compromised embed
   key that someone copies into a load generator. NULL on admin
   keys (no per-minute cap). Required on embed keys.

4. ``widget_config JSONB NULL``
   Three-knob branding payload, schema fixed at v1:

     - ``accent_color``     : 7-char hex, validated server-side
     - ``greeting_message`` : plaintext, length-capped server-side
     - ``display_name``     : plaintext, length-capped server-side

   No logo upload, no font selection, no free-form CSS. The
   conservative shape is deliberate: zero customization knobs lose
   the sales conversation; ten open an XSS surface on customer
   sites we cannot QA. v1.1 may add a logo URL slot if customers
   actually request it; HTML/CSS injection is permanently off the
   table.

Why this is safe to apply now
-----------------------------

- ``key_kind`` adds with ``NOT NULL DEFAULT 'admin'`` so existing
  rows backfill in a single statement and no application read path
  sees a NULL.
- The other three columns are nullable, so existing admin keys are
  left untouched and remain valid.
- No row mutations, no deletions; Pattern E preserved.
- ``IF NOT EXISTS`` guards on every ADD COLUMN make this migration
  redeploy-safe.
- No index added in this migration; embed-key lookup uses the
  existing ``key_hash`` unique index. A future migration may add a
  partial index on ``key_kind`` if the admin/embed split becomes
  read-hot, but the current scale does not justify it.

Idempotency
-----------

Every column addition uses IF NOT EXISTS. Re-running this migration
on a database where it already applied is a no-op. ``downgrade()``
drops all four columns with IF EXISTS in the reverse order.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


# Revision identifiers, used by Alembic.
revision = "a7c1f4e92b85"
down_revision = "d8e2c4b1a0f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Each column is added with IF NOT EXISTS so this migration is
    # safe to re-run on a database that already received it (e.g. a
    # replay against a hand-patched workstation copy).
    op.execute(
        sa.text(
            """
            ALTER TABLE api_keys
            ADD COLUMN IF NOT EXISTS key_kind VARCHAR(20) NOT NULL DEFAULT 'admin'
            """
        )
    )
    op.execute(
        sa.text(
            """
            ALTER TABLE api_keys
            ADD COLUMN IF NOT EXISTS allowed_origins TEXT[] NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            ALTER TABLE api_keys
            ADD COLUMN IF NOT EXISTS rate_limit_per_minute INTEGER NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            ALTER TABLE api_keys
            ADD COLUMN IF NOT EXISTS widget_config JSONB NULL
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE api_keys DROP COLUMN IF EXISTS widget_config"))
    op.execute(sa.text("ALTER TABLE api_keys DROP COLUMN IF EXISTS rate_limit_per_minute"))
    op.execute(sa.text("ALTER TABLE api_keys DROP COLUMN IF EXISTS allowed_origins"))
    op.execute(sa.text("ALTER TABLE api_keys DROP COLUMN IF EXISTS key_kind"))
