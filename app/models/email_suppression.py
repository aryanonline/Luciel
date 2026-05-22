"""EmailSuppression model -- application-layer suppression list for SES sends.

Arc 8 WU-6 (SES feedback / suppression / deliverability cohort closure).
Closes `D-ses-suppression-app-layer-not-implemented-2026-05-22`.

Why a dedicated table (vs. relying on SES account-level suppression):
- SES account-level suppression operates at the SES wire. It prevents the
  send, but the failure surfaces to the application as a normal API call
  cost (and as an audit-shape blind spot: the application has no record
  of why a send was suppressed unless it parses the SES response shape).
- An application-layer mirror lets us short-circuit the send entirely
  (cheaper, faster, and decoupled from SES API state). It also gives the
  audit chain a first-class row for every suppression event, with the
  reason and a pointer to the source event id (the SNS Bounce / Complaint
  notification id from `email_send_event`).

Design contract
---------------

* One row per suppressed address. `address` is the natural key
  (case-insensitive via LOWER expression index). Re-suppression of an
  already-suppressed address updates `last_suppressed_at` and the
  `reason` but does not create a second row.
* `reason` is one of three values, kept as module-level constants
  rather than a PG enum so a fourth reason can be added without a
  schema migration:
    * SUPPRESSION_REASON_HARD_BOUNCE -- SES Bounce event with bounceType
      'Permanent' (HardBounce). The address is structurally undeliverable.
    * SUPPRESSION_REASON_COMPLAINT  -- SES Complaint event. The recipient
      marked the message as spam in their mail client.
    * SUPPRESSION_REASON_MANUAL_BLOCK -- an operator-initiated entry via
      a future admin endpoint. For incident response (e.g. an address
      we know is a typo or a known abuser).
* The `source_event_id` field points back to `email_send_event.event_id`
  (the SNS MessageId) when the suppression was triggered by a feedback
  event. For manual blocks, this is NULL.
* No soft-delete column. Removing a suppression is an explicit admin
  action that hard-deletes the row (and writes an audit row of its own
  -- the `EMAIL_SUPPRESSION_CLEARED` action constant added in WU-6).
  Hard-delete is acceptable here because the audit chain carries the
  record; the table itself is a working-state lookup, not the
  system-of-record for whether a suppression ever existed.

Relationships
-------------

* EmailSuppression has no SQLAlchemy relationships. It is a leaf table
  whose only consumer is the application-layer precheck in
  `app/services/email_service.py`. The `source_event_id` reference to
  `email_send_event.event_id` is captured as a string FK (with
  `ON DELETE SET NULL`) so a feedback-event purge does not cascade-
  delete the suppression rows it triggered -- the suppression must
  survive its source event's retention window.

Invariants honored
------------------

* Invariant 3 (soft-delete only): waived here per the design note above
  (admin-initiated hard delete with audit trail substitutes for the
  soft-delete column). Documented in DRIFTS once the WU-6 commit lands.
* Six-pillar contract (reliability + traceability): every suppression
  row is paired with an `EMAIL_SUPPRESSION_RECORDED` audit row in the
  same transaction; every clearance writes an
  `EMAIL_SUPPRESSION_CLEARED` audit row.
"""
from __future__ import annotations

import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# ---------------------------------------------------------------------
# Reason constants -- module-level strings (not a PG enum) so new
# reasons can be added without a schema migration. Service-layer
# enforcement is the gate; the table column carries a CHECK constraint
# at the migration layer as defence-in-depth.
# ---------------------------------------------------------------------

SUPPRESSION_REASON_HARD_BOUNCE = "HardBounce"
SUPPRESSION_REASON_COMPLAINT = "Complaint"
SUPPRESSION_REASON_MANUAL_BLOCK = "ManualBlock"

SUPPRESSION_REASONS = frozenset(
    {
        SUPPRESSION_REASON_HARD_BOUNCE,
        SUPPRESSION_REASON_COMPLAINT,
        SUPPRESSION_REASON_MANUAL_BLOCK,
    }
)


class EmailSuppression(Base, TimestampMixin):
    """An address that the application layer refuses to send to.

    Created by the SES feedback handler on Bounce / Complaint events, or
    by an operator-initiated manual block. The application-layer send
    sites in `app/services/email_service.py` precheck this table via
    `EmailSuppressionService.is_suppressed(address)` and raise
    `SuppressedRecipientError` rather than calling
    `client.send_email`.
    """

    __tablename__ = "email_suppression"

    # Surrogate PK so audit refs are stable across an address re-creation.
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # The suppressed address, stored as provided. Case-insensitive
    # uniqueness is enforced by the LOWER(address) expression index at
    # the migration layer (mirrors User.email convention).
    address: Mapped[str] = mapped_column(
        String(320),
        nullable=False,
        index=True,
        comment=(
            "Suppressed email address. Stored raw; uniqueness enforced "
            "by LOWER(address) expression index per User.email convention."
        ),
    )

    # Why this address is suppressed. One of SUPPRESSION_REASONS above.
    # CHECK constraint at the migration layer enforces the allowed set.
    reason: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment=(
            "One of: HardBounce, Complaint, ManualBlock. Service-layer "
            "enforced via SUPPRESSION_REASONS set; CHECK constraint at "
            "the migration layer is defence-in-depth."
        ),
    )

    # Pointer to the SNS MessageId of the feedback event that triggered
    # this suppression. NULL for manual blocks. ON DELETE SET NULL so a
    # feedback-event retention purge does not cascade-delete the
    # suppression -- the suppression must outlive its source event.
    source_event_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("email_send_event.event_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment=(
            "SNS MessageId of the Bounce / Complaint event that "
            "triggered this suppression. NULL for ManualBlock."
        ),
    )

    # When the address was first suppressed. Distinct from created_at
    # (TimestampMixin) so that a manual-block-then-feedback-bounce
    # scenario can preserve the original first-suppressed moment while
    # updating last_suppressed_at on subsequent events.
    # Set by the service layer at insert time (now()); subsequent
    # re-suppressions do not update this column -- only
    # `last_suppressed_at` and `reason`.
    first_suppressed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # When the address was most recently suppressed. Updated on every
    # re-suppression event (e.g. a Bounce followed weeks later by a
    # Complaint from the same address).
    last_suppressed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        # Case-insensitive uniqueness on the address. Mirrors the
        # User.email LOWER() index convention. Service-layer lookup uses
        # LOWER(address) = LOWER(:lookup) so the index is hit.
        Index(
            "ux_email_suppression_lower_address",
            text("LOWER(address)"),
            unique=True,
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<EmailSuppression(id={self.id}, address={self.address!r}, "
            f"reason={self.reason!r})>"
        )
