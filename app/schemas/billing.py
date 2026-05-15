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
from typing import Literal

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

    Step 30a.1 lifted the v1 Individual-only carve-out: ``tier`` is now
    a ``Literal['individual','team','company']`` and the new
    ``billing_cadence`` field accepts ``Literal['monthly','annual']``.
    The (tier, cadence) pair routes to a Stripe Price ID via
    ``BillingService.resolve_price_id`` — see the table in that module.

    Why tenant_id is OPTIONAL here:
      v1 self-serve mints a fresh tenant for every checkout. A buyer
      who is already a Team / Company member upgrading to a paid
      Individual seat (Sarah → department, CANONICAL_RECAP Q5) is a
      Step 38 concern; the field is reserved here so a future
      cross-tenant flow can route onto an existing tenant without an
      API change.
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
    tier: Literal["individual", "team", "company"] = Field(
        default="individual",
        description=(
            "Tier slug. Step 30a.1 accepts all three; the (tier, cadence) "
            "pair must have a configured Stripe Price ID or the route "
            "returns 501."
        ),
    )
    billing_cadence: Literal["monthly", "annual"] = Field(
        default="monthly",
        description=(
            "Billing cadence. Step 30a.1 adds 'annual' (\u224817% prepay "
            "incentive). Default 'monthly' preserves Step 30a behaviour."
        ),
    )
    tenant_id: str | None = Field(
        default=None,
        description="Reserved for Step 38 upgrade flows; ignored at v1.",
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

    Step 30a.1 extended the response with ``billing_cadence`` and
    ``instance_count_cap`` so the dashboard can render a cadence badge
    and gate the Create-Luciel form on remaining cap.
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
    # Step 30a.1 additions.
    billing_cadence: str
    instance_count_cap: int
    # Step 30a.2-pilot additions: surface the pilot signal so the
    # Account UI can decide whether to render the self-serve refund
    # button without speculatively POSTing /pilot-refund. The pilot
    # status is derived from ``provider_snapshot.metadata.luciel_intro_applied``
    # ("true" iff the subscription was minted under the $100 CAD intro
    # offer) and ``trial_end`` (the day-91 conversion point, which is
    # also the refund-window cliff). Refer to the eligibility logic in
    # ``BillingService.process_pilot_refund`` -- the API must match.
    is_pilot: bool = Field(
        default=False,
        description="True iff this subscription was created under the "
                    "$100 CAD 90-day pilot offer (Step 30a.2-pilot). "
                    "Distinct from `status=='trialing'` because pilots "
                    "and normal trials share the trialing status.",
    )
    pilot_window_end: datetime | None = Field(
        default=None,
        description="UTC instant the 90-day pilot refund window closes. "
                    "Always equal to ``trial_end`` when ``is_pilot=True``; "
                    "None otherwise. Surfaced as its own field so the UI "
                    "can ignore ``trial_end`` when not in a pilot.",
    )


# ---------------------------------------------------------------------
# POST /api/v1/billing/pilot-refund   (Step 30a.2-pilot)
# ---------------------------------------------------------------------

class PilotRefundResponse(BaseModel):
    """Result of a self-serve pilot-refund.

    All fields are confirmation values the marketing site can render
    immediately ("$100 refunded to your card, pilot canceled"). The
    underlying Stripe Refund row + audit log entry are the durable
    source of truth; this response is for the UX, not the audit.

    Fields:
      refund_id            -- Stripe Refund id (re_...). Surfaced so a
                              support ticket can quote it back to the
                              buyer without a Stripe dashboard lookup.
      charge_id            -- Stripe Charge id that was refunded (ch_/py_).
      refunded_amount_cents-- Always 10000 by the locked policy; carried
                              explicitly so the marketing site does not
                              hardcode the cents value.
      currency             -- Lowercased ISO-4217. Always 'cad' today;
                              field exists so a future currency expansion
                              doesn't change the response shape.
      tenant_id            -- The tenant that just cascaded to inactive.
                              Surfaced so the marketing site can purge
                              the cookied session state correctly.
      deactivated_at       -- Server-side timestamp the cascade ran.
    """
    refund_id: str | None = Field(
        default=None,
        description="Stripe Refund id (re_...). Nullable in the rare "
                    "case where Stripe returns a Refund with no id.",
    )
    charge_id: str = Field(..., description="Stripe Charge id that was refunded.")
    refunded_amount_cents: int = Field(
        ..., description="Amount refunded in the smallest currency unit (cents for CAD).",
    )
    currency: str = Field(..., description="ISO-4217 currency, lowercased (e.g. 'cad').")
    tenant_id: str = Field(..., description="Tenant that was cascade-deactivated.")
    deactivated_at: datetime = Field(..., description="UTC time the cascade ran.")
