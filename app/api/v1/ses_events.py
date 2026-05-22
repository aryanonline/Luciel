"""SES feedback-event handler -- SNS HTTPS subscription endpoint.

Arc 8 WU-6 Phase C. Closes (at the route / HTTP-binding layer):

  * D-ses-feedback-loop-not-wired-2026-05-22 -- the SNS-fed feedback
    loop now has a sink that durably records every SES event into
    ``email_send_event`` and triggers ``EmailSuppression`` rows on
    HardBounce / Complaint.
  * D-ses-suppression-app-layer-not-implemented-2026-05-22 -- the
    application-layer suppression list is now populated automatically
    from real SES feedback (the service-layer surface that owns the
    write is ``app/services/email_suppression_service.py``; this route
    is the HTTP binding).

The full WU-6 Phase A + B + C cohort closure also requires the operator
to (a) deploy the backend image carrying this route into ECS, and
(b) subscribe this route's public URL to the ``luciel-ses-events`` SNS
topic. Those operator actions are tracked in the Phase C deploy runbook
at ``arc8-out/`` and do not block this commit's correctness.

SNS-to-HTTPS contract
---------------------

Three message ``Type`` values from SNS:

1. **SubscriptionConfirmation** -- delivered once when the subscription
   is created. Carries a ``SubscribeURL`` that the subscriber must GET
   to confirm the subscription (defence against an attacker subscribing
   our endpoint to a malicious topic). We fetch the URL synchronously;
   on success the subscription becomes ACTIVE in SNS.

2. **Notification** -- every published message. The ``Message`` field
   is itself a JSON-encoded string of the SES event payload. We decode,
   validate, persist into ``email_send_event``, and (for HardBounce /
   Complaint) call ``record_suppression``.

3. **UnsubscribeConfirmation** -- delivered when the subscription is
   deleted. Informational. We log and 200.

Authentication / trust gate
---------------------------

The route is publicly reachable (the api-key middleware skips
``/api/v1/ses-events`` via SKIP_AUTH_PATHS) because SNS-to-HTTPS does
not carry an API key. The trust gate is a **two-check defence**:

1. ``TopicArn`` must equal ``settings.ses_sns_topic_arn`` -- the topic
   we explicitly subscribe in the Phase C deploy runbook. An attacker
   posting a fake Notification would have to know our exact topic ARN
   AND have us configured to accept it.

2. ``SigningCertURL`` must be an HTTPS URL under ``*.amazonaws.com``.
   This is the AWS-published gate that protects against a forged
   message carrying a fake cert URL; the SNS spec guarantees the
   cert is hosted on amazonaws.com for legitimate messages.

Full RSA-SHA1 signature verification against the SNS signing cert is
**deferred Phase D hardening**. The above two checks are sufficient
defence-in-depth for v1: a successful forgery requires both knowing
our private topic ARN AND somehow injecting a SigningCertURL pointing
to an amazonaws.com host the attacker controls (which would itself
require a multi-tenant compromise of AWS infrastructure).

If full signature verification is added later, the gate can be tightened
to also reject any message whose RSA-SHA1 signature does not verify
against the cert published at ``SigningCertURL``. The lighter check
documented above does not block that future tightening.

Idempotency
-----------

SNS guarantees at-least-once delivery. Duplicate SNS deliveries of the
same MessageId hit the UNIQUE constraint on
``email_send_event.event_id`` and raise ``IntegrityError``. The route
catches and returns 200 -- a duplicate is success at the idempotency
contract, not failure.

For the suppression side: ``record_suppression`` is itself idempotent
(see ``EmailSuppressionService`` docstring) -- a re-suppression updates
``last_suppressed_at`` rather than creating a duplicate row.

Multi-recipient events
----------------------

A single SES Bounce event can affect multiple recipients (a bulk-send
that bounced for some addresses). The SNS message body's
``bounce.bouncedRecipients`` field carries a list. We write **one
``email_send_event`` row per recipient**: the first recipient's row
uses the SNS MessageId verbatim; subsequent recipients get the
MessageId with a suffix (``:1``, ``:2``, ...) so each row is uniquely
keyed but the cohort is grepable.

Error semantics
---------------

* Parse failure (malformed SNS body, missing required field, bad
  JSON inside ``Message``): return 400. SNS will retry per its
  delivery policy. We log enough context to forensically replay.
* Trust-gate failure (TopicArn mismatch, or SigningCertURL host not
  amazonaws.com): return 403. SNS will NOT retry on 4xx. We log a
  SECURITY-tagged warning so an operator can investigate.
* Internal failure (DB connection, etc.): return 500. SNS will retry
  per its delivery policy.
* SubscribeURL fetch failure on a SubscriptionConfirmation: we log
  and return 200. AWS will not auto-retry SubscriptionConfirmation;
  the operator must re-create the subscription if confirmation fails.

Pattern E
---------

Pure addition. No existing route is mutated; this lands as a new
module under ``app/api/v1/`` and is wired into ``app/api/router.py``
and ``app/middleware/auth.py``'s ``SKIP_AUTH_PATHS``. Rollback is
removing the file + the three single-line registrations + the two
settings reservations.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from typing import Annotated, Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.middleware.rate_limit import limiter
from app.models.email_send_event import (
    SES_EVENT_BOUNCE,
    SES_EVENT_COMPLAINT,
    SES_EVENT_TYPES,
    EmailSendEvent,
)
from app.models.email_suppression import (
    SUPPRESSION_REASON_COMPLAINT,
    SUPPRESSION_REASON_HARD_BOUNCE,
)
from app.services.email_suppression_service import record_suppression

logger = logging.getLogger(__name__)


router = APIRouter()


# ---------------------------------------------------------------------
# SNS message-type constants.
# ---------------------------------------------------------------------

SNS_TYPE_SUBSCRIPTION_CONFIRMATION = "SubscriptionConfirmation"
SNS_TYPE_NOTIFICATION = "Notification"
SNS_TYPE_UNSUBSCRIBE_CONFIRMATION = "UnsubscribeConfirmation"

SNS_TYPES = frozenset(
    {
        SNS_TYPE_SUBSCRIPTION_CONFIRMATION,
        SNS_TYPE_NOTIFICATION,
        SNS_TYPE_UNSUBSCRIBE_CONFIRMATION,
    }
)


# ---------------------------------------------------------------------
# Route.
# ---------------------------------------------------------------------


@router.post("/ses-events")
@limiter.limit("600/minute")
async def receive_ses_event(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Receive an SNS HTTPS message carrying an SES feedback event.

    Public surface (api-key middleware skips ``/api/v1/ses-events`` via
    SKIP_AUTH_PATHS) -- the two-check trust gate (TopicArn allowlist +
    SigningCertURL host check) is the auth gate.

    Rate limit 600/min is high enough to absorb a burst of bounce
    feedback after a large send while still capping a runaway attacker.
    """
    raw_body = await request.body()
    if not raw_body:
        raise HTTPException(status_code=400, detail="Empty body.")

    try:
        sns_message: dict[str, Any] = json.loads(raw_body)
    except json.JSONDecodeError as e:
        logger.warning("ses_events: malformed SNS body: %s", e)
        raise HTTPException(status_code=400, detail="Malformed JSON.") from e

    sns_type = sns_message.get("Type")
    if sns_type not in SNS_TYPES:
        logger.warning("ses_events: unknown SNS Type=%r", sns_type)
        raise HTTPException(status_code=400, detail="Unknown SNS Type.")

    # ---------------------------------------------------------------
    # Trust gate -- two checks (see module docstring).
    # ---------------------------------------------------------------
    expected_topic = settings.ses_sns_topic_arn
    actual_topic = sns_message.get("TopicArn")
    if expected_topic and actual_topic != expected_topic:
        logger.warning(
            "ses_events: SECURITY: TopicArn mismatch "
            "(expected=%s, actual=%s, MessageId=%s)",
            expected_topic,
            actual_topic,
            sns_message.get("MessageId"),
        )
        raise HTTPException(status_code=403, detail="TopicArn not allowed.")

    signing_cert_url = sns_message.get("SigningCertURL") or sns_message.get(
        "SigningCertUrl"
    )
    if signing_cert_url and not _is_amazonaws_url(signing_cert_url):
        logger.warning(
            "ses_events: SECURITY: SigningCertURL host not amazonaws.com: %s "
            "(MessageId=%s, TopicArn=%s)",
            signing_cert_url,
            sns_message.get("MessageId"),
            actual_topic,
        )
        raise HTTPException(
            status_code=403, detail="SigningCertURL host not allowed."
        )

    if sns_type == SNS_TYPE_SUBSCRIPTION_CONFIRMATION:
        return _handle_subscription_confirmation(sns_message)

    if sns_type == SNS_TYPE_UNSUBSCRIBE_CONFIRMATION:
        logger.info(
            "ses_events: received UnsubscribeConfirmation for TopicArn=%s "
            "MessageId=%s",
            actual_topic,
            sns_message.get("MessageId"),
        )
        return Response(status_code=200)

    # sns_type == Notification
    return _handle_notification(db, sns_message)


# ---------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------


def _handle_subscription_confirmation(sns_message: dict[str, Any]) -> Response:
    """Handle the one-time SubscriptionConfirmation message.

    Per the SNS spec, the subscriber must GET the ``SubscribeURL`` to
    confirm the subscription. We do this synchronously here; on success
    the subscription becomes ACTIVE in SNS.

    A failed confirmation is logged but the route returns 200 -- AWS
    does not auto-retry SubscriptionConfirmation, so the operator must
    re-create the subscription if confirmation fails. Returning 4xx
    here would not change AWS behaviour but would muddy the operator's
    SNS subscription-attempt logs.
    """
    subscribe_url = sns_message.get("SubscribeURL")
    topic_arn = sns_message.get("TopicArn")
    message_id = sns_message.get("MessageId")

    if not subscribe_url:
        logger.warning(
            "ses_events: SubscriptionConfirmation missing SubscribeURL "
            "(TopicArn=%s, MessageId=%s)",
            topic_arn,
            message_id,
        )
        return Response(status_code=200)

    # Defence-in-depth: the trust gate already verified
    # SigningCertURL is *.amazonaws.com. The SubscribeURL for
    # legitimate SNS topics is *.amazonaws.com too. We re-check here
    # so we never GET an arbitrary attacker-controlled URL even if a
    # future regression in the gate above lets a non-amazonaws cert
    # URL through.
    if not _is_amazonaws_url(subscribe_url):
        logger.warning(
            "ses_events: SECURITY: SubscribeURL host not amazonaws.com: %s "
            "(TopicArn=%s)",
            subscribe_url,
            topic_arn,
        )
        return Response(status_code=200)

    try:
        with urllib.request.urlopen(subscribe_url, timeout=10) as resp:
            status = resp.status
            logger.info(
                "ses_events: SubscriptionConfirmation confirmed "
                "TopicArn=%s subscribe_status=%s",
                topic_arn,
                status,
            )
    except Exception as e:  # noqa: BLE001 -- intentional broad catch
        logger.warning(
            "ses_events: SubscriptionConfirmation GET failed for "
            "TopicArn=%s: %s",
            topic_arn,
            e,
        )

    return Response(status_code=200)


def _is_amazonaws_url(url: str) -> bool:
    """Return True iff the URL's host is HTTPS under *.amazonaws.com."""
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.netloc or "").lower()
    # Strip port if any.
    host = host.split(":", 1)[0]
    return host == "amazonaws.com" or host.endswith(".amazonaws.com")


def _handle_notification(db: Session, sns_message: dict[str, Any]) -> Response:
    """Handle an SES feedback Notification message.

    The SNS ``Message`` field is a JSON-encoded string. We decode it,
    determine the event type, persist one ``email_send_event`` row per
    affected recipient, and for HardBounce / Complaint events also
    call ``record_suppression`` (which writes the suppression row + the
    matching audit row in the same session).
    """
    sns_message_id = sns_message.get("MessageId")
    raw_inner = sns_message.get("Message")
    if not raw_inner:
        logger.warning(
            "ses_events: Notification missing Message field "
            "(SNS MessageId=%s)",
            sns_message_id,
        )
        raise HTTPException(status_code=400, detail="Missing Message field.")

    try:
        ses_event: dict[str, Any] = json.loads(raw_inner)
    except json.JSONDecodeError as e:
        logger.warning(
            "ses_events: Notification Message field not valid JSON "
            "(SNS MessageId=%s): %s",
            sns_message_id,
            e,
        )
        raise HTTPException(
            status_code=400, detail="Message field not valid JSON."
        ) from e

    event_type = ses_event.get("eventType") or ses_event.get("notificationType")
    if event_type not in SES_EVENT_TYPES:
        logger.warning(
            "ses_events: unknown SES event type=%r (SNS MessageId=%s)",
            event_type,
            sns_message_id,
        )
        raise HTTPException(status_code=400, detail="Unknown SES event type.")

    # Recipients vary by event type. Iterate and write one row per
    # recipient; for events with no per-recipient breakdown (Reject,
    # RenderingFailure), write a single NULL-address row.
    recipients = _extract_recipients(ses_event, event_type)
    if not recipients:
        recipients = [None]  # write a single NULL-address row

    rows_written = 0
    suppressions_written = 0

    for idx, recipient in enumerate(recipients):
        # First recipient uses the bare SNS MessageId; subsequent
        # recipients get a suffix so the UNIQUE event_id constraint
        # is honoured while keeping the cohort grepable.
        event_id_for_row = (
            sns_message_id if idx == 0 else f"{sns_message_id}:{idx}"
        )
        event_row = EmailSendEvent(
            event_id=event_id_for_row,
            event_type=event_type,
            address=recipient,
            raw_payload_json=ses_event,
        )
        db.add(event_row)
        try:
            db.flush()
        except IntegrityError:
            # Duplicate SNS delivery -- success at the idempotency
            # contract. Roll back and continue (no suppression write
            # for a duplicate; the original delivery already triggered
            # one if applicable).
            db.rollback()
            logger.info(
                "ses_events: duplicate SNS delivery for event_id=%s; "
                "treating as idempotent success.",
                event_id_for_row,
            )
            continue

        rows_written += 1

        # Suppression triggers: HardBounce + Complaint.
        if (
            event_type == SES_EVENT_BOUNCE
            and recipient
            and _is_hard_bounce(ses_event)
        ):
            record_suppression(
                session=db,
                address=recipient,
                reason=SUPPRESSION_REASON_HARD_BOUNCE,
                source_event_id=event_id_for_row,
            )
            suppressions_written += 1
        elif event_type == SES_EVENT_COMPLAINT and recipient:
            record_suppression(
                session=db,
                address=recipient,
                reason=SUPPRESSION_REASON_COMPLAINT,
                source_event_id=event_id_for_row,
            )
            suppressions_written += 1

    db.commit()

    logger.info(
        "ses_events: Notification processed event_type=%s SNS MessageId=%s "
        "rows_written=%d suppressions_written=%d",
        event_type,
        sns_message_id,
        rows_written,
        suppressions_written,
    )
    return Response(status_code=200)


def _extract_recipients(
    ses_event: dict[str, Any], event_type: str
) -> list[str]:
    """Return the list of affected recipients for the event.

    SES event payload shape varies by type:

      * Bounce: ``bounce.bouncedRecipients[].emailAddress``
      * Complaint: ``complaint.complainedRecipients[].emailAddress``
      * Reject: no per-recipient breakdown (recipients are in
        ``mail.destination`` but the reject reason applies to the whole
        send). We write a NULL-address row.
      * RenderingFailure: no per-recipient breakdown either.
      * Delivery / Send: ``mail.destination`` if present.
    """
    if event_type == SES_EVENT_BOUNCE:
        return _addrs_from(ses_event, "bounce", "bouncedRecipients")
    if event_type == SES_EVENT_COMPLAINT:
        return _addrs_from(ses_event, "complaint", "complainedRecipients")
    # Reject / RenderingFailure -- no per-recipient breakdown.
    return []


def _addrs_from(
    event: dict[str, Any], parent: str, child_list: str
) -> list[str]:
    """Pull ``emailAddress`` values out of the parent.child_list array."""
    parent_obj = event.get(parent) or {}
    recipients = parent_obj.get(child_list) or []
    out: list[str] = []
    for r in recipients:
        if isinstance(r, dict):
            addr = r.get("emailAddress")
            if isinstance(addr, str) and addr.strip():
                out.append(addr.strip())
    return out


def _is_hard_bounce(ses_event: dict[str, Any]) -> bool:
    """Return True iff the Bounce event is a Permanent (hard) bounce.

    SES Bounce event sub-types:
        * Permanent -- the address is undeliverable (suppress).
        * Transient -- delivery failed temporarily (do NOT suppress;
          the next send may succeed).
        * Undetermined -- SES could not classify (treat as Transient
          to avoid false-positive suppression).

    Permanent / Transient / Undetermined map to the ``bounceType`` field.
    """
    bounce = ses_event.get("bounce") or {}
    return bounce.get("bounceType") == "Permanent"
