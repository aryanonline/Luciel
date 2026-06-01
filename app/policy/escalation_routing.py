"""Arc 14 U2 — escalation routing: WHO + HOW (tier-shaped channels).

The §3.4.5 Escalation Judgment Module decides WHEN to escalate (the four
fixed signals). This module decides WHO is notified and over WHICH
channels — and that split is doctrinal: triggers are NOT
admin-configurable, but the contact + channel routing IS per-instance /
per-tier configuration.

Two concerns live here:

  * **Tier-shaped admin-notification channel set** — independent of the
    customer-facing channels. The platform notifies the human operator
    over a channel set that widens with tier:

        Free       → email only
        Pro        → email + SMS
        Enterprise → email + SMS + Slack + custom paths

    The admin-notification email is the single-platform SES path (NOT a
    per-instance Twilio number). The ACTUAL send is gated behind the
    existing ``channels_live_provisioning_enabled`` live-switch so tests
    never send real email / SMS — they assert the ROUTING DECISION.

  * **Escalation-contact lookup (WHO)** — per-instance config of who to
    notify and how to reach them. NOTE (documented ambiguity, see PR):
    v2 has NO escalation-contact surface on the Instance model. This
    module ships the MINIMAL contact lookup the escalation flow needs,
    falling back to the Admin's own contact when no per-instance config
    exists. A first-class admin-authored contact surface is deferred
    (a later arc / unit can replace ``resolve_contact`` without touching
    the judge or the channel-set policy).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.policy.entitlements import TIER_ENTERPRISE, TIER_FREE, TIER_PRO

logger = logging.getLogger(__name__)


# Notification-channel ids for the ADMIN-facing escalation notice. These
# are intentionally distinct from the customer-facing channel ids
# (widget / email / sms in app/models/instance.py): a customer talking
# over the widget can still trigger an SMS to the operator.
NOTIFY_EMAIL = "email"
NOTIFY_SMS = "sms"
NOTIFY_SLACK = "slack"
NOTIFY_CUSTOM = "custom"


# Tier → ordered admin-notification channel set. Fixed by tier, NOT
# admin-configurable (the WHEN is doctrinal; the channel SHAPE is a tier
# entitlement). Enterprise's "custom paths" is represented by NOTIFY_CUSTOM
# — the concrete webhook/PagerDuty target is resolved from contact config.
_CHANNELS_BY_TIER: dict[str, tuple[str, ...]] = {
    TIER_FREE: (NOTIFY_EMAIL,),
    TIER_PRO: (NOTIFY_EMAIL, NOTIFY_SMS),
    TIER_ENTERPRISE: (NOTIFY_EMAIL, NOTIFY_SMS, NOTIFY_SLACK, NOTIFY_CUSTOM),
}


def channels_for_tier(tier: str) -> tuple[str, ...]:
    """Return the admin-notification channel set for a tier.

    Fail-closed to the Free set (email only) when the tier is unknown —
    an operator always gets at least an email; an unrecognised tier
    never silently widens to SMS/Slack.
    """
    return _CHANNELS_BY_TIER.get(tier, _CHANNELS_BY_TIER[TIER_FREE])


@dataclass(frozen=True)
class EscalationContact:
    """WHO to notify for an escalation, and over which addresses.

    ``channels`` is the tier-shaped channel set (the HOW). The address
    fields are best-effort — a missing address for a channel in the set
    means "the routing wanted this channel but had no target," which the
    notifier records rather than sending. ``email`` is the single-platform
    SES recipient; ``sms_to`` an E.164; ``slack_target`` a channel/webhook
    id; ``custom_targets`` arbitrary Enterprise paths.
    """

    admin_id: str
    tier: str
    channels: tuple[str, ...]
    email: str | None = None
    sms_to: str | None = None
    slack_target: str | None = None
    custom_targets: tuple[str, ...] = field(default_factory=tuple)


def resolve_contact(
    db,
    *,
    admin_id: str,
    luciel_instance_id: int | None,
) -> EscalationContact:
    """Resolve the escalation contact (WHO + HOW) for a turn.

    Looks up the Admin's tier to shape the channel set. v2 has no
    per-instance escalation-contact surface and no contact-email column
    on the Admin row yet (documented ambiguity, see PR), so the address
    fields are left unresolved — the tier-shaped channel set IS the
    routing decision this unit makes; binding concrete addresses is
    deferred to the unit that adds the contact surface. Never raises: a
    lookup failure degrades to the Free channel set so the escalation
    decision + event row still land (the notify leg is best-effort).
    """
    tier = TIER_FREE
    try:
        from sqlalchemy import select

        from app.models.admin import Admin
        from app.policy.entitlements import TIER_ENTITLEMENTS

        resolved_tier = db.execute(
            select(Admin.tier).where(Admin.id == admin_id)
        ).scalar_one_or_none()
        if resolved_tier in TIER_ENTITLEMENTS:
            tier = resolved_tier
    except Exception as exc:  # noqa: BLE001 — never crash the escalation
        logger.warning(
            "escalation contact resolution failed: exc_class=%s admin_prefix=%s "
            "— degrading to Free channel set",
            type(exc).__name__, (admin_id or "")[:8],
        )

    return EscalationContact(
        admin_id=admin_id,
        tier=tier,
        channels=channels_for_tier(tier),
    )
