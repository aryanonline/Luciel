"""EmailSuppressionService -- application-layer SES suppression gate.

Arc 8 WU-6. Closes
``D-ses-suppression-app-layer-not-implemented-2026-05-22`` at the
service / runtime layer (the schema-layer close lands with the
``email_suppression`` migration ``b2e5f17a3d9c``).

Responsibilities
----------------

This service is the single chokepoint for two operations:

1. **Lookup** -- ``is_suppressed(session, address) -> bool``.
   Called by every outbound send site in
   ``app/services/email_service.py`` BEFORE the ``boto3.client.send_email``
   call. The send is short-circuited (and a ``SuppressedRecipientError``
   raised) when the lookup returns True. This avoids burning a SES API
   call (and absorbing the per-call cost) when we already know the
   address is undeliverable / has complained.

2. **Record** -- ``record_suppression(session, address, reason,
   source_event_id=None) -> EmailSuppression``.
   Called by the SES feedback route
   (``app/api/v1/ses_events.py``, lands in WU-6 Phase C) when a
   Bounce (with bounceType=Permanent) or Complaint event arrives via
   SNS. Idempotent: re-suppression of an existing address updates
   ``last_suppressed_at`` and ``reason`` rather than inserting a
   second row. Also writes an ``EMAIL_SUPPRESSION_RECORDED`` audit
   row in the same SQLAlchemy session as the suppression INSERT, so
   the audit chain's tamper-evident invariant (Invariant 4 -- audit
   row and mutation must commit atomically) is preserved.

Why this is a service, not a repository
---------------------------------------

The SES feedback route's calling context is not a normal tenant-
scoped request:

* It is HTTPS-driven by AWS SNS, not by a human admin or a tenant
  API key.
* It has no actor identity (no API key prefix, no permissions list)
  -- the actor is the system itself.
* The mutation crosses two tables atomically (an INSERT into
  ``email_suppression`` plus an INSERT into ``admin_audit_log``) and
  optionally references a third (the ``email_send_event`` row whose
  ``event_id`` is captured as ``source_event_id``).

A service layer is the right home for that orchestration because:

* The repository layer (``AdminAuditRepository``) requires a
  ``ctx`` carrying actor identity -- it would be wrong to fabricate
  one for a system actor; the system actor pattern is to construct
  ``AdminAuditLog`` directly, with the chain population happening
  via the SQLAlchemy ``before_flush`` event handler in
  ``app/repositories/audit_chain.py``. This service is one of those
  direct-construction call sites.
* The send-site precheck path is read-only and does not need the
  ceremony of going through a repository -- a single index lookup is
  the entire operation.

Idempotency contract
--------------------

``record_suppression`` is the idempotent surface for SNS at-least-once
delivery semantics. The same address being bounced twice in the same
hour MUST map to one row, not two. The implementation uses the case-
insensitive ``LOWER(address)`` unique index as the schema-layer gate
plus an explicit "if exists, update; else, insert" service-layer
fast path. If the fast path races with another transaction and the
INSERT hits an IntegrityError on the unique index, the service catches
it, rolls back the current attempt, and falls back to UPDATE -- so the
caller always sees a successful return.

Audit chaining
--------------

This service deliberately constructs ``AdminAuditLog`` directly (the
"direct-add" path documented in ``app/repositories/audit_chain.py``)
rather than calling ``AdminAuditRepository.record(...)``. The audit
chain's before_flush event handler picks up either path, so chain
integrity is preserved either way. We use the direct path so the
suppression INSERT and the audit INSERT share a single ``session.flush()``
boundary -- making it impossible (at the database layer, via the
chain handler) for one to land without the other.
"""
from __future__ import annotations

import logging
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.admin_audit_log import (
    ACTION_EMAIL_SUPPRESSION_CLEARED,
    ACTION_EMAIL_SUPPRESSION_RECORDED,
    AdminAuditLog,
    RESOURCE_EMAIL_SUPPRESSION,
)
from app.models.email_suppression import (
    SUPPRESSION_REASONS,
    EmailSuppression,
)

logger = logging.getLogger(__name__)


# The admin_id slot on AdminAuditLog is NOT NULL. System-actor writes
# use the literal "platform" string per the convention established by
# AdminAuditRepository.SYSTEM_ACTOR_TENANT. We avoid importing that
# constant here so this service has no dependency on the repository
# layer -- the convention is stable and documented.
_SYSTEM_ACTOR_TENANT: Final[str] = "platform"


class SuppressedRecipientError(RuntimeError):
    """Raised when an outbound send target is on the application-layer
    suppression list.

    Callers in ``app/services/email_service.py`` precheck via
    ``is_suppressed`` and raise this rather than calling
    ``client.send_email``. The caller's audit row records the
    suppression-short-circuit, so the trail captures both the original
    feedback event (``EMAIL_SUPPRESSION_RECORDED``) and the
    downstream-attempted send (the caller's existing audit row, with
    a note flagging the suppression).

    Callers SHOULD treat this as a non-retryable failure -- the
    address will not become deliverable until an operator explicitly
    clears the suppression row (which writes an
    ``EMAIL_SUPPRESSION_CLEARED`` audit row of its own).
    """


def is_suppressed(session: Session, address: str) -> bool:
    """Return True iff the given address is on the suppression list.

    Case-insensitive: lookup matches the ``LOWER(address)`` expression
    index on ``email_suppression``. The index is the canonical natural-
    key gate, so this lookup is O(log n) on the suppression table.

    Defensive on input:
        * ``None`` / empty string returns False (a no-op send target
          cannot be suppressed -- the caller will fail downstream at
          SES itself when it tries to send to an empty address).
        * Whitespace is stripped before the lookup so leading /
          trailing spaces in an SES feedback event's recipient field
          don't escape the gate.
    """
    if not address:
        return False
    normalized = address.strip()
    if not normalized:
        return False

    stmt = (
        select(EmailSuppression.id)
        .where(func.lower(EmailSuppression.address) == func.lower(normalized))
        .limit(1)
    )
    return session.execute(stmt).first() is not None


def record_suppression(
    session: Session,
    address: str,
    reason: str,
    source_event_id: str | None = None,
    *,
    actor_label: str | None = None,
    note: str | None = None,
) -> EmailSuppression:
    """Insert (or update) a suppression row + write the matching audit row.

    Parameters
    ----------
    session:
        SQLAlchemy session. The caller controls the transaction
        boundary; this service performs ``session.flush()`` to surface
        any integrity errors but does NOT commit. Callers that want
        the suppression visible immediately must commit themselves.

    address:
        The email address to suppress. Stored as provided (whitespace
        stripped). Case-insensitive uniqueness is enforced by the
        ``LOWER(address)`` index.

    reason:
        One of ``SUPPRESSION_REASONS``: ``HardBounce``, ``Complaint``,
        or ``ManualBlock``. ValueError raised for any other value.

    source_event_id:
        SNS MessageId of the Bounce / Complaint event that triggered
        this suppression. NULL for ``ManualBlock`` entries. Captured
        as a string FK to ``email_send_event.event_id``; the FK is
        ON DELETE SET NULL so a feedback-event retention purge does
        not cascade-delete the suppression.

    actor_label:
        Optional. For ManualBlock entries, the operator's email or
        slug. NULL for Bounce / Complaint feedback (system actor).
        Stored on the audit row.

    note:
        Optional free-form note (e.g. "operator XYZ blocked known
        typo address"). Stored on the audit row, NOT on the
        suppression row.

    Returns
    -------
    EmailSuppression:
        The persistent row -- either a freshly-inserted one or an
        updated existing one. The caller can read ``id`` and the
        ``first_suppressed_at`` / ``last_suppressed_at`` to
        distinguish the two cases.

    Raises
    ------
    ValueError:
        If ``reason`` is not in SUPPRESSION_REASONS, or ``address``
        is empty / whitespace-only.

    Idempotency
    -----------
    Re-suppression of an existing address:
        * does NOT create a second row;
        * updates ``last_suppressed_at`` to now();
        * updates ``reason`` and ``source_event_id`` to the latest
          values (so a Complaint after a HardBounce reflects the
          more-recent reason, which is the more-actionable signal
          for the operator);
        * writes a fresh ``EMAIL_SUPPRESSION_RECORDED`` audit row
          (every re-suppression is auditable, not just the first).
    """
    if reason not in SUPPRESSION_REASONS:
        raise ValueError(
            f"Unknown suppression reason {reason!r}; "
            f"must be one of {sorted(SUPPRESSION_REASONS)!r}."
        )

    normalized = (address or "").strip()
    if not normalized:
        raise ValueError("Cannot suppress an empty / whitespace-only address.")

    # Fast path -- find existing row by case-insensitive match.
    existing_stmt = (
        select(EmailSuppression)
        .where(func.lower(EmailSuppression.address) == func.lower(normalized))
        .limit(1)
    )
    existing: EmailSuppression | None = session.execute(existing_stmt).scalar_one_or_none()

    if existing is not None:
        existing.reason = reason
        existing.source_event_id = source_event_id
        existing.last_suppressed_at = func.now()
        row = existing
    else:
        row = EmailSuppression(
            address=normalized,
            reason=reason,
            source_event_id=source_event_id,
        )
        session.add(row)
        try:
            # Flush to surface the unique-index violation if a
            # concurrent transaction beat us to the insert. We catch
            # below and fall back to UPDATE.
            session.flush()
        except IntegrityError:
            session.rollback()
            # Retry as an update -- the row now exists from the racing
            # transaction. We re-select inside the new (post-rollback)
            # transaction context.
            logger.info(
                "email_suppression: insert race detected for address=%s "
                "(reason=%s); falling back to UPDATE.",
                normalized,
                reason,
            )
            existing = session.execute(existing_stmt).scalar_one_or_none()
            if existing is None:
                # Should be impossible: the IntegrityError says the row
                # exists, but the select can't find it. Re-raise the
                # original problem rather than silently swallowing.
                raise
            existing.reason = reason
            existing.source_event_id = source_event_id
            existing.last_suppressed_at = func.now()
            row = existing

    # Audit row -- direct construction (no repository ctx). The chain
    # population happens via app/repositories/audit_chain.py's
    # before_flush event handler when session.flush() runs below.
    after_payload = {
        "address": normalized,
        "reason": reason,
        "source_event_id": source_event_id,
    }
    audit = AdminAuditLog(
        actor_key_prefix=None,  # system actor (SNS-driven)
        actor_permissions=None,
        actor_label=actor_label,
        admin_id=_SYSTEM_ACTOR_TENANT,
        luciel_instance_id=None,
        action=ACTION_EMAIL_SUPPRESSION_RECORDED,
        resource_type=RESOURCE_EMAIL_SUPPRESSION,
        resource_pk=row.id,
        resource_natural_id=normalized.lower(),
        before_json=None,
        after_json=after_payload,
        note=note,
    )
    session.add(audit)
    session.flush()  # triggers audit_chain before_flush handler

    logger.info(
        "email_suppression: recorded address=%s reason=%s "
        "source_event_id=%s row_id=%s",
        normalized,
        reason,
        source_event_id,
        row.id,
    )
    return row


def clear_suppression(
    session: Session,
    address: str,
    *,
    actor_label: str | None = None,
    note: str | None = None,
) -> bool:
    """Remove a suppression row + write the matching audit row.

    Returns True if a row was deleted; False if no row existed (no-op).

    Hard-delete is the design: the working-state table is a lookup,
    not a system-of-record. The audit row (with the cleared row's
    state in ``before_json``) is the durable record that the
    suppression ever existed and was lifted.

    This entry point is reserved for a future admin endpoint (no
    WU-6 route lands it yet). It is wired here so the WU-6 commit
    is the single landing for the audit-action constants AND the
    service surface that uses them, leaving the route surface as a
    pure HTTP-binding layer.
    """
    normalized = (address or "").strip()
    if not normalized:
        return False

    stmt = (
        select(EmailSuppression)
        .where(func.lower(EmailSuppression.address) == func.lower(normalized))
        .limit(1)
    )
    existing: EmailSuppression | None = session.execute(stmt).scalar_one_or_none()
    if existing is None:
        return False

    before_payload = {
        "address": existing.address,
        "reason": existing.reason,
        "source_event_id": existing.source_event_id,
        "first_suppressed_at": existing.first_suppressed_at.isoformat()
        if existing.first_suppressed_at is not None
        else None,
    }
    row_id = existing.id

    session.delete(existing)

    audit = AdminAuditLog(
        actor_key_prefix=None,
        actor_permissions=None,
        actor_label=actor_label,
        admin_id=_SYSTEM_ACTOR_TENANT,
        luciel_instance_id=None,
        action=ACTION_EMAIL_SUPPRESSION_CLEARED,
        resource_type=RESOURCE_EMAIL_SUPPRESSION,
        resource_pk=row_id,
        resource_natural_id=normalized.lower(),
        before_json=before_payload,
        after_json=None,
        note=note,
    )
    session.add(audit)
    session.flush()

    logger.info(
        "email_suppression: cleared address=%s row_id=%s",
        normalized,
        row_id,
    )
    return True
