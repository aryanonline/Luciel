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
    """Return the cookied user's current subscription state.

    Returns 404 if the user has no subscription on file -- the
    Account/billing UI uses that to show the "subscribe" CTA.

    Step 30a.5: also surfaces ``active_role`` -- the cookied user's
    role on their active ScopeAssignment (preferring the assignment
    matching the session JWT's tenant_id, falling back to the first
    active assignment for single-tenant common case). The dashboard
    gates the CompanyTab on (tier=='company' AND role in
    ('tenant_admin','owner')) and the TeamTab on (tier in
    ('team','company') AND role in ('tenant_admin','owner',
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

    # Resolve active_role off the cookied user's ScopeAssignment.
    # Users with no active assignment surface as active_role=None
    # (rather than 403), since /billing/me is a read-only status
    # endpoint -- the dashboard simply hides org-building tabs.
    from app.repositories.scope_assignment_repository import (
        ScopeAssignmentRepository,
    )
    sar = ScopeAssignmentRepository(db)
    active_assignments = sar.list_for_user(user.id, active_only=True)
    active_role: str | None = None
    if active_assignments:
        chosen = next(
            (a for a in active_assignments if a.tenant_id == session_tenant_id),
            active_assignments[0],
        )
        active_role = chosen.role

    svc = _service(db)
    sub = svc.get_active_subscription_for_user(user_id=user.id)
    if sub is None:
        raise HTTPException(status_code=404, detail="No subscription on file.")

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

    return SubscriptionStatusResponse(
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
#   1. (Soft-gate) Verify the hCaptcha token. Captcha is Optional
#      during the Commit 8 -> Commit 9 sandbox window; Commit 9
#      flips it back to required + lands the front-end widget +
#      tightens one-per-email-and-IP accounting (closes
#      D-free-tier-captcha-missing-2026-05-22).
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
      * 422 -- captcha verification failed when a token was provided
               (token missing entirely is a Commit 8 soft-pass, see
               schema docstring).
      * 501 -- hCaptcha is not configured on this backend
               (``settings.hcaptcha_secret_key`` is empty) AND a
               token was provided. Boot-safe pattern: the backend
               boots fine without hCaptcha; the route still works
               when no token is sent.
    """
    # Pull the client IP from request.client for hCaptcha's risk
    # score. We deliberately do NOT trust X-Forwarded-For at this
    # layer -- a future ALB / WAF rewrite of the trusted-IP chain is
    # tracked separately; for now, ``request.client.host`` is what
    # the FastAPI app sees from the ALB-target uvicorn worker.
    remote_ip = request.client.host if request.client else None

    # ----- 1. (Soft-gate) Captcha verification ------------------------------
    #
    # Arc 6 Commit 8 window: captcha_token is Optional. If absent or
    # empty, log a structured WARN and continue. Commit 9 flips this
    # back to hard-required. The window is sandbox-only because
    # Commit 10 is the deploy gate and Commit 9 ships first.
    if body.captcha_token:
        try:
            await verify_captcha(body.captcha_token, remote_ip=remote_ip)
        except CaptchaNotConfiguredError as exc:
            logger.warning("signup_free.captcha_not_configured")
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail=str(exc),
            )
        except CaptchaInvalidError as exc:
            logger.info(
                "signup_free.captcha_invalid error_codes=%s",
                exc.error_codes,
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "message": exc.message,
                    "error_codes": exc.error_codes,
                },
            )
    else:
        # D-free-tier-captcha-missing-2026-05-22 -- intentionally
        # logged loud at WARN so we can grep this window's signups
        # in CloudWatch when Commit 9 lands and we audit the gap.
        logger.warning(
            "signup_free.captcha_soft_pass email=%s remote_ip=%s "
            "window=commit-8-to-commit-9",
            body.email,
            remote_ip or "unknown",
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
