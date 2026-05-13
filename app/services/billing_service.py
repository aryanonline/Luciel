"""Step 30a: BillingService.

Read-mostly facade around the ``subscriptions`` table plus the two
thin Stripe write paths (checkout creation, portal session creation).
The heavy lifting -- minting tenants on payment, cascading
deactivation on cancel -- lives in ``BillingWebhookService``; this
class is what the API routes call from the request path.

Separation rationale:

  - Request-path code (BillingService) runs inside a request, has an
    AuditContext built from request.state, and either reads
    subscription state for the cookied user or *initiates* a Stripe
    operation that will eventually round-trip through a webhook.
  - Webhook-path code (BillingWebhookService) runs from a Stripe
    POST, has a synthetic AuditContext labelled ``stripe_webhook``,
    and is the only place that mutates ``subscriptions`` rows.

Keeping the two paths separate means a future change to the webhook
handler (e.g. adding a new event type) cannot accidentally tee into
the request-path audit trail, and a route-level test does not need a
running webhook listener.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.integrations.stripe import StripeClient
from app.models.subscription import (
    Subscription,
    TIER_INDIVIDUAL,
)

logger = logging.getLogger(__name__)


class BillingNotConfiguredError(Exception):
    """Raised when a billing route is called but Stripe is not configured.

    The route layer maps this to HTTP 501 Not Implemented so a CI run
    against a backend without billing env vars exits with a clear
    message rather than a 500 from a Stripe library error.
    """


class BillingService:
    def __init__(self, db: Session, stripe_client: StripeClient) -> None:
        self.db = db
        self.stripe = stripe_client

    # -----------------------------------------------------------------
    # Configuration gate -- routes call this first
    # -----------------------------------------------------------------

    def require_configured(self) -> None:
        """Raises ``BillingNotConfiguredError`` if Stripe is not configured."""
        if not self.stripe.is_configured:
            raise BillingNotConfiguredError("Stripe secret key not configured.")
        if not settings.stripe_price_individual:
            raise BillingNotConfiguredError("Stripe Individual price id not configured.")

    # -----------------------------------------------------------------
    # Checkout
    # -----------------------------------------------------------------

    def create_checkout(
        self,
        *,
        email: str,
        display_name: str,
        tier: str = TIER_INDIVIDUAL,
    ) -> dict[str, str]:
        """Create a Stripe Checkout session and return the redirect URL + id.

        v1 only accepts the Individual tier. Anything else is a 400 at
        the route layer; this service does not silently fall back.

        We pass two pieces of metadata into Stripe so the webhook
        handler can correlate without an extra Stripe API call:

          - ``luciel_email``         -- the email the buyer entered. We
                                        copy it onto the eventual User
                                        and Subscription rows.
          - ``luciel_display_name``  -- carried onto TenantConfig.display_name.
          - ``luciel_tier``          -- 'individual' at v1.

        The Stripe Checkout session also sets ``customer_email`` so
        Stripe pre-fills the email field; the metadata copy is the
        canonical value for our webhook because Stripe may transform
        the customer-side email during a Customer object create.
        """
        self.require_configured()
        if tier != TIER_INDIVIDUAL:
            raise ValueError(
                f"Self-serve checkout is Individual-only at v1; got tier={tier!r}."
            )

        # Build the redirect URLs. We accept {CHECKOUT_SESSION_ID} as
        # a Stripe placeholder in billing_success_url; Stripe substitutes
        # the real id when redirecting the buyer.
        success_url = settings.billing_success_url
        cancel_url = settings.billing_cancel_url

        metadata = {
            "luciel_email": email,
            "luciel_display_name": display_name,
            "luciel_tier": tier,
        }

        session = self.stripe.create_checkout_session(
            customer_email=email,
            price_id=settings.stripe_price_individual,
            success_url=success_url,
            cancel_url=cancel_url,
            trial_period_days=settings.billing_trial_days or None,
            metadata=metadata,
        )

        logger.info(
            "billing: checkout session created stripe_id=%s email=%s tier=%s",
            getattr(session, "id", "?"),
            email,
            tier,
        )
        return {
            "checkout_url": session.url,
            "session_id": session.id,
        }

    # -----------------------------------------------------------------
    # Portal
    # -----------------------------------------------------------------

    def create_portal_session_for_user(self, *, user_id: Any) -> str:
        """Look up the cookied user's current subscription and create a portal session.

        Raises ``LookupError`` if the user has no active subscription
        on file -- the route layer maps this to 404.
        """
        self.require_configured()
        sub = self.get_active_subscription_for_user(user_id=user_id)
        if sub is None:
            raise LookupError("No active subscription found for this user.")

        return_url = f"{settings.marketing_site_url.rstrip('/')}/account/billing"
        portal = self.stripe.create_portal_session(
            customer_id=sub.stripe_customer_id,
            return_url=return_url,
        )
        return portal.url

    # -----------------------------------------------------------------
    # Reads
    # -----------------------------------------------------------------

    def get_active_subscription_for_user(self, *, user_id: Any) -> Subscription | None:
        """Return the user's currently-active subscription, if any.

        "Active" here is the Pattern E soft-delete sense -- ``active=True``
        on the row. Whether that row is *entitled* (Stripe status in
        ENTITLED_STATUSES) is a separate read via ``Subscription.is_entitled``.
        """
        stmt = (
            select(Subscription)
            .where(Subscription.user_id == user_id, Subscription.active.is_(True))
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
        return self.db.execute(stmt).scalars().first()

    def get_subscription_by_stripe_id(self, *, stripe_subscription_id: str) -> Subscription | None:
        stmt = select(Subscription).where(
            Subscription.stripe_subscription_id == stripe_subscription_id
        )
        return self.db.execute(stmt).scalars().first()

    def get_active_subscription_for_tenant(self, *, tenant_id: str) -> Subscription | None:
        stmt = (
            select(Subscription)
            .where(Subscription.tenant_id == tenant_id, Subscription.active.is_(True))
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
        return self.db.execute(stmt).scalars().first()
