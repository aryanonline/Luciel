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

    Step 30a.1 lifted the v1 Individual-only carve-out: ``tier`` accepts
    multiple values and the new ``billing_cadence`` field accepts
    ``Literal['monthly','annual']``. The (tier, cadence) pair routes to
    a Stripe Price ID via ``BillingService.resolve_price_id`` -- see the
    table in that module.

    Arc 6 / Commit 8.5a (2026-05-23) -- V2 vocab fix-in-place. The
    ``tier`` Literal previously declared ``individual/team/company``
    even though Commit 5 retired that vocab from the service layer.
    Schema-validation occurs BEFORE the service call, so a marketing
    site POST with ``tier='pro'`` 422'd at the FastAPI boundary; the
    Pro Checkout funnel was silently broken from Commit 5 onward and
    only surfaced when Commit 8.5a wired the upgrade endpoint onto the
    same schema. Flipped to V2 vocab as a Path-A side-fix; tracked as
    drift ``D-arc6-checkout-schema-tier-v1-vocab-2026-05-23``.

    Why admin_id is OPTIONAL here:
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
    tier: Literal["pro", "enterprise"] = Field(
        default="pro",
        description=(
            "V2 tier slug. Arc 6 vocab: 'pro' or 'enterprise'. Free has "
            "its own no-card route (POST /signup-free) and never reaches "
            "this endpoint. The (tier, cadence) pair must have a "
            "configured Stripe Price ID or the route returns 501."
        ),
    )
    billing_cadence: Literal["monthly", "annual"] = Field(
        default="monthly",
        description=(
            "Billing cadence. Step 30a.1 adds 'annual' (\u224817% prepay "
            "incentive). Default 'monthly' preserves Step 30a behaviour."
        ),
    )
    admin_id: str | None = Field(
        default=None,
        description="Reserved for Step 38 upgrade flows; ignored at v1.",
    )


class CheckoutSessionResponse(BaseModel):
    """Stripe redirect URL the marketing site will navigate to."""
    checkout_url: str = Field(..., description="Stripe-hosted Checkout URL.")
    session_id: str = Field(..., description="Stripe Checkout session id (cs_...).")


# ---------------------------------------------------------------------
# POST /api/v1/billing/upgrade  (Arc 6 / Commit 8.5a)
# ---------------------------------------------------------------------


class UpgradeRequest(BaseModel):
    """Tier-upgrade request from a cookied (signed-in) admin owner.

    Used by the Account/Billing page to upgrade an existing Free or Pro
    Admin to a higher tier. Distinct from ``CheckoutSessionRequest``:

      * ``CheckoutSessionRequest`` is for *prospective* (no-cookie)
        buyers landing from the marketing site /signup form.
      * ``UpgradeRequest`` is for *existing* admins (cookie-auth) whose
        Admin row already exists and just needs a tier-flip + Stripe
        Subscription row attached.

    The cookied user's existing Admin id is derived from the session
    cookie's admin_id claim server-side; the buyer cannot pass it.
    This eliminates a class of cross-admin-upgrade attacks where a
    Free user could try to upgrade someone else's admin by posting a
    foreign admin_id.

    Webhook routing: the upgrade Checkout session stamps
    ``luciel_admin_id`` into Stripe metadata. The webhook
    (``billing_webhook_service._on_checkout_completed``) detects this
    metadata key and routes into the upgrade-branch (tier-flip on the
    existing Admin) instead of the default mint-new-Admin path.
    """
    target_tier: Literal["pro", "enterprise"] = Field(
        ...,
        description=(
            "Tier to upgrade to. Must be strictly higher than the "
            "caller's current tier (free<pro<enterprise); otherwise "
            "the route returns 400 with detail='not_an_upgrade'."
        ),
    )
    billing_cadence: Literal["monthly", "annual"] = Field(
        default="monthly",
        description=(
            "Cadence for the upgrade subscription. Enterprise is "
            "annual-only; the route 400s on (enterprise, monthly)."
        ),
    )


class UpgradeResponse(BaseModel):
    """Same shape as CheckoutSessionResponse -- a Stripe-hosted URL
    the marketing site navigates to. The webhook does the tier-flip
    when Stripe confirms payment.
    """
    checkout_url: str = Field(..., description="Stripe-hosted Checkout URL.")
    session_id: str = Field(..., description="Stripe Checkout session id (cs_...).")


# ---------------------------------------------------------------------
# POST /api/v1/billing/downgrade  (Arc 6 / Commit 8.5b)
# ---------------------------------------------------------------------


class DowngradeRequest(BaseModel):
    """Tier-downgrade request from a cookied (signed-in) admin owner.

    Twin of ``UpgradeRequest`` for the deferred-downgrade flow locked in
    Commit 8.5b. Unlike upgrade, downgrade does NOT round-trip through
    Stripe Checkout -- the buyer keeps their current tier until the
    current_period_end boundary, at which point Stripe fires
    ``customer.subscription.deleted`` and the webhook's V2 branch flips
    the Admin row, archives overflow (LRU), and -- for Ent->Pro --
    emails a transactional ``/signup?tier=pro`` magic link.

    The cookied user's existing Admin id is derived from the session
    cookie's admin_id claim server-side; the buyer cannot pass it.
    This mirrors the cross-admin-attack mitigation in UpgradeRequest.

    Three-layer Enterprise rejection (CANONICAL_RECAP \u00a717 lock):
      * Route layer: target_tier Literal excludes ``"enterprise"``
      * Service layer: ``BillingService.schedule_downgrade`` raises
        ValueError if target_tier not in {free, pro}
      * Schema layer: CHECK on subscriptions.pending_downgrade_target
        rejects ``'enterprise'`` at the DB. Mis-routing into the
        downgrade path with an Enterprise target is treated as a
        logic bug at all three layers.
    """
    target_tier: Literal["free", "pro"] = Field(
        ...,
        description=(
            "Tier to downgrade to. Must be strictly lower than the "
            "caller's current tier (free<pro<enterprise); otherwise "
            "the route returns 400 with detail='not_a_downgrade'. "
            "Enterprise is never a downgrade target (it is the top)."
        ),
    )


class DowngradeResponse(BaseModel):
    """Result of arming a deferred downgrade.

    Mirrors the dict returned by
    ``BillingService.schedule_downgrade()``. The frontend uses
    ``effective_at`` to surface the boundary date in the soft-warn
    confirm modal ("You will keep Pro features until {effective_at};
    after that we'll archive overflow per the audit retention window").

    ``effective_at`` is an ISO-8601 UTC timestamp string and may be
    None in the edge case where the Stripe subscription has no
    ``current_period_end`` populated (e.g. trialing without a period
    end set). The frontend must handle that gracefully -- show
    "end of current billing period" as a fall-back label.
    """
    admin_id: str = Field(..., description="Admin (tenant) id being downgraded.")
    old_tier: Literal["free", "pro", "enterprise"] = Field(
        ..., description="Current tier at the moment the downgrade was armed.",
    )
    target_tier: Literal["free", "pro"] = Field(
        ..., description="Tier the admin will land on at the boundary.",
    )
    effective_at: str | None = Field(
        None,
        description=(
            "ISO-8601 UTC timestamp of the current_period_end when the "
            "webhook will apply the downgrade. None when Stripe has no "
            "period_end on file (rare; frontend renders 'end of period')."
        ),
    )
    stripe_subscription_id: str = Field(
        ..., description="Stripe subscription whose cancel_at_period_end was set.",
    )


class DowngradePreviewRequest(BaseModel):
    """Preview the per-axis overflow that would occur if a downgrade
    were applied right now.

    Used by the soft-warn confirm modal to show the buyer exactly
    what would be archived at the boundary. Does NOT arm anything
    -- this is a pure read against the Admin's current entitlement
    surface vs the target tier's caps.
    """
    target_tier: Literal["free", "pro"] = Field(
        ...,
        description=(
            "Tier to preview overflow against. Same constraints as "
            "DowngradeRequest.target_tier."
        ),
    )


class AxisOverflowResponse(BaseModel):
    """Per-axis overflow shape returned by the downgrade preview.

    Mirrors the ``AxisOverflow`` dataclass on
    ``DowngradeArchiveService``. Field semantics:
      * cap       -- the target tier's cap on this axis
      * current   -- the admin's currently-active count
      * overflow  -- max(0, current - cap)
      * archived_ids -- the LRU-selected resource ids that *would* be
                        archived at the boundary (empty on preview;
                        populated only on the apply branch in the webhook
                        when ``DowngradeArchiveService.archive_overflow_for_admin``
                        commits the writes).
    """
    axis: Literal["instances", "embed_keys", "cnames", "seats"] = Field(
        ..., description="Which entitlement axis this overflow row is for.",
    )
    cap: int | None = Field(
        ...,
        description=(
            "Target tier's cap on this axis. ``None`` means unlimited "
            "(only possible when destination is Enterprise, which the "
            "downgrade route layer already rejects -- defensive shape)."
        ),
    )
    current: int = Field(..., description="Admin's currently-active count.")
    overflow: int = Field(
        ..., description="max(0, current - cap) -- 0 means no archive will run.",
    )
    archived_ids: list[str] = Field(
        default_factory=list,
        description=(
            "LRU-selected resource ids that would be archived, stringified "
            "for JSON portability (ints for instance/embed/seat tables are "
            "coerced to str; CNAMEs are already str UUIDs). Empty on "
            "preview-with-no-overflow."
        ),
    )


class DowngradePreviewResponse(BaseModel):
    """Bundle of per-axis overflow rows for the soft-warn modal.

    Frontend renders this as a four-row table (instances, embed keys,
    CNAMEs, seats) with cap/current/overflow columns and a warning
    badge when ``overflow > 0`` on any row. ``any_overflow`` is a
    convenience flag so the modal can short-circuit rendering.
    """
    admin_id: str = Field(..., description="Admin (tenant) id being previewed.")
    current_tier: Literal["free", "pro", "enterprise"] = Field(
        ..., description="Admin's current tier (unchanged by preview).",
    )
    target_tier: Literal["free", "pro"] = Field(
        ..., description="Tier the preview was computed against.",
    )
    any_overflow: bool = Field(
        ...,
        description=(
            "True if any axis has overflow>0 -- the modal should render "
            "the soft-warn banner. False means the downgrade is clean."
        ),
    )
    axes: list[AxisOverflowResponse] = Field(
        default_factory=list,
        description="Per-axis overflow rows. Always four entries in axis-name order.",
    )


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

    Arc 6 / Commit 8.5a (2026-05-23) -- /me now answers for ALL signed-in
    users, not only those with a Subscription row. Free admins have no
    Subscription row by V2 design (Gap 1 lock); the response carries the
    tier off the Admin row instead and leaves Subscription-derived fields
    null. The new ``has_subscription`` boolean lets the Account/Billing
    UI choose between "current tier + upgrade CTAs" (Free admins) and
    "current sub + manage-billing" (Pro/Enterprise admins) without
    second-guessing the field set. Status code is now 200 for any cookied
    user with a valid session, regardless of subscription state.
    """
    model_config = ConfigDict(from_attributes=True)

    # Arc 6 / Commit 8.5a -- has_subscription is the explicit branch
    # signal. True for Pro / Enterprise (has Subscription row), False
    # for Free (no Stripe row by design). The Account UI keys its
    # tier-transition CTAs off this flag rather than off the legacy
    # 404-vs-200 status-code distinction.
    has_subscription: bool = Field(
        default=False,
        description="True iff a Stripe Subscription row exists for the "
                    "cookied user. False for Free admins (Free has no "
                    "Subscription by V2 design).",
    )
    admin_id: str
    tier: str
    # Subscription-derived fields are nullable for Free admins (no
    # Subscription row). When has_subscription=True every field below
    # is populated; when False, only ``status`` carries a sentinel
    # value of ``"free"`` to keep clients happy with non-empty strings.
    status: str
    active: bool
    is_entitled: bool
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    trial_end: datetime | None = None
    cancel_at_period_end: bool = False
    canceled_at: datetime | None = None
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
    # Step 30a.5 addition: surface the cookied user's role within their
    # active scope_assignment so the dashboard can gate the CompanyTab
    # (visible iff tier=='company' AND role in ('tenant_admin','owner'))
    # and the TeamTab (visible iff tier in ('team','company') AND role
    # in ('tenant_admin','owner','department_lead')). Gating on tier
    # alone would leak Company-tier Domain visibility to invited
    # department leads -- see design doc §11 Q5 (resolved 2026-05-18:
    # tier AND role). The role is read off the cookied user's active
    # ScopeAssignment; users with no active assignment surface as None.
    active_role: str | None = Field(
        default=None,
        description="Role on the cookied user's active ScopeAssignment. "
                    "Common values: 'owner', 'tenant_admin', "
                    "'department_lead', 'teammate'. Used by the "
                    "frontend to gate org-building UI surfaces.",
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
      admin_id            -- The tenant that just cascaded to inactive.
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
    admin_id: str = Field(..., description="Tenant that was cascade-deactivated.")
    deactivated_at: datetime = Field(..., description="UTC time the cascade ran.")


# ---------------------------------------------------------------------
# POST /api/v1/billing/signup-free  (Arc 8 Work-Unit 5)
# ---------------------------------------------------------------------

class SignupFreeRequest(BaseModel):
    """Inbound request for the Free-tier self-serve signup endpoint.

    Arc 4 Deliverable #4 introduced the Free / Pro / Enterprise tier
    shape. The Free tier is self-serve and **unauthenticated** at the
    moment of signup (no credential exists yet -- the act of signing
    up is what mints the first Admin row). That makes the endpoint a
    free SES-quota drain and a free database-row drain unless we put
    a bot gate in front of it; D-free-tier-captcha-missing-2026-05-22
    is the drift, and hCaptcha is the gate (see
    ``app/services/hcaptcha_service.py``).

    Field set is deliberately minimal:

      email         -- becomes the new Admin.email and the SES
                       destination for the magic-link verification
                       email. EmailStr enforces shape; the
                       ``_validate_email_shape`` defence-in-depth at
                       the tier-provisioning service (Arc 3 Work-Unit
                       C) is the second layer.
      display_name  -- carried onto the new Admin row and surfaced
                       in the welcome SES email. min=1, max=200
                       matches the v1 CheckoutSessionRequest field.
      captcha_token -- the ``h-captcha-response`` value emitted by
                       the front-end hCaptcha widget. Verified
                       server-side before any DB write.

    No ``tier`` field because this endpoint is Free-tier-only by
    contract. Pro / Enterprise signups go through the paid
    ``/billing/checkout`` flow which has its own (Stripe-based) bot
    gate via the card-issuing step.
    """
    email: EmailStr = Field(
        ..., description="Email of the new Free-tier admin. Becomes "
                        "Admin.email after captcha + Arc-5 mint.",
    )
    display_name: str = Field(
        ..., min_length=1, max_length=200,
        description="Display name carried onto the Admin row and the "
                    "welcome SES email.",
    )
    # Arc 6 Commit 9 (2026-05-23) -- hard-required. The Commit-8
    # window where this field was Optional with a backend soft-pass
    # is closed by this commit; Commit 9 ships the hCaptcha widget
    # on the marketing site and flips this schema slot to required.
    # Closes D-free-tier-captcha-missing-2026-05-22 (the P1 gate on
    # the Free-tier launch). The IP-bucket accounting that the
    # Commit-8 prose mentioned is deliberately deferred to a follow-up
    # commit and tracked at D-free-tier-ip-bucket-deferred-2026-05-23
    # -- the captcha plus the existing one-Admin-per-email collision
    # at OnboardingService is the bot gate; the per-IP bucket is an
    # anti-distributed-bot defense and is post-launch hardening.
    captcha_token: str = Field(
        ..., min_length=1,
        description="hCaptcha h-captcha-response token from the front-end "
                    "widget. Required (Arc 6 Commit 9). Verified "
                    "server-side against api.hcaptcha.com/siteverify "
                    "before any DB write.",
    )


class SignupFreeResponse(BaseModel):
    """Outbound response for the Free-tier signup endpoint.

    Arc 6 Commit 8 (2026-05-23): full mint logic now lands. Two shapes
    still coexist behind one schema for backward-compat:

      Post-Arc-6-Commit-8 (today):
        status="ok", admin_id="free-XXXXXXXX", email="...",
        message="Check your email for a link to set your password."

      Pre-Arc-6-Commit-8 (legacy fallback, retained for grep):
        status="pending-arc-5", admin_id=None, message=...

    The marketing site renders both states the same way -- a
    "Thanks, check your email" panel -- so this is a backend-only
    enrichment with no client-side schema break.
    """
    status: Literal["ok", "pending-arc-5"] = Field(
        ..., description="'ok' once Arc 6 Commit 8 lands; 'pending-arc-5' "
                        "is the legacy pre-mint shape.",
    )
    admin_id: str | None = Field(
        default=None,
        description="Admin slug of the newly-minted row (V2 shape: "
                    "'free-<8hex>'). None for the legacy shape.",
    )
    email: str | None = Field(
        default=None,
        description="Echo of the email the verification was sent to. "
                    "None for the legacy shape.",
    )
    message: str = Field(
        ..., description="Human-readable status message for the marketing site.",
    )
