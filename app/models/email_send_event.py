"""EmailSendEvent model -- durable record of SES feedback events received via SNS.

Arc 8 WU-6 (SES feedback / suppression / deliverability cohort closure).
Closes `D-ses-feedback-loop-not-wired-2026-05-22`.

Why this table exists
---------------------

SES emits structured notifications for Bounce, Complaint, Reject, and
RenderingFailure events. These notifications route through an SES
configuration set (`luciel-default`) to an SNS topic
(`luciel-ses-events`) which then HTTPS-subscribes to our backend at
`POST /api/v1/ses-events`. The route in `app/api/v1/ses_events.py`
validates the SNS message signature, parses the event, and writes a
row into this table.

Three reasons to persist:

1. **Suppression decisions are downstream of feedback events.** A
   HardBounce or Complaint event triggers an `EmailSuppression` row;
   the `email_send_event` row is the source-of-record that ties the
   suppression to the SNS MessageId. Without this table, the
   suppression's `source_event_id` column would be a dangling
   reference.
2. **Deliverability posture is auditable.** AWS requires (for the
   sandbox-exit approval bar) that the operator can demonstrate
   handling of bounce/complaint feedback. The table is the artifact
   that demonstrates it: every received feedback event is recorded
   with its raw payload, the parsed event type, the affected address,
   and the SNS message id.
3. **Forensic replay.** If we discover later that a configuration set
   mis-routed events (or that the SNS subscription was unverified for
   a window), we can re-process the raw payloads. The `raw_payload_json`
   column carries the entire SNS message body for that purpose.

Design contract
---------------

* One row per received SNS message. `event_id` is the SNS MessageId,
  which is UUID-shaped and globally unique per SNS topic. Idempotency
  is enforced by a UNIQUE constraint -- a duplicate SNS delivery (which
  SNS guarantees can happen) maps to a single row.
* `event_type` is the SES event-type string: `Bounce`, `Complaint`,
  `Reject`, `RenderingFailure`, `Delivery` (only enabled if the
  configuration set adds it), `Send` (likewise), or `Open` / `Click`
  (we do not enable these but defensively allow the column to hold
  them). No PG enum; module-level string constants instead so a new
  event type does not require a schema migration.
* `address` is the affected recipient for Bounce / Complaint /
  RenderingFailure / Reject events. For events that affect multiple
  recipients (a bulk Bounce), we write one row per recipient -- the
  SNS message body carries a list, and the route layer iterates.
* `received_at` is when the route layer wrote the row. Distinct from
  the SES event's own timestamp (which is inside the raw payload) so
  that operator clock skew between SES and our backend remains
  forensically separable.
* `raw_payload_json` is the entire decoded SNS message body. Stored
  as JSONB for queryability.

Relationships
-------------

* EmailSendEvent has a reverse relationship from EmailSuppression
  (`source_event_id` FK with ON DELETE SET NULL). No forward
  SQLAlchemy relationship is declared because the consumer is the
  suppression service, which looks up by event_id directly.

Retention
---------

Feedback events are operational telemetry, not customer data. They
fall under the standard operational-telemetry retention class
(ARCHITECTURE §3.2.4 retention purge worker) which today purges
beyond the configured cutoff. The Arc 8 WU-6 migration does not add
this table to the retention worker's purge set yet -- that lands as
follow-up hygiene once the WU-6 surface has stabilized in production.
The deferred purge is tracked separately and does not block WU-6
closure.

Invariants honored
------------------

* Append-only. No UPDATE path exists in the service layer; the route
  inserts a row and never touches it again. The suppression service
  reads but does not write.
* Idempotent. UNIQUE on `event_id` makes a duplicate SNS delivery a
  no-op at the schema layer; the route handles the IntegrityError as
  a successful idempotent path.
"""
from __future__ import annotations

import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# ---------------------------------------------------------------------
# Event-type constants -- module-level strings (not a PG enum). The
# CHECK constraint at the migration layer enforces the allowed set as
# defence-in-depth; the route layer is the gate.
# ---------------------------------------------------------------------

SES_EVENT_BOUNCE = "Bounce"
SES_EVENT_COMPLAINT = "Complaint"
SES_EVENT_REJECT = "Reject"
SES_EVENT_RENDERING_FAILURE = "RenderingFailure"
SES_EVENT_DELIVERY = "Delivery"
SES_EVENT_SEND = "Send"
SES_EVENT_OPEN = "Open"
SES_EVENT_CLICK = "Click"

SES_EVENT_TYPES = frozenset(
    {
        SES_EVENT_BOUNCE,
        SES_EVENT_COMPLAINT,
        SES_EVENT_REJECT,
        SES_EVENT_RENDERING_FAILURE,
        SES_EVENT_DELIVERY,
        SES_EVENT_SEND,
        SES_EVENT_OPEN,
        SES_EVENT_CLICK,
    }
)

# Subset that triggers an EmailSuppression row. Other event types are
# recorded but do not auto-suppress.
SES_EVENT_TYPES_TRIGGER_SUPPRESSION = frozenset(
    {
        SES_EVENT_BOUNCE,  # only HardBounce sub-type triggers; service layer filters
        SES_EVENT_COMPLAINT,
    }
)


class EmailSendEvent(Base, TimestampMixin):
    """A received SES feedback event, routed via SNS into our backend.

    Created by `app/api/v1/ses_events.py` on every SNS POST. Read by
    `EmailSuppressionService` to look up the source event when paging
    through suppression decisions. Append-only.
    """

    __tablename__ = "email_send_event"

    # Surrogate PK for stable internal references.
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # SNS MessageId. UUID-shaped and globally unique per topic.
    # UNIQUE so duplicate SNS deliveries are idempotent at the schema
    # layer. Length 128 to absorb any future SNS message-id format
    # changes (today's UUID-with-dashes is 36 chars).
    event_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        unique=True,
        index=True,
        comment=(
            "SNS MessageId. UNIQUE for idempotent delivery; duplicate "
            "SNS posts collapse to a single row via IntegrityError."
        ),
    )

    # The SES event type. CHECK constraint enforces the allowed set
    # (see SES_EVENT_TYPES at module level).
    event_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
        comment=(
            "One of: Bounce, Complaint, Reject, RenderingFailure, "
            "Delivery, Send, Open, Click. Enforced by CHECK constraint."
        ),
    )

    # Recipient address affected by this event. For multi-recipient
    # events (bulk Bounce), the route layer writes one row per
    # recipient. NULL is allowed for Reject / RenderingFailure events
    # that do not carry a single recipient address.
    address: Mapped[str | None] = mapped_column(
        String(320),
        nullable=True,
        index=True,
        comment=(
            "Affected recipient address. One row per recipient for "
            "multi-recipient events. NULL for Reject / RenderingFailure."
        ),
    )

    # When the route layer wrote the row. Distinct from the SES event's
    # own timestamp inside `raw_payload_json` to make operator clock
    # skew between SES and our backend forensically separable.
    received_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # The full SNS message body, decoded. JSONB for queryability.
    raw_payload_json: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        comment=(
            "Decoded SNS message body, verbatim. JSONB for forensic "
            "replay and queryability."
        ),
    )

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('Bounce', 'Complaint', 'Reject', "
            "'RenderingFailure', 'Delivery', 'Send', 'Open', 'Click')",
            name="ck_email_send_event_event_type",
        ),
        Index(
            "ix_email_send_event_address_event_type",
            "address",
            "event_type",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<EmailSendEvent(id={self.id}, event_id={self.event_id!r}, "
            f"event_type={self.event_type!r}, address={self.address!r})>"
        )
