"""Arc 8 WU-6 -- email_suppression table (application-layer suppression list).

Revision ID: b2e5f17a3d9c
Revises: a91c4d2e7f08
Create Date: 2026-05-22

Why this migration exists
-------------------------

Companion to ``a91c4d2e7f08_arc8_wu6_email_send_event.py``. Lands the
application-layer suppression mirror that the email-send sites in
``app/services/email_service.py`` precheck before calling SES.

Together with the companion migration, this closes
``D-ses-feedback-loop-not-wired-2026-05-22`` and
``D-ses-suppression-app-layer-not-implemented-2026-05-22`` at the schema
layer. The service layer (``EmailSuppressionService``) and the route
layer (``ses_events.py``) close them at the runtime layer.

Schema design
-------------

* ``id``                  INTEGER PK, autoincrement. Surrogate so audit
                          rows can reference a stable id even if the
                          address is later cleared and re-suppressed.
* ``address``             VARCHAR(320) NOT NULL. Suppressed recipient.
                          Stored as provided; uniqueness enforced via
                          ``LOWER(address)`` expression index.
* ``reason``              VARCHAR(32) NOT NULL. One of HardBounce,
                          Complaint, ManualBlock. CHECK constraint
                          enforces the set.
* ``source_event_id``     VARCHAR(128) NULL. FK to
                          ``email_send_event.event_id`` with
                          ON DELETE SET NULL. NULL for ManualBlock
                          entries.
* ``first_suppressed_at`` TIMESTAMPTZ NOT NULL default now(). Set once at
                          insert. Never updated.
* ``last_suppressed_at``  TIMESTAMPTZ NOT NULL default now(). Updated on
                          every re-suppression by the service layer.
* ``created_at``          TIMESTAMPTZ NOT NULL default now() (TimestampMixin).
* ``updated_at``          TIMESTAMPTZ NOT NULL default now() (TimestampMixin).

Indexes:
* PK on ``id``.
* Non-unique index on ``address`` for raw lookup.
* Non-unique index on ``source_event_id`` for "all suppressions
  triggered by event X" queries.
* UNIQUE expression index ``ux_email_suppression_lower_address`` on
  ``LOWER(address)``. Mirrors the ``users.email`` LOWER() convention
  so the service-layer lookup ``WHERE LOWER(address) = LOWER(:lookup)``
  hits an index. This is the natural-key uniqueness gate -- the table
  carries no UNIQUE on raw ``address`` because the expression index is
  the canonical case-insensitive constraint.

CHECK constraint:
* ``reason IN ('HardBounce', 'Complaint', 'ManualBlock')``. Service-layer
  enforcement via the ``SUPPRESSION_REASONS`` set in
  ``app/models/email_suppression.py`` is the primary gate; the CHECK
  constraint is defence-in-depth.

FK contract
-----------

``source_event_id`` -> ``email_send_event.event_id``, ON DELETE SET NULL.
A feedback-event retention purge must not cascade-delete the
suppression rows it triggered -- the suppression must outlive its
source event so that a re-bounce after retention purge does not
re-suppress an already-known-bad address.

Idempotency
-----------

The service layer (``EmailSuppressionService.record_suppression``) is
idempotent at the application layer: re-suppression of an existing
address updates ``last_suppressed_at`` and ``reason`` rather than
inserting a second row. The UNIQUE ``LOWER(address)`` index is the
schema-layer safety net for that contract.

Rollback
--------

``downgrade()`` drops the table (and its indexes / CHECK / FK
implicitly). The migration creates the table empty; no data
preservation on downgrade is meaningful. Operators rolling back WU-6
should also disable the SES configuration-set event destination so no
new feedback events stack up unprocessed.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "b2e5f17a3d9c"
down_revision = "a91c4d2e7f08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create email_suppression table, its indexes, CHECK, and FK."""
    op.create_table(
        "email_suppression",
        sa.Column(
            "id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "address",
            sa.String(length=320),
            nullable=False,
            comment=(
                "Suppressed email address. Stored as provided; "
                "case-insensitive uniqueness enforced by the LOWER(address) "
                "expression index."
            ),
        ),
        sa.Column(
            "reason",
            sa.String(length=32),
            nullable=False,
            comment=(
                "One of HardBounce, Complaint, ManualBlock. Service-layer "
                "enforced; CHECK constraint is defence-in-depth."
            ),
        ),
        sa.Column(
            "source_event_id",
            sa.String(length=128),
            nullable=True,
            comment=(
                "SNS MessageId of the Bounce / Complaint event that "
                "triggered this suppression. NULL for ManualBlock."
            ),
        ),
        sa.Column(
            "first_suppressed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_suppressed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
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
            "reason IN ('HardBounce', 'Complaint', 'ManualBlock')",
            name="ck_email_suppression_reason",
        ),
        sa.ForeignKeyConstraint(
            ["source_event_id"],
            ["email_send_event.event_id"],
            name="fk_email_suppression_source_event_id",
            ondelete="SET NULL",
        ),
    )

    # Non-unique secondary indexes
    op.create_index(
        "ix_email_suppression_address",
        "email_suppression",
        ["address"],
    )
    op.create_index(
        "ix_email_suppression_source_event_id",
        "email_suppression",
        ["source_event_id"],
    )

    # Case-insensitive UNIQUE expression index -- the canonical
    # natural-key gate. Mirrors users.email LOWER() convention so the
    # service-layer lookup hits this index directly.
    op.create_index(
        "ux_email_suppression_lower_address",
        "email_suppression",
        [sa.text("LOWER(address)")],
        unique=True,
    )


def downgrade() -> None:
    """Drop the email_suppression table and all dependent indexes / constraints.

    The FK to ``email_send_event.event_id`` is dropped implicitly when
    the table is dropped. Operators rolling back WU-6 should also
    disable the SES configuration-set event destination so unprocessed
    feedback events do not stack up against a missing target table.
    """
    op.drop_index("ux_email_suppression_lower_address", table_name="email_suppression")
    op.drop_index("ix_email_suppression_source_event_id", table_name="email_suppression")
    op.drop_index("ix_email_suppression_address", table_name="email_suppression")
    op.drop_table("email_suppression")
