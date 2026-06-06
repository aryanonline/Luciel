"""Arc 8 WU-6 -- email_send_event table (SES feedback ingestion).

Revision ID: a91c4d2e7f08
Revises: b4d8a2e7c1f3
Create Date: 2026-05-22

Why this migration exists
-------------------------

Arc 8 Work-Unit 6 closes the SES deliverability cohort: feedback loop,
application-layer suppression, IAM right-shape, and (asynchronously) the
sandbox-exit ticket. This migration lands the first of two new tables:
`email_send_event`, the durable record of every SES feedback event we
receive via SNS.

The companion migration (`b2e5f17a3d9c_arc8_wu6_email_suppression.py`)
lands `email_suppression`, which carries a foreign key into this table's
`event_id` column. These two migrations together close
`D-ses-feedback-loop-not-wired-2026-05-22` and
`D-ses-suppression-app-layer-not-implemented-2026-05-22` at the schema
layer.

Schema design
-------------

* ``id``                INTEGER PK, autoincrement. Surrogate so internal
                        FK refs are stable across address re-creation.
* ``event_id``          VARCHAR(128) NOT NULL UNIQUE. SNS MessageId.
                        UUID-shaped today (~36 chars) but sized for
                        future SNS format changes.
* ``event_type``        VARCHAR(32) NOT NULL. One of Bounce, Complaint,
                        Reject, RenderingFailure, Delivery, Send, Open,
                        Click. CHECK constraint enforces the set.
* ``address``           VARCHAR(320) NULL. Affected recipient. One row
                        per recipient for multi-recipient events. NULL
                        for Reject / RenderingFailure.
* ``received_at``       TIMESTAMPTZ NOT NULL default now(). When our
                        route layer wrote the row.
* ``raw_payload_json``  JSONB NOT NULL. Full decoded SNS message body.
* ``created_at``        TIMESTAMPTZ NOT NULL default now() (TimestampMixin).
* ``updated_at``        TIMESTAMPTZ NOT NULL default now() (TimestampMixin).

Indexes:
* PK on ``id``.
* UNIQUE on ``event_id`` (idempotent SNS delivery; duplicate posts
  collapse via IntegrityError at the route layer).
* Single-column index on ``event_type`` for time-range queries by type.
* Single-column index on ``address`` for "all events affecting this
  recipient" queries.
* Composite ``(address, event_type)`` index for the suppression
  service's "has this address ever had a HardBounce or Complaint?"
  lookup.

CHECK constraint:
* ``event_type IN (...)`` over the eight allowed values. Service-layer
  enforcement via the ``SES_EVENT_TYPES`` set in
  ``app/models/email_send_event.py`` is the primary gate; the CHECK
  constraint is defence-in-depth and prevents a regressed route from
  writing a typo'd event_type into the table.

No FKs into this table at upgrade time. The companion
`email_suppression.source_event_id` FK is added by the companion
migration after this one.

Idempotency
-----------

The route layer (``app/api/v1/ses_events.py``) treats a UNIQUE
violation on ``event_id`` as a successful no-op -- SNS guarantees
at-least-once delivery, so the route must be idempotent at the
schema layer. The migration creates the table empty; no backfill
is required because there is no prior shape being replaced.

Rollback
--------

``downgrade()`` drops the table (and its indexes / CHECK constraint
implicitly). No data is preserved on downgrade because the table is
append-only and any received events can be replayed from CloudWatch
(SNS does not retain message bodies long-term, but our route layer
logs the full payload before insert).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = "a91c4d2e7f08"
down_revision = "b4d8a2e7c1f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create email_send_event table, its indexes, and the CHECK constraint."""
    op.create_table(
        "email_send_event",
        sa.Column(
            "id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "event_id",
            sa.String(length=128),
            nullable=False,
            comment=(
                "SNS MessageId. UNIQUE for idempotent delivery; "
                "duplicate SNS posts collapse to a single row via "
                "IntegrityError handled at the route layer."
            ),
        ),
        sa.Column(
            "event_type",
            sa.String(length=32),
            nullable=False,
            comment=(
                "One of: Bounce, Complaint, Reject, RenderingFailure, "
                "Delivery, Send, Open, Click. CHECK constraint enforces "
                "the set."
            ),
        ),
        sa.Column(
            "address",
            sa.String(length=320),
            nullable=True,
            comment=(
                "Affected recipient. One row per recipient for "
                "multi-recipient events. NULL for Reject / "
                "RenderingFailure."
            ),
        ),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "raw_payload_json",
            JSONB,
            nullable=False,
            comment=(
                "Decoded SNS message body, verbatim. JSONB for forensic "
                "replay and queryability."
            ),
        ),
        # TimestampMixin columns
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
        sa.CheckConstraint(
            "event_type IN ('Bounce', 'Complaint', 'Reject', "
            "'RenderingFailure', 'Delivery', 'Send', 'Open', 'Click')",
            name="ck_email_send_event_event_type",
        ),
        sa.UniqueConstraint("event_id", name="uq_email_send_event_event_id"),
    )

    # Indexes
    op.create_index(
        "ix_email_send_event_event_id",
        "email_send_event",
        ["event_id"],
        unique=False,  # UNIQUE constraint above already gives uniqueness
    )
    op.create_index(
        "ix_email_send_event_event_type",
        "email_send_event",
        ["event_type"],
    )
    op.create_index(
        "ix_email_send_event_address",
        "email_send_event",
        ["address"],
    )
    op.create_index(
        "ix_email_send_event_address_event_type",
        "email_send_event",
        ["address", "event_type"],
    )


def downgrade() -> None:
    """Drop the email_send_event table and all dependent indexes / constraints.

    The companion `email_suppression` migration's `source_event_id` FK
    is created with ON DELETE SET NULL, but that FK lives on
    `email_suppression`, not here -- dropping `email_send_event` first
    requires that the companion migration has already been rolled back
    (Alembic enforces this via the revision chain).
    """
    op.drop_index("ix_email_send_event_address_event_type", table_name="email_send_event")
    op.drop_index("ix_email_send_event_address", table_name="email_send_event")
    op.drop_index("ix_email_send_event_event_type", table_name="email_send_event")
    op.drop_index("ix_email_send_event_event_id", table_name="email_send_event")
    op.drop_table("email_send_event")
