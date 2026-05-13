"""Step 30a: billing schemas.

Pydantic request/response models for the billing surface. Kept narrow:
the marketing site only needs four shapes (start checkout, claim the
returned session, fetch portal URL, fetch current status). Anything
richer (invoices, transactions) is on Stripe and reached via the
Customer Portal, not through this API.

Naming convention matches the rest of ``app/schemas``: ``*Request``
for inbound bodies, ``*Response`` for returned shapes; ``model_config``
with ``from_attributes=True`` only where the response is built from a
SQLAlchemy row.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


# ---------------------------------------------------------------------
# POST /api/v1/billing/checkout
# ---------------------------------------------------------------------

class CheckoutSessionRequest(BaseModel):
    """Inbound request for the checkout-session creation endpoint.

    The marketing site collects email + display_name on the /signup
    page and posts them here. The buyer's identity at this point is
    *prospective* -- they have not paid yet -- so no User row is
    created and no tenant is minted. The webhook does that work once
    Stripe confirms payment.

    Why tenant_id is OPTIONAL here:
      v1 self-serve mints a fresh tenant for every Individual checkout.
      A buyer who is already a Team / Company member upgrading to a
      paid Individual seat is sales-assisted at v1 (deliberate non-goal
      tracked in DRIFTS as D-billing-team-company-not-self-serve).
      The field is reserved so a future Step 30a.1 can route an
      Individual upgrade onto an existing tenant without an API change.
    """
    email: EmailStr = Field(
        ..., description="Email of the buyer. Becomes the User.email "
                        "and Subscription.customer_email."
    )
    display_name: str = Field(
        ..., min_length=1, max_length=200,
        description="Buyer's name. Carried into the Stripe customer "
                    "object and onto the eventual TenantConfig.",
    )
    tier: str = Field(
        default="individual",
        description="Tier slug. v1 only accepts 'individual'.",
    )
    tenant_id: str | None = Field(
        default=None,
        description="Reserved for Step 30a.1 upgrade flows; ignored at v1.",
    )


class CheckoutSessionResponse(BaseModel):
    """Stripe redirect URL the marketing site will navigate to."""
    checkout_url: str = Field(..., description="Stripe-hosted Checkout URL.")
    session_id: str = Field(..., description="Stripe Checkout session id (cs_...).")


# ---------------------------------------------------------------------
# POST /api/v1/billing/onboarding/claim
# ---------------------------------------------------------------------

class OnboardingClaimRequest(BaseModel):
    """The marketing site posts this after Stripe redirects to
    /onboarding?session_id={CHECKOUT_SESSION_ID}.

    At that moment the webhook may or may not have arrived yet. The
    backend handles both orderings: if the subscription row already
    exists, we mint a magic link immediately; if not, we accept the
    claim and let the webhook drive the email send when it arrives.
    """
    session_id: str = Field(
        ..., min_length=10, max_length=120,
        description="The Stripe checkout session id from the redirect.",
    )


class OnboardingClaimResponse(BaseModel):
    """Tells the marketing site what to render to the buyer.

    state:
      'pending'  -- webhook hasn't landed yet; show "we sent you an
                    email" optimistically (we will when it arrives).
      'ready'    -- subscription row exists; magic link emailed now;
                    show "we sent you an email."
      'unknown'  -- session_id does not match anything Stripe knows;
                    show a generic error.
    """
    state: str = Field(..., description="One of 'pending', 'ready', 'unknown'.")
    email_sent_to: str | None = Field(
        default=None,
        description="The email address the magic link went to. "
                    "Echoed back so the marketing site can show "
                    "'check <email>' without storing it client-side.",
    )


# ---------------------------------------------------------------------
# POST /api/v1/billing/portal
# ---------------------------------------------------------------------

class PortalSessionResponse(BaseModel):
    """The Stripe Customer Portal URL the cookied user should redirect to.

    The portal handles plan changes, payment-method updates, and the
    cancel flow. We do not implement any of those primitives ourselves
    at v1 -- the portal is the entire surface.
    """
    portal_url: str = Field(..., description="Stripe-hosted portal URL.")


# ---------------------------------------------------------------------
# GET /api/v1/billing/me
# ---------------------------------------------------------------------

class SubscriptionStatusResponse(BaseModel):
    """Read-only billing state for the cookied user.

    Surfaces only the fields the Account/billing UI renders. The full
    Stripe object is *not* exposed -- a forensic engineer reads it
    out of ``subscriptions.provider_snapshot`` instead.
    """
    model_config = ConfigDict(from_attributes=True)

    tenant_id: str
    tier: str
    status: str
    active: bool
    is_entitled: bool
    current_period_start: datetime | None
    current_period_end: datetime | None
    trial_end: datetime | None
    cancel_at_period_end: bool
    canceled_at: datetime | None
    customer_email: str
