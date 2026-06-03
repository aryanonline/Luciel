"""Billing-period anchor resolution for the conversation budget (Arc 18).

The budget counter key is ``(admin_id, instance_id, billing_period_start)``
(§3.4.1b). ``billing_period_start`` advances on RESET, and reset is a
Stripe webhook (``invoice.paid`` / ``customer.subscription.renewed``) —
NOT the calendar month — for PAYING tiers.

Free has no Stripe subscription and never bills, so there is no Stripe
cycle to anchor against. Free still needs a deterministic period_start
that (a) makes the Redis key well-defined and (b) rolls over WITHOUT a
webhook so the 200/inst/mo cap resets predictably. We anchor the Free
window to the ADMIN's signup day-of-month (``admins.created_at``): the
current period starts on the most recent occurrence of that day-of-month
at-or-before now. This is per-admin deterministic, rolls monthly with no
external trigger, and is NOT a flat calendar-month anchor (which would
contradict the per-account billing-anchor doctrine). Documented in
ARC18_BACKEND_REPORT.md.

The anchor is returned as an ISO-8601 date string (UTC), which is what
the Redis key embeds.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

logger = logging.getLogger(__name__)


def _to_utc_date(dt: datetime) -> date:
    if dt.tzinfo is None:
        return dt.date()
    return dt.astimezone(timezone.utc).date()


def free_period_start(signup_at: datetime, *, now: datetime | None = None) -> str:
    """Signup-day-anchored monthly window start for a Free admin.

    Returns the ISO date (UTC) of the most recent signup-day-of-month
    occurrence at-or-before ``now``. Days past the end of a short month
    (e.g. signup on the 31st in February) clamp to the last day of that
    month.
    """
    now_d = _to_utc_date(now or datetime.now(timezone.utc))
    anchor_day = _to_utc_date(signup_at).day

    def clamp(year: int, month: int) -> date:
        last = calendar.monthrange(year, month)[1]
        return date(year, month, min(anchor_day, last))

    candidate = clamp(now_d.year, now_d.month)
    if candidate > now_d:
        # This month's anchor day hasn't arrived yet — use last month's.
        year = now_d.year - 1 if now_d.month == 1 else now_d.year
        month = 12 if now_d.month == 1 else now_d.month - 1
        candidate = clamp(year, month)
    return candidate.isoformat()


def period_start_iso(dt: datetime | None) -> str:
    """Normalise a Stripe ``current_period_start`` datetime to the ISO
    date string used in the counter key. ``None`` (subscription with no
    cycle dates yet) falls back to the UTC epoch date so the key stays
    well-defined; the next reset advances it to the real cycle anchor.
    """
    if dt is None:
        return "1970-01-01"
    return _to_utc_date(dt).isoformat()


@dataclass(frozen=True)
class BillingContext:
    """The (tier, cadence, period anchor) a turn needs to key its budget.

    ``tier`` and ``cadence`` drive the budget/overage entitlement lookup;
    ``period_start`` is the ISO date embedded in the Redis counter key.
    """

    tier: str
    cadence: str
    period_start: str


def resolve_billing_context(
    db, *, admin_id: str, now: datetime | None = None
) -> BillingContext:
    """Resolve the budget billing context for an Admin.

    Paying tiers (Pro/Enterprise) read their active Subscription's
    ``tier``, ``billing_cadence``, and ``current_period_start`` — the
    period anchor that the Stripe reset webhook advances. Free admins
    have NO Subscription row (Gap 1 lock), so the anchor is the
    signup-day monthly window (``free_period_start`` on ``admins.created_at``).

    Fail-closed to Free with a calendar-safe signup-anchored window on
    any lookup failure, so the counter key is always well-defined and a
    DB hiccup never crashes the turn.
    """
    from app.policy.entitlements import (
        CADENCE_MONTHLY,
        TIER_ENTITLEMENTS,
        TIER_FREE,
    )

    try:
        from sqlalchemy import select

        from app.models.admin import Admin
        from app.models.subscription import Subscription

        sub = db.execute(
            select(Subscription)
            .where(Subscription.admin_id == admin_id, Subscription.active.is_(True))
            .order_by(Subscription.id.desc())
        ).scalars().first()

        if sub is not None and sub.tier in TIER_ENTITLEMENTS and sub.tier != TIER_FREE:
            return BillingContext(
                tier=sub.tier,
                cadence=sub.billing_cadence or CADENCE_MONTHLY,
                period_start=period_start_iso(sub.current_period_start),
            )

        # Free (or no paying subscription): signup-anchored monthly window.
        signup_at = db.execute(
            select(Admin.created_at).where(Admin.id == admin_id)
        ).scalar_one_or_none()
        anchor = (
            free_period_start(signup_at, now=now)
            if signup_at is not None
            else _to_utc_date(now or datetime.now(timezone.utc)).replace(day=1).isoformat()
        )
        return BillingContext(
            tier=TIER_FREE, cadence=CADENCE_MONTHLY, period_start=anchor
        )
    except Exception as exc:  # noqa: BLE001 — never crash the turn
        logger.warning(
            "billing context resolution failed: exc_class=%s admin_prefix=%s "
            "— defaulting to Free signup-window floor",
            type(exc).__name__,
            (admin_id or "")[:8],
        )
        anchor = _to_utc_date(now or datetime.now(timezone.utc)).replace(day=1).isoformat()
        return BillingContext(
            tier=TIER_FREE, cadence=CADENCE_MONTHLY, period_start=anchor
        )
