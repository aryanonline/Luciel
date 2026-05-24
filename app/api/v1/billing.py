"""Step 30a: billing API routes.

Public surface for the self-serve subscription flow. Nine routes
(seventh route /pilot-refund added in Step 30a.2-pilot; ninth route
/signup-free added in Arc 8 Work-Unit 5):

  POST /api/v1/billing/checkout           -- create a Stripe Checkout session
  POST /api/v1/billing/webhook            -- Stripe webhook receiver
  POST /api/v1/billing/onboarding/claim   -- post-checkout email-link mint
  GET  /api/v1/billing/login              -- exchange magic-link token for cookie
  POST /api/v1/billing/portal             -- Stripe Customer Portal session
  POST /api/v1/billing/pilot-refund       -- Step 30a.2-pilot: self-serve
                                             $100 refund + cancel in 90-day window
  GET  /api/v1/billing/me                 -- read current subscription state
  POST /api/v1/billing/logout             -- clear the session cookie
  POST /api/v1/billing/signup-free        -- Arc 8 WU-5: Free-tier self-serve
                                             signup (hCaptcha-gated, mint logic
                                             completes at Arc 5)

Auth model:

  * /checkout, /webhook, /onboarding/claim, /login -- no api key, no cookie
    required. /webhook verifies a Stripe signature; /login validates a JWT;
    /checkout + /onboarding/claim are public-by-design (the marketing site
    calls them anonymously).
  * /portal, /pilot-refund, /me -- require the session cookie minted by /login.
  * /logout -- idempotent and credential-free; safe to call when already
    logged out (clears the cookie if present).

All routes return 501 when Stripe is not configured (empty
``stripe_secret_key`` etc.) for the routes that talk to Stripe. That keeps
CI / dev environments boot-safe without needing the billing surface live.

Full architecture: see docs/ARCHITECTURE.md §3.2.13 (Billing surface).
Roadmap row: docs/CANONICAL_RECAP.md §12 Step 30a (closing tag
`step-30a-subscription-billing-complete`).
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from app.api.deps import DbSession
from app.core.config import settings
from app.integrations.stripe import StripeSignatureError, get_stripe_client
from app.models.user import User
from app.schemas.billing import (
    CheckoutSessionRequest,
    CheckoutSessionResponse,
    OnboardingClaimRequest,
    OnboardingClaimResponse,
    PilotRefundResponse,
    PortalSessionResponse,
    SignupFreeRequest,
    SignupFreeResponse,
    SubscriptionStatusResponse,
    UpgradeRequest,
    UpgradeResponse,
    DowngradeRequest,
    DowngradeResponse,
    DowngradePreviewRequest,
    DowngradePreviewResponse,
    AxisOverflowResponse,
)
from app.services.billing_service import (
    BillingNotConfiguredError,
    BillingService,
    NotFirstTimePilotError,
    PilotChargeNotFoundError,
    PilotWindowExpiredError,
)
from app.services.billing_webhook_service import BillingWebhookService
from app.services.hcaptcha_service import (
    CaptchaInvalidError,
    CaptchaNotConfiguredError,
    verify_captcha,
)
from app.services.magic_link_service import (
    MagicLinkError,
    build_magic_link_url,
    mint_magic_link_token,
    mint_session_token,
    validate_session_token,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing", tags=["billing"])


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _service(db: DbSession) -> BillingService:
    """Build a BillingService for the request. Inline so each route is
    obviously self-contained -- no FastAPI dep factory needed because
    the StripeClient is a process-singleton."""
    return BillingService(db, get_stripe_client())


def _501_if_billing_not_ready(exc: BillingNotConfiguredError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=str(exc) or "Billing is not configured on this backend.",
    )


def _resolve_cookied_user(*, db, session_cookie: str | None) -> User:
    """Validate the cookie and return the User row. Raises 401 on any failure."""
    if not session_cookie:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    try:
        payload = validate_session_token(session_cookie)
    except MagicLinkError as exc:
        raise HTTPException(status_code=401, detail=str(exc) or "Invalid session.") from exc

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Malformed session.")

    user = db.get(User, user_id)
    if user is None or not user.active:
        raise HTTPException(status_code=401, detail="User not found or inactive.")
    return user


def _set_session_cookie(response: Response, token: str) -> None:
    """Stamp the session cookie with the configured TTL / security flags."""
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_cookie_ttl_days * 24 * 3600,
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
        # An empty domain string falls back to a host-only cookie,
        # which is what we want in dev. In prod the domain is set
        # explicitly so the apex domain (vantagemind.ai) sees it.
        domain=settings.session_cookie_domain or None,
        path="/",
    )


# ---------------------------------------------------------------------
# POST /checkout
# ---------------------------------------------------------------------

@router.post(
    "/checkout",
    response_model=CheckoutSessionResponse,
    status_code=status.HTTP_200_OK,
)
def create_checkout(payload: CheckoutSessionRequest, db: DbSession) -> CheckoutSessionResponse:
    """Begin a Stripe Checkout session.

    Anonymous endpoint. Rate limiting happens at the edge (CloudFront /
    ALB); we do not need a per-route limiter inside the backend because
    each call is a Stripe API call and Stripe imposes its own limits.
    """
    svc = _service(db)
    try:
        result = svc.create_checkout(
            email=str(payload.email),
            display_name=payload.display_name,
            tier=payload.tier,
            billing_cadence=payload.billing_cadence,
        )
    except BillingNotConfiguredError as exc:
        raise _501_if_billing_not_ready(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return CheckoutSessionResponse(**result)


# ---------------------------------------------------------------------
# POST /webhook
# ---------------------------------------------------------------------

@router.post("/webhook", status_code=status.HTTP_200_OK)
async def stripe_webhook(request: Request, db: DbSession) -> JSONResponse:
    """Stripe webhook receiver.

    Always returns 2xx unless the signature is invalid (then 400). A
    handler-level exception is allowed to bubble to a 500 only if it
    represents a genuine server problem we want Stripe to retry against
    (e.g. a transient DB failure during the onboard transaction).
    Application-level "I don't know this event type" outcomes are
    recorded in audit and answered with 200.
    """
    stripe_client = get_stripe_client()
    if not stripe_client.webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Stripe webhook secret is not configured.",
        )

    sig_header = request.headers.get("stripe-signature", "")
    payload = await request.body()

    try:
        event = stripe_client.construct_event(payload=payload, sig_header=sig_header)
    except StripeSignatureError as exc:
        logger.warning("billing-webhook: signature verification failed: %s", exc)
        # Record audit row for the rejection -- the webhook service
        # has the helper for it but expects to be inside its dispatch
        # path, so we record directly here via the same context.
        try:
            # Arc 2 (2026-05-20) -- D-billing-webhook-service-stripe-attribute-error-2026-05-18:
            # Pass the singleton stripe_client through so the service's
            # __init__ does not re-resolve it on the bad-signature audit path.
            BillingWebhookService(db, stripe_client=stripe_client)._record_unknown_event(
                event_id="<bad-signature>",
                event_type="signature_verification_failed",
            )
        except Exception:  # pragma: no cover - belt-and-suspenders
            logger.exception("billing-webhook: failed to record bad-sig audit row")
        raise HTTPException(status_code=400, detail="Invalid signature.") from exc

    # Step 30a.2-pilot Commit 3e: convert the Stripe Event StripeObject
    # into a plain nested ``dict`` BEFORE handing it to
    # ``BillingWebhookService.handle()``.
    #
    # D-stripe-event-dict-conversion-python314-2026-05-15:
    #   On 2026-05-15 the live GATE 4 Path B smoke produced two webhook
    #   500s with the traceback:
    #
    #     File "/app/app/api/v1/billing.py", line 201, in stripe_webhook
    #       result = BillingWebhookService(db).handle(dict(event))
    #     File ".../stripe/_stripe_object.py", line 203, in __getitem__
    #     File ".../stripe/_stripe_object.py", line 224, in __getitem__
    #     KeyError: 0
    #
    #   The Stripe SDK's ``StripeObject.__getitem__`` raises ``KeyError``
    #   on missing keys. On Python 3.14 (the version pinned by the
    #   production image, ``python:3.14-slim``), one of the
    #   dict-construction code paths probes positional index ``0``
    #   during iteration, which ``StripeObject`` correctly rejects --
    #   so ``dict(event)`` raises. The defect did not surface on
    #   Python 3.13 because the older iteration protocol only called
    #   ``__getitem__`` for the string keys yielded by ``__iter__``.
    #
    #   The materialisation method name has shifted between SDK majors:
    #     stripe 10.x-12.x: ``event.to_dict_recursive()`` (public)
    #     stripe 13.x+   : ``event._to_dict_recursive()`` (underscored)
    #   The currently installed prod SDK is **15.1.0** (verified by
    #   ``docker run --rm <image> python -c "import stripe; ..."`` on
    #   2026-05-15) which only exposes the underscored form.
    #
    #   Rather than couple to either private name, we round-trip through
    #   ``json.loads(str(event))``. ``StripeObject.__str__`` has emitted
    #   valid JSON via the SDK's own recursive serializer since v1.x,
    #   so this is the most version-resilient public path. The result
    #   is a plain nested ``dict`` satisfying every access
    #   ``BillingWebhookService.handle()`` makes:
    #     - ``event.get("id")``
    #     - ``event.get("type")``
    #     - ``(event.get("data") or {}).get("object")``
    #     - ``data_object.get("metadata")`` and its nested ``.get`` calls
    #
    #   Belt-and-suspenders: ``pyproject.toml`` is also being pinned to
    #   ``stripe>=10.0.0,<16`` in the same commit so a future SDK
    #   major rev cannot silently break this again.
    try:
        event_dict = json.loads(str(event))
    except (TypeError, ValueError):
        # ``str(event)`` should always be JSON for a real Stripe Event,
        # but if a future SDK ever breaks that contract we fall back to
        # the documented (currently underscored in 15.x) recursive
        # serializer. We deliberately probe both the public and the
        # underscored name so the fallback works on every published
        # SDK major from 1.x to 15.x.
        logger.warning(
            "billing-webhook: json.loads(str(event)) failed; "
            "falling back to _to_dict_recursive/to_dict_recursive",
        )
        recursive = getattr(
            event,
            "to_dict_recursive",
            getattr(event, "_to_dict_recursive", None),
        )
        if recursive is None:  # pragma: no cover -- last-ditch defence
            logger.exception(
                "billing-webhook: cannot materialise Stripe Event into "
                "a plain dict; SDK surface unrecognised",
            )
            raise HTTPException(
                status_code=500,
                detail="Stripe event materialisation failed.",
            )
        event_dict = recursive()

    try:
        # Arc 2 (2026-05-20) -- D-billing-webhook-service-stripe-attribute-error-2026-05-18:
        # Inject the singleton stripe_client so `_on_checkout_completed`
        # can read the canonical Subscription object via
        # `self.stripe.retrieve_subscription` instead of relying on the
        # bare-AttributeError fallback path the prior implementation
        # accidentally promoted to primary.
        result = BillingWebhookService(db, stripe_client=stripe_client).handle(event_dict)
    except Exception:
        # Genuine server-side failure -- let it bubble so Stripe retries.
        logger.exception("billing-webhook: handler raised")
        raise

    return JSONResponse(content=result, status_code=200)


# ---------------------------------------------------------------------
# POST /onboarding/claim
# ---------------------------------------------------------------------

@router.post(
    "/onboarding/claim",
    response_model=OnboardingClaimResponse,
    status_code=status.HTTP_200_OK,
)
def onboarding_claim(payload: OnboardingClaimRequest, db: DbSession) -> OnboardingClaimResponse:
    """Inspect a checkout session id and either:

      * if the webhook already minted the subscription AND the user
        has not yet set a password, mint a fresh **welcome-set-password**
        link and email it (idempotent: each claim re-sends the link,
        so a buyer who closed the email tab can re-trigger from the
        marketing site without sales involvement).
      * if the webhook has not arrived yet, return 'pending' so the
        marketing site can show "we'll email you shortly" and the
        webhook drives the eventual email send.
      * if Stripe does not recognize the session_id, return 'unknown'.

    Step 30a.3 (Option B welcome-email mechanic): this route was the
    post-Checkout magic-link resender pre-30a.3; it is now the
    post-Checkout welcome-set-password resender. The cookie-issuance-
    without-password-set anti-pattern of the magic-link resend path is
    removed -- the resent link lands on /auth/set-password just like
    the original welcome email, and the buyer must type a password
    before a session cookie mints. If the user already has a password
    hash on file (e.g. the buyer redeemed the welcome from a different
    device and is now revisiting /onboarding from the original tab),
    the resend mints a reset_password-class token instead so the
    surface degrades gracefully into a password-reset rather than
    silently re-overwriting their hash.
    """
    stripe_client = get_stripe_client()
    if not stripe_client.is_configured:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Stripe is not configured on this backend.",
        )

    try:
        session = stripe_client.retrieve_checkout_session(payload.session_id)
    except Exception:
        logger.warning("billing: onboarding_claim could not retrieve session %s", payload.session_id)
        return OnboardingClaimResponse(state="unknown", email_sent_to=None)

    stripe_subscription_id = getattr(session, "subscription", None)
    # ``session.subscription`` may be the expanded object or the bare
    # id depending on Stripe's response shape.
    if hasattr(stripe_subscription_id, "id"):
        stripe_subscription_id = stripe_subscription_id.id

    svc = _service(db)
    sub = None
    if stripe_subscription_id:
        sub = svc.get_subscription_by_stripe_id(stripe_subscription_id=stripe_subscription_id)

    if sub is None:
        # Webhook hasn't applied yet. Echo back the email Stripe knows
        # about so the marketing site can show "check <email>".
        customer_email = (
            (getattr(session, "customer_details", None) or {}).get("email")
            if isinstance(getattr(session, "customer_details", None), dict)
            else None
        )
        if customer_email is None and getattr(session, "customer_details", None) is not None:
            customer_email = getattr(session.customer_details, "email", None)
        return OnboardingClaimResponse(
            state="pending",
            email_sent_to=customer_email or None,
        )

    # Subscription exists -- resend the welcome-set-password email.
    # Step 30a.3 (Option B): we mint a set_password-class token when
    # the user has not yet redeemed their welcome, OR a reset_password-
    # class token when they have. Both consume through the same
    # /auth/set-password route on the marketing site.
    user = db.get(User, sub.user_id)
    if user is None:  # pragma: no cover - referential integrity guarantees this
        return OnboardingClaimResponse(state="unknown", email_sent_to=None)

    from app.services.email_service import (
        WelcomeEmailError,
        send_welcome_set_password_email,
    )
    from app.services.magic_link_service import (
        build_set_password_url,
        mint_reset_password_token,
        mint_set_password_token,
    )

    try:
        if user.password_hash:
            # Password already set -- degrade to a reset link so we
            # do not silently overwrite their hash on a resend.
            token = mint_reset_password_token(
                user_id=user.id, email=user.email, tenant_id=sub.tenant_id,
            )
            purpose = "reset"
        else:
            token = mint_set_password_token(
                user_id=user.id,
                email=user.email,
                tenant_id=sub.tenant_id,
                purpose="signup",
            )
            purpose = "signup"
        url = build_set_password_url(token)
        send_welcome_set_password_email(
            to_email=user.email,
            set_password_url=url,
            display_name=user.display_name,
            purpose=purpose,
        )
    except (MagicLinkError, WelcomeEmailError) as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=str(exc) or "Email delivery failed.",
        ) from exc

    return OnboardingClaimResponse(state="ready", email_sent_to=user.email)


# ---------------------------------------------------------------------
# GET /login
# ---------------------------------------------------------------------

@router.get("/login", status_code=status.HTTP_200_OK)
def login_with_magic_link(token: str, db: DbSession) -> JSONResponse:
    """Exchange a magic-link JWT for a session cookie.

    Marketing-site /login route POSTs the URL-query token here, then
    redirects the buyer to /account/billing. The cookie is set on the
    JSON response so the browser carries it on the subsequent fetches.

    We deliberately return JSON (not a 302) so the marketing site has
    full control of the post-login redirect. Stripe-portal-style
    integrations often need to chain another navigation after login;
    a plain JSON response makes that trivial.
    """
    from app.services.magic_link_service import consume_magic_link_token

    try:
        payload = consume_magic_link_token(token)
    except MagicLinkError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    user_id = payload.get("sub")
    email = payload.get("email")
    tenant_id = payload.get("tenant_id")
    if not user_id or not email or not tenant_id:
        raise HTTPException(status_code=401, detail="Malformed token.")

    user = db.get(User, user_id)
    if user is None or not user.active:
        raise HTTPException(status_code=401, detail="User not found or inactive.")

    session_token = mint_session_token(user_id=user.id, email=email, tenant_id=tenant_id)
    body = {
        "ok": True,
        "redirect_to": "/account/billing",
        "email": email,
        "tenant_id": tenant_id,
    }
    response = JSONResponse(content=body, status_code=200)
    _set_session_cookie(response, session_token)
    return response


# ---------------------------------------------------------------------
# POST /portal
# ---------------------------------------------------------------------

@router.post(
    "/portal",
    response_model=PortalSessionResponse,
    status_code=status.HTTP_200_OK,
)
def create_portal(request: Request, db: DbSession) -> PortalSessionResponse:
    """Create a Stripe Customer Portal session for the cookied user.

    We read the session cookie off the Request directly (rather than via
    FastAPI's ``Cookie(...)`` dependency) because the cookie name is
    configurable via ``settings.session_cookie_name`` -- the dep would
    require a hard-coded parameter name.
    """
    cookie = request.cookies.get(settings.session_cookie_name)
    user = _resolve_cookied_user(db=db, session_cookie=cookie)

    svc = _service(db)
    try:
        url = svc.create_portal_session_for_user(user_id=user.id)
    except BillingNotConfiguredError as exc:
        raise _501_if_billing_not_ready(exc) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return PortalSessionResponse(portal_url=url)


# ---------------------------------------------------------------------
# POST /pilot-refund   (Step 30a.2-pilot)
# ---------------------------------------------------------------------

@router.post(
    "/pilot-refund",
    response_model=PilotRefundResponse,
    status_code=status.HTTP_200_OK,
)
def pilot_refund(request: Request, db: DbSession) -> PilotRefundResponse:
    """Self-serve $100 intro-fee refund + immediate pilot teardown.

    Locked policy (CANONICAL_RECAP §14 ¶273, Step 30a.2-pilot):
      * Eligible during the 90-day intro window only.
      * Only first-time customers ever paid the $100; repeat customers
        get 403.
      * Refund cancels the subscription and cascades the tenant in the
        same database transaction. There is no separate "refund without
        cancel" affordance -- by policy, the refund IS the cancel.
      * Past day 91 the intro fee is non-refundable; the buyer must
        cancel via the Customer Portal instead (recurring rate applies).

    HTTP mapping:
      200  -- refund + cancel + cascade succeeded; PilotRefundResponse body.
      401  -- no valid session cookie.
      403  -- not on the first-time intro path (NotFirstTimePilotError).
      404  -- no active subscription on file OR Stripe cannot locate the
              intro charge (PilotChargeNotFoundError / LookupError).
      409  -- 90-day window has closed (PilotWindowExpiredError).
      501  -- Stripe / intro fee Price not configured on this backend.
    """
    cookie = request.cookies.get(settings.session_cookie_name)
    user = _resolve_cookied_user(db=db, session_cookie=cookie)

    svc = _service(db)
    try:
        result = svc.process_pilot_refund(user=user)
    except BillingNotConfiguredError as exc:
        raise _501_if_billing_not_ready(exc) from exc
    except NotFirstTimePilotError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except PilotWindowExpiredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PilotChargeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return PilotRefundResponse(**result)


# ---------------------------------------------------------------------
# GET /me
# ---------------------------------------------------------------------

@router.get(
    "/me",
    response_model=SubscriptionStatusResponse,
    status_code=status.HTTP_200_OK,
)
def me(request: Request, db: DbSession) -> SubscriptionStatusResponse:
    """Return the cookied user's current tier + subscription state.

    Arc 6 / Commit 8.5a (2026-05-23) -- rewritten to Option A semantics:
    /me now answers 200 for ANY cookied user with a valid session,
    regardless of whether a Subscription row exists. The legacy 404
    branch ('no subscription on file') was load-bearing for the legacy
    'pay to get an account' shape; in V2 every signed-in user has an
    Admin row (Free admins have no Subscription by design -- Gap 1
    lock). The Account/Billing UI now keys its tier-transition CTAs
    off the new ``has_subscription`` boolean instead of the HTTP status.

    Resolution order:
      1. Cookie -> User (401 on bad cookie).
      2. ScopeAssignment lookup -> Admin id + role.
      3. Admin row -> tier (V2 source of truth for Free admins).
      4. Optional Subscription lookup -> Stripe-derived fields when
         the admin is on a paid tier; null when Free.

    Step 30a.5: also surfaces ``active_role`` -- the cookied user's
    role on their active ScopeAssignment (preferring the assignment
    matching the session JWT's tenant_id, falling back to the first
    active assignment for single-tenant common case). The dashboard
    gates the CompanyTab on (tier=='enterprise' AND role in
    ('tenant_admin','owner')) and the TeamTab on (tier in
    ('pro','enterprise') AND role in ('tenant_admin','owner',
    'department_lead')) -- see design doc §11 Q5.
    """
    cookie = request.cookies.get(settings.session_cookie_name)
    user = _resolve_cookied_user(db=db, session_cookie=cookie)

    # Re-decode the JWT for tenant_id only. The cookie has already
    # been validated above by _resolve_cookied_user (which raises 401
    # on failure), so this second decode is safe; we accept the small
    # duplication rather than reshape the helper's return type and
    # touch every caller. Mirrors the pattern in _resolve_invite_actor
    # in admin.py.
    session_tenant_id: str | None = None
    try:
        payload = validate_session_token(cookie or "")
        session_tenant_id = payload.get("tenant_id")
    except MagicLinkError:
        # Already validated upstream; if it somehow fails here we just
        # leave session_tenant_id None and pick the first active scope.
        session_tenant_id = None

    # Resolve the cookied user's active ScopeAssignment (preferring
    # the assignment matching the session JWT's tenant_id). This is
    # also where we derive ``admin_id`` for the Admin-row lookup
    # below: ScopeAssignment.tenant_id physically retains the column
    # name from Arc 5 Path A but semantically points at admins.id.
    from app.repositories.scope_assignment_repository import (
        ScopeAssignmentRepository,
    )
    sar = ScopeAssignmentRepository(db)
    active_assignments = sar.list_for_user(user.id, active_only=True)
    active_role: str | None = None
    chosen_admin_id: str | None = None
    if active_assignments:
        chosen = next(
            (a for a in active_assignments if a.tenant_id == session_tenant_id),
            active_assignments[0],
        )
        active_role = chosen.role
        chosen_admin_id = chosen.tenant_id

    # Read the Admin row to source the tier (V2 source of truth).
    # A cookied user with no active ScopeAssignment is a forensic
    # edge case (deleted assignment, deactivated admin); answer 200
    # with sentinel values rather than 401 so the dashboard can
    # render a sensible "no plan yet" view.
    from app.models.admin import Admin as AdminModel
    admin = (
        db.get(AdminModel, chosen_admin_id) if chosen_admin_id else None
    )
    admin_tier = admin.tier if admin is not None else "free"
    resolved_admin_id = admin.id if admin is not None else (chosen_admin_id or "")

    svc = _service(db)
    sub = svc.get_active_subscription_for_user(user_id=user.id)

    if sub is None:
        # Free admin (or transient no-sub state): build a tier-only
        # response off the Admin row. ``status='free'`` is the
        # sentinel; instance_count_cap reads from the tier-cap map
        # so a Free admin still sees "1 instance" in the UI without
        # a Subscription row.
        from app.models.subscription import TIER_INSTANCE_CAPS
        return SubscriptionStatusResponse(
            has_subscription=False,
            tenant_id=resolved_admin_id,
            tier=admin_tier,
            status="free",
            active=admin is not None and admin.active,
            is_entitled=admin is not None and admin.active,
            current_period_start=None,
            current_period_end=None,
            trial_end=None,
            cancel_at_period_end=False,
            canceled_at=None,
            customer_email=user.email,
            billing_cadence="none",
            instance_count_cap=TIER_INSTANCE_CAPS.get(admin_tier, 1) or 1,
            is_pilot=False,
            pilot_window_end=None,
            active_role=active_role,
        )

    # Step 30a.2-pilot: derive pilot signal from the same source the
    # refund-eligibility check uses (``provider_snapshot.metadata.
    # luciel_intro_applied``). The Account UI uses ``is_pilot`` to
    # decide whether to render the self-serve refund button; keeping
    # the derivation here (not in the service) so the read path stays
    # cheap and dependency-free. If we ever change the metadata key,
    # update both this site and ``BillingService.process_pilot_refund``
    # together.
    #
    # Commit 3f: ``is_pilot`` is now driven by the metadata flag ALONE.
    # Earlier code additionally required ``sub.trial_end is not None``,
    # but that conflated *"sold as a pilot"* (metadata) with *"Stripe
    # stamped a trial_end on the Subscription object"* (which depends on
    # the Subscription having been retrieved at checkout time -- see
    # drift D-stripe-webhook-checkout-vs-subscription-field-source-2026-05-15).
    # ``pilot_window_end`` still prefers ``trial_end`` when populated
    # (canonical), and falls back to ``created_at + 90 days`` when it
    # is null so older Commit-3e rows display the right deadline.
    snapshot = sub.provider_snapshot if isinstance(sub.provider_snapshot, dict) else {}
    snapshot_meta = snapshot.get("metadata") if isinstance(snapshot.get("metadata"), dict) else {}
    is_pilot = str(snapshot_meta.get("luciel_intro_applied", "")).lower() == "true"
    if is_pilot:
        if sub.trial_end is not None:
            pilot_window_end = sub.trial_end
        elif sub.created_at is not None:
            pilot_window_end = sub.created_at + timedelta(days=90)
        else:
            pilot_window_end = None
    else:
        pilot_window_end = None

    # When a Subscription exists, prefer its tier over the Admin
    # row's tier for the read response (they are kept in sync by
    # the webhook + upgrade_admin_tier path; if they ever drift
    # the Subscription is the more recent signal because the
    # webhook commits Subscription THEN flips Admin.tier).
    return SubscriptionStatusResponse(
        has_subscription=True,
        tenant_id=sub.tenant_id,
        tier=sub.tier,
        status=sub.status,
        active=sub.active,
        is_entitled=sub.is_entitled,
        current_period_start=sub.current_period_start,
        current_period_end=sub.current_period_end,
        trial_end=sub.trial_end,
        cancel_at_period_end=sub.cancel_at_period_end,
        canceled_at=sub.canceled_at,
        customer_email=sub.customer_email,
        # Step 30a.1 additions.
        billing_cadence=sub.billing_cadence,
        instance_count_cap=sub.instance_count_cap,
        # Step 30a.2-pilot additions.
        is_pilot=is_pilot,
        pilot_window_end=pilot_window_end,
        # Step 30a.5 addition.
        active_role=active_role,
    )


# ---------------------------------------------------------------------
# POST /upgrade  (Arc 6 / Commit 8.5a)
# ---------------------------------------------------------------------

@router.post(
    "/upgrade",
    response_model=UpgradeResponse,
    status_code=status.HTTP_200_OK,
)
def upgrade_tier(
    payload: UpgradeRequest,
    request: Request,
    db: DbSession,
) -> UpgradeResponse:
    """Create a Stripe Checkout session for an existing-admin tier upgrade.

    Cookie-authenticated. The admin id is derived from the session
    JWT's tenant_id claim (which physically points at admins.id in
    V2 after the Arc 5 Path A rename retained the column name); the
    buyer cannot specify it in the body. This closes a class of
    cross-admin-upgrade attacks where a Free user could POST another
    admin's id to upgrade somebody else's tenant on their own card.

    Validation (returns 400 detail=...):
      * ``not_an_upgrade``      -- target_tier <= current Admin.tier
      * ``no_admin_for_user``   -- cookied user has no active scope

    Note: the ``enterprise_monthly`` 400 reject was RETIRED at Arc 7
    Commit 1 (2026-05-24) when Enterprise gained a monthly-cadence
    Price symmetric with Pro. The (enterprise, monthly) pair is now a
    valid Checkout path; if its config slot is empty the route 501s
    via BillingNotConfiguredError (the standard not-configured shape)
    rather than 400-rejecting at the route layer.

    On success returns ``{checkout_url, session_id}`` -- the client
    redirects to the Stripe-hosted Checkout. The webhook does the
    tier-flip on ``checkout.session.completed`` when Stripe confirms
    payment (proration is handled by Stripe's standard immediate-
    proration semantics; we do not compute credits ourselves).
    """
    cookie = request.cookies.get(settings.session_cookie_name)
    user = _resolve_cookied_user(db=db, session_cookie=cookie)

    # Resolve admin_id off the session's tenant_id JWT claim, falling
    # back to the cookied user's active ScopeAssignment (single-scope
    # users). Mirrors the resolution order in /me above.
    session_tenant_id: str | None = None
    try:
        sess_payload = validate_session_token(cookie or "")
        session_tenant_id = sess_payload.get("tenant_id")
    except MagicLinkError:
        session_tenant_id = None

    from app.repositories.scope_assignment_repository import (
        ScopeAssignmentRepository,
    )
    sar = ScopeAssignmentRepository(db)
    active_assignments = sar.list_for_user(user.id, active_only=True)
    if not active_assignments:
        raise HTTPException(status_code=400, detail="no_admin_for_user")
    chosen = next(
        (a for a in active_assignments if a.tenant_id == session_tenant_id),
        active_assignments[0],
    )
    # Only owners may initiate an upgrade. department_leads and
    # teammates do not have billing authority on a Pro/Enterprise
    # account. (For Free, the signup-free path mints owner-role on
    # the first user, so every Free admin has exactly one owner.)
    if chosen.role != "owner":
        raise HTTPException(status_code=403, detail="upgrade_requires_owner")
    admin_id = chosen.tenant_id

    # Read current Admin.tier to enforce strict upgrade direction.
    from app.models.admin import Admin as AdminModel
    admin = db.get(AdminModel, admin_id)
    if admin is None or not admin.active:
        raise HTTPException(status_code=400, detail="admin_inactive")

    _tier_order = {"free": 0, "pro": 1, "enterprise": 2}
    current_rank = _tier_order.get(admin.tier, -1)
    target_rank = _tier_order.get(payload.target_tier, -1)
    if target_rank <= current_rank:
        raise HTTPException(status_code=400, detail="not_an_upgrade")

    # Arc 7 Commit 1 (2026-05-24) RETIRED the (enterprise, monthly)
    # 400 reject. Enterprise is now flat-recurring symmetric with Pro:
    # both monthly + annual cadences are first-class Checkout paths.
    # The hybrid/metered-overage shape that justified the annual-only
    # restriction was retired by partner doctrine pivot — see
    # CANONICAL §17 Arc 7 Commit 1 entry and the closure of
    # D-enterprise-metering-not-implemented-2026-05-22.

    svc = _service(db)
    try:
        result = svc.create_upgrade_checkout(
            admin_id=admin_id,
            email=user.email,
            display_name=user.display_name or user.email,
            target_tier=payload.target_tier,
            billing_cadence=payload.billing_cadence,
        )
    except BillingNotConfiguredError as exc:
        raise _501_if_billing_not_ready(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return UpgradeResponse(**result)


# ---------------------------------------------------------------------
# POST /downgrade  (Arc 6 / Commit 8.5b)
# POST /downgrade/preview
# ---------------------------------------------------------------------

# Tier ordinals used by both upgrade and downgrade routes. Pulled out of
# the inner function so a single source-of-truth governs both direction
# checks. NB: this dict intentionally mirrors the constant in
# tier_provisioning_service so a misalignment between routes and service
# would surface as a diff in two places. Keep them in sync; downgrade
# direction is strictly LOWER (target_rank < current_rank), upgrade is
# strictly HIGHER (target_rank > current_rank).
_TIER_RANK = {"free": 0, "pro": 1, "enterprise": 2}


def _resolve_owner_admin_id(
    *, db, request: Request,
) -> tuple[User, str]:
    """Resolve (user, admin_id) for cookied-owner routes.

    Shared helper for the downgrade twin routes. Returns the validated
    cookied User plus the admin (tenant) id derived from the session
    JWT's tenant_id claim (falling back to the user's first active
    ScopeAssignment for single-scope users).

    Raises:
      HTTPException(401) -- invalid/missing session cookie
      HTTPException(400, detail='no_admin_for_user') -- no active scope
      HTTPException(403, detail='downgrade_requires_owner') -- non-owner
        cookied user (department_lead / teammate). Downgrade is a
        billing-authority action; only the owner role may initiate one.

    Mirrors the resolution logic in ``upgrade_tier`` so the two routes
    cannot drift on auth shape. Inline import of ScopeAssignmentRepository
    matches the upgrade route's pattern -- it's a sibling helper, not a
    long-lived module-level dep.
    """
    cookie = request.cookies.get(settings.session_cookie_name)
    user = _resolve_cookied_user(db=db, session_cookie=cookie)

    session_tenant_id: str | None = None
    try:
        sess_payload = validate_session_token(cookie or "")
        session_tenant_id = sess_payload.get("tenant_id")
    except MagicLinkError:
        session_tenant_id = None

    from app.repositories.scope_assignment_repository import (
        ScopeAssignmentRepository,
    )
    sar = ScopeAssignmentRepository(db)
    active_assignments = sar.list_for_user(user.id, active_only=True)
    if not active_assignments:
        raise HTTPException(status_code=400, detail="no_admin_for_user")
    chosen = next(
        (a for a in active_assignments if a.tenant_id == session_tenant_id),
        active_assignments[0],
    )
    # Only owners may initiate a downgrade. Same gate as upgrade: a
    # department_lead's downgrade attempt would silently strip the
    # owner's seats -- we 403 here so the owner gets a recoverable
    # error in the UI instead.
    if chosen.role != "owner":
        raise HTTPException(
            status_code=403, detail="downgrade_requires_owner",
        )
    return user, chosen.tenant_id


@router.post(
    "/downgrade",
    response_model=DowngradeResponse,
    status_code=status.HTTP_200_OK,
)
def downgrade_tier(
    payload: DowngradeRequest,
    request: Request,
    db: DbSession,
) -> DowngradeResponse:
    """Arm a deferred tier downgrade for the cookied admin owner.

    Cookie-authenticated. Admin id is derived server-side from the
    session JWT's tenant_id claim (with single-scope ScopeAssignment
    fallback); the body cannot specify it. This mirrors the
    cross-admin-attack mitigation on the upgrade route -- a hostile
    Free user cannot post somebody else's admin_id to trigger their
    downgrade.

    Validation (returns 400 detail=...):
      * ``not_a_downgrade``         -- target_tier >= current Admin.tier
      * ``no_admin_for_user``       -- cookied user has no active scope
      * ``admin_inactive``          -- admin row missing or active=False
      * ``no_subscription``         -- caller is Free (no Stripe sub to
                                       cancel; nothing to schedule)
      * 403 ``downgrade_requires_owner`` -- caller is not the owner

    On success returns ``DowngradeResponse`` with ``effective_at`` set
    to the boundary timestamp. NOTHING is mutated on the Admin row or
    entitlement surface here -- the buyer keeps their current tier
    until Stripe fires ``customer.subscription.deleted`` at the
    boundary, at which point the webhook V2 branch applies the tier
    flip + LRU overflow archive.

    Idempotency:
      A second POST with the same target_tier on an admin whose sub
      already has the same ``pending_downgrade_target`` set is a no-op
      on both Stripe and the local row (see
      ``BillingService.schedule_downgrade`` docstring). The webhook
      apply branch is itself idempotent on replay (the LRU query
      excludes rows already stamped with ``pending_downgrade_archived_at``).
    """
    user, admin_id = _resolve_owner_admin_id(db=db, request=request)

    from app.models.admin import Admin as AdminModel
    admin = db.get(AdminModel, admin_id)
    if admin is None or not admin.active:
        raise HTTPException(status_code=400, detail="admin_inactive")

    current_rank = _TIER_RANK.get(admin.tier, -1)
    target_rank = _TIER_RANK.get(payload.target_tier, -1)
    if target_rank >= current_rank:
        # Covers same-tier and upward targets. Same-tier is meaningful
        # to reject because a Pro->Pro "downgrade" would still arm
        # cancel_at_period_end on Stripe -- silently cancelling the
        # buyer's subscription would be a foot-gun.
        raise HTTPException(status_code=400, detail="not_a_downgrade")

    # Audit context. This is a cookied-user-initiated action but the
    # billing surface treats it as system-initiated (no api-key column
    # in actor_key_prefix). We tag with a stable label so forensic
    # queries can isolate cookied account-downgrade flows from the
    # webhook-driven apply rows.
    from app.repositories.admin_audit_repository import AuditContext
    from dataclasses import replace as _dc_replace
    audit_ctx = _dc_replace(
        AuditContext.system(label=f"account_downgrade:user={user.id}"),
        actor_tenant_id=admin_id,
    )

    svc = _service(db)
    try:
        result = svc.schedule_downgrade(
            admin_id=admin_id,
            target_tier=payload.target_tier,
            audit_ctx=audit_ctx,
        )
    except BillingNotConfiguredError as exc:
        raise _501_if_billing_not_ready(exc) from exc
    except ValueError as exc:
        # Map service-layer ValueErrors to 400 detail strings the
        # frontend can switch on. The service raises a ValueError with
        # a descriptive message for: unknown target_tier (impossible
        # at the route layer because Literal already gates it), and
        # Free-admin downgrade attempts (caller has no Subscription).
        msg = str(exc).lower()
        if "no active" in msg and "subscription" in msg:
            raise HTTPException(
                status_code=400, detail="no_subscription",
            ) from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return DowngradeResponse(**result)


@router.post(
    "/downgrade/preview",
    response_model=DowngradePreviewResponse,
    status_code=status.HTTP_200_OK,
)
def downgrade_preview(
    payload: DowngradePreviewRequest,
    request: Request,
    db: DbSession,
) -> DowngradePreviewResponse:
    """Preview the per-axis overflow a downgrade would trigger.

    Pure read. Mutates nothing. Used by Account.tsx's soft-warn
    confirm modal so the buyer sees exactly what would be archived
    at the boundary (\"3 instances and 1 CNAME will be archived\").

    Same auth + admin resolution as ``/downgrade``; the preview is
    also owner-gated so a teammate can't enumerate the owner's
    overflow state. (We could relax to ScopeAssignment-only checks
    if a UX need emerges, but the conservative posture matches our
    pillars: traceability + security.)
    """
    _user, admin_id = _resolve_owner_admin_id(db=db, request=request)

    from app.models.admin import Admin as AdminModel
    admin = db.get(AdminModel, admin_id)
    if admin is None or not admin.active:
        raise HTTPException(status_code=400, detail="admin_inactive")

    current_rank = _TIER_RANK.get(admin.tier, -1)
    target_rank = _TIER_RANK.get(payload.target_tier, -1)
    if target_rank >= current_rank:
        raise HTTPException(status_code=400, detail="not_a_downgrade")

    # Inline import to keep the route module's top-level import graph
    # quiet -- archive service pulls in a chain of ORM imports we'd
    # rather defer until the preview is actually requested.
    from app.services.downgrade_archive_service import (
        DowngradeArchiveService,
    )
    summary = DowngradeArchiveService(db).preview_overflow_for_admin(
        admin_id=admin_id,
        target_tier=payload.target_tier,
    )

    # Project the dataclass-shaped OverflowSummary into the response
    # shape. Always emit the four axes in stable order so the frontend
    # can render the table with hard-coded row labels.
    axis_rows: list[AxisOverflowResponse] = []
    for axis_key in ("instances", "embed_keys", "cnames", "seats"):
        tally = summary.axes.get(axis_key)
        if tally is None:
            # Defensive: service guarantees all four axes are present;
            # if a future refactor drops one, emit a zero-overflow row
            # rather than 500ing the modal.
            axis_rows.append(AxisOverflowResponse(
                axis=axis_key,
                cap=None,
                current=0,
                overflow=0,
                archived_ids=[],
            ))
            continue
        axis_rows.append(AxisOverflowResponse(
            axis=axis_key,
            cap=tally.cap,
            current=tally.current,
            overflow=tally.overflow,
            archived_ids=[str(rid) for rid in tally.archived_ids],
        ))

    return DowngradePreviewResponse(
        admin_id=admin_id,
        current_tier=admin.tier,
        target_tier=payload.target_tier,
        any_overflow=summary.any_overflow,
        axes=axis_rows,
    )


# ---------------------------------------------------------------------
# POST /logout (optional convenience for the marketing site)
# ---------------------------------------------------------------------

@router.post("/logout", status_code=status.HTTP_200_OK)
def logout() -> JSONResponse:
    """Clear the session cookie. Idempotent; safe to call when
    already logged out."""
    response = JSONResponse(content={"ok": True}, status_code=200)
    response.delete_cookie(
        key=settings.session_cookie_name,
        domain=settings.session_cookie_domain or None,
        path="/",
    )
    return response


# ---------------------------------------------------------------------
# POST /signup-free  (Arc 6 Commit 8 -- unified-signup Free flow)
# ---------------------------------------------------------------------
#
# History:
#   * Arc 8 Work-Unit 5 stubbed this route as a captcha-gated
#     placeholder returning ``status="pending-arc-5"``. The mint
#     logic was deferred until the ``admins`` table existed.
#   * Arc 5 Path A landed the ``admins`` table.
#   * Arc 6 Commit 8 (2026-05-23) wires the actual mint here as the
#     load-bearing Free-tier signup surface for the unified-signup
#     redesign (one signup path; Free by default; in-product tier
#     transitions from Account).
#
# Flow (5 atomic steps under one OnboardingService transaction +
# one TierProvisioning transaction):
#
#   1. (Hard-gate, Arc 6 Commit 9) Verify the hCaptcha token. The
#      token is required at the schema layer (Pydantic 422 if
#      missing entirely); a present-but-invalid token returns 422
#      from the route via CaptchaInvalidError; a missing server-side
#      configuration returns 501. The Commit-8 soft-pass window is
#      closed by this commit; production never serves /signup-free
#      without a verified captcha. Closes
#      D-free-tier-captcha-missing-2026-05-22 (P1).
#   2. Resolve-or-create the User row by email.
#   3. Mint a fresh admin_id ("free-<8hex>") and onboard the Admin
#      with ``tier="free"`` + ``tier_source="free_signup"``.
#      Includes retention policies (PIPEDA-compliant) and the
#      admin's first API key in the same transaction.
#   4. Pre-mint the owner-side ScopeAssignment + the buyer's
#      primary Instance via ``TierProvisioningService`` (the same
#      surface Pro / Enterprise use; Arc 6 Commit 8 lifted the L172
#      Free-rejection so all three tiers route through one service).
#   5. Mint a set_password magic-link and send the welcome email.
#      Consuming the link is BOTH proof of email reachability AND
#      the password-set step (one click, no separate verification
#      mailshot). See ``auth_service.set_password`` for the atomic
#      ``email_verified=True`` flip.
#
# After the email send, the user clicks the link, lands at
# ``/auth/set-password``, types a password, the existing
# ``POST /api/v1/auth/set-password`` route auto-logins (mints the
# session cookie) and redirects to ``/app`` (which the marketing
# site routes to /dashboard).
#
# Auth posture: PUBLIC. The path is exempt from api-key middleware
# via the existing ``/api/v1/billing`` prefix on SKIP_AUTH_PATHS
# (see app/middleware/auth.py). The captcha is the access gate.

@router.post(
    "/signup-free",
    response_model=SignupFreeResponse,
    status_code=status.HTTP_200_OK,
)
async def signup_free(
    body: SignupFreeRequest,
    request: Request,
    db: DbSession,
) -> SignupFreeResponse:
    """Free-tier self-serve signup -- unified-signup entry point.

    Response codes:
      * 200 -- success. Body carries ``status="ok"``, ``admin_id``
               (V2 slug ``free-<8hex>``), the echo of the email the
               verification link was sent to, and a human-readable
               message.
      * 409 -- the email is already attached to an Admin ("one Admin
               per email" V2 invariant). Body carries a plain detail
               string for the marketing site to surface.
      * 422 -- captcha verification failed. Two shapes: (a) the
               Pydantic schema layer rejects a request body that
               omits ``captcha_token`` entirely (schema-level 422
               with the standard FastAPI validation envelope); (b)
               the route handler rejects a present-but-invalid
               token from hCaptcha (route-level 422 with
               ``{message, error_codes}``).
      * 501 -- hCaptcha is not configured on this backend
               (``settings.hcaptcha_secret_key`` is empty). Boot-safe
               pattern: the backend boots fine without hCaptcha; the
               route fails 501 (not 500) at request time so an ops
               misconfiguration is debuggable from a single
               structured log line + a clean client-side error.
    """
    # Pull the client IP from request.client for hCaptcha's risk
    # score. We deliberately do NOT trust X-Forwarded-For at this
    # layer -- a future ALB / WAF rewrite of the trusted-IP chain is
    # tracked separately; for now, ``request.client.host`` is what
    # the FastAPI app sees from the ALB-target uvicorn worker.
    remote_ip = request.client.host if request.client else None

    # ----- 1.pre (Soft-gate) 1-per-IP rolling 24h gate ----------------------
    #
    # Arc 7 Commit 6 (2026-05-24) -- second Free-signup from the same IP
    # within a rolling 24h window returns 429. The captcha (next gate) is
    # the hard boundary against scripted abuse; THIS gate is the
    # multi-account abuse boundary (a human who solves the captcha but
    # then tries to mint a second Free admin on the same residential IP).
    #
    # Doctrine choices:
    #   * SOFT gate -- 429 with a human-readable detail string, not a hard
    #     409. The buyer might be a legitimate household behind a shared
    #     NAT (one spouse signed up yesterday, the other today); 429 with
    #     "try again later or contact support" is the right ergonomics.
    #   * Free-only -- paid Stripe Checkout flows leave ``last_signup_ip``
    #     NULL on purpose (the payment surface is the abuse boundary).
    #     This gate runs only inside ``signup_free``.
    #   * Fail-open on missing IP -- ``request.client.host`` can be None
    #     on certain ALB / test paths; the captcha already covered that
    #     surface, and a hard 500 here would be a self-inflicted DoS.
    #   * 24h window -- short enough to not punish a household for a
    #     month, long enough to make a single residential-IP funnel
    #     economically uninteresting at Free's value.
    if remote_ip is not None:
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import select, func
        from app.models.admin import Admin

        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        recent_same_ip = db.execute(
            select(func.count())
            .select_from(Admin)
            .where(
                Admin.last_signup_ip == remote_ip,
                Admin.active.is_(True),
                Admin.created_at >= cutoff,
            )
        ).scalar_one()
        if recent_same_ip >= 1:
            logger.info(
                "signup_free.ip_gate_blocked remote_ip=%s recent_same_ip=%d",
                remote_ip, recent_same_ip,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    "An account from your network was created in the last "
                    "24 hours. Please try again later or contact support if "
                    "this is a shared connection (office, household, campus)."
                ),
            )

    # ----- 1. (Hard-gate) Captcha verification ------------------------------
    #
    # Arc 6 Commit 9 (2026-05-23) -- the Commit-8 soft-pass branch is
    # removed. `captcha_token` is required at the schema layer, so a
    # missing/empty token is rejected as Pydantic 422 BEFORE this
    # handler runs; the only paths to consider here are
    # (verify success) -> fall through to mint; (CaptchaNotConfigured)
    # -> 501; (CaptchaInvalid) -> 422 with structured error codes.
    # Closes D-free-tier-captcha-missing-2026-05-22 (P1).
    try:
        await verify_captcha(body.captcha_token, remote_ip=remote_ip)
    except CaptchaNotConfiguredError as exc:
        logger.warning(
            "signup_free.captcha_not_configured remote_ip=%s",
            remote_ip or "unknown",
        )
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=str(exc),
        )
    except CaptchaInvalidError as exc:
        logger.info(
            "signup_free.captcha_invalid error_codes=%s remote_ip=%s",
            exc.error_codes,
            remote_ip or "unknown",
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": exc.message,
                "error_codes": exc.error_codes,
            },
        )

    # ----- 2-5. Mint Admin + ScopeAssignment + Instance + email link ------
    #
    # Inline imports to keep the route module's top-level import
    # graph from pulling the full service stack into every request
    # (the route module is hot-imported by every billing API call).
    from app.repositories.admin_audit_repository import AuditContext
    from app.services.billing_webhook_service import (
        _mint_admin_id_from_email,
    )
    from app.services.onboarding_service import OnboardingService
    from app.services.tier_provisioning_service import (
        TierProvisioningService,
    )
    from app.services.magic_link_service import (
        build_set_password_url,
        mint_set_password_token,
    )
    from app.services.email_service import send_welcome_set_password_email
    from app.services.billing_webhook_service import BillingWebhookService

    email = body.email.lower()
    display_name = body.display_name.strip()

    # Audit context. Free signup is system-initiated (no api-key, no
    # cookie); we tag the audit row with a stable label so forensic
    # queries can isolate the Free funnel.
    audit_ctx = AuditContext.system(label="signup_free")

    # 2. Resolve-or-create the User row. We borrow the
    #    BillingWebhookService helper because the resolve-or-create
    #    semantics are identical (LOWER(email) lookup; new rows born
    #    synthetic=False + active=True). Reusing the helper keeps
    #    the User-row shape consistent across paid checkout and
    #    free signup.
    webhook_helper = BillingWebhookService(db)
    user = webhook_helper._resolve_or_create_user(
        email=email, display_name=display_name
    )
    # ``_resolve_or_create_user`` flushes but does not commit; the User
    # row is staged in the current SQLAlchemy session and will be
    # committed by ``OnboardingService.onboard_tenant`` below as part
    # of the same atomic transaction (matches the paid-checkout
    # webhook pattern -- see billing_webhook_service.py:333-354).

    # 3. Onboard the Admin.
    admin_id = _mint_admin_id_from_email(email, tier="free")
    onboarding = OnboardingService(db)
    try:
        onboarding.onboard_tenant(
            tenant_id=admin_id,
            display_name=display_name,
            tier="free",
            tier_source="free_signup",
            description=f"Self-serve Free signup -- email={email}",
            api_key_display_name=f"{display_name} -- Free admin key",
            created_by="signup_free",
            audit_ctx=audit_ctx,
        )
    except ValueError as exc:
        # Slug collision (1 in 2^32) OR the (unlikely) repeat of an
        # admin_id from a previous Free signup retry. We surface as
        # 409 so the marketing site can offer a "try again" affordance.
        logger.warning(
            "signup_free.admin_collision email=%s admin_id=%s detail=%s",
            email, admin_id, exc,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    # 3.5 Stamp last_signup_ip on the freshly-minted Admin row.
    #     We do this AFTER onboard_tenant returns (so we know the row
    #     exists and the slug collision check already ran) and BEFORE
    #     pre-mint (so a later pre-mint failure cannot leave the row
    #     un-stamped). Best-effort: if the write fails the funnel
    #     still completes (the gate is a soft 429, not a hard 5xx,
    #     so degraded write here means at-most-one missed gate, not
    #     a broken funnel).
    if remote_ip is not None:
        try:
            from app.models.admin import Admin

            admin_row = db.get(Admin, admin_id)
            if admin_row is not None:
                admin_row.last_signup_ip = remote_ip
                db.flush()
                logger.info(
                    "signup_free.ip_stamped admin=%s",
                    admin_id,
                )
        except Exception:  # pragma: no cover -- best-effort stamp
            logger.exception(
                "signup_free.ip_stamp_failed admin=%s",
                admin_id,
            )

    # 4. Pre-mint ScopeAssignment + primary Instance.
    #    TierProvisioningService.premint_for_tier accepts TIER_FREE as
    #    of Arc 6 Commit 8; the pre-mint shape is identical across
    #    Free / Pro / Enterprise (only entitlement caps differ).
    try:
        premint = TierProvisioningService(db)
        premint.premint_for_tier(
            tenant_id=admin_id,
            tier="free",
            primary_user=user,
            audit_ctx=audit_ctx,
        )
    except Exception:  # pragma: no cover -- best-effort post-onboard
        # The Admin row is already committed; pre-mint failure leaves
        # the buyer with an Admin but no Instance/ScopeAssignment.
        # Log loudly; a reconciler can re-run. We still send the
        # welcome email so the buyer can recover via the link.
        logger.exception(
            "signup_free.premint_failed admin=%s email=%s",
            admin_id, email,
        )

    # 5. Mint set-password magic-link + send welcome email.
    try:
        token = mint_set_password_token(
            user_id=user.id,
            email=email,
            tenant_id=admin_id,
            purpose="signup",
        )
        url = build_set_password_url(token)
        send_welcome_set_password_email(
            to_email=email,
            set_password_url=url,
            display_name=display_name,
        )
        logger.info(
            "signup_free.welcome_email_sent admin=%s email=%s",
            admin_id, email,
        )
    except Exception:  # pragma: no cover
        # Email is best-effort. The buyer can recover via
        # POST /api/v1/auth/forgot-password against the same email
        # (the password-reset and set-password tokens are
        # interchangeable at the route layer).
        logger.exception(
            "signup_free.welcome_email_failed admin=%s email=%s",
            admin_id, email,
        )

    return SignupFreeResponse(
        status="ok",
        admin_id=admin_id,
        email=email,
        message=(
            "Account created. Check your email for a link to set your "
            "password -- the link also confirms your email address."
        ),
    )
