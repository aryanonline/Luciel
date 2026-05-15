"""Step 30a: billing API routes.

Public surface for the self-serve subscription flow. Eight routes
(seventh route /pilot-refund added in Step 30a.2-pilot):

  POST /api/v1/billing/checkout           -- create a Stripe Checkout session
  POST /api/v1/billing/webhook            -- Stripe webhook receiver
  POST /api/v1/billing/onboarding/claim   -- post-checkout email-link mint
  GET  /api/v1/billing/login              -- exchange magic-link token for cookie
  POST /api/v1/billing/portal             -- Stripe Customer Portal session
  POST /api/v1/billing/pilot-refund       -- Step 30a.2-pilot: self-serve
                                             $100 refund + cancel in 90-day window
  GET  /api/v1/billing/me                 -- read current subscription state
  POST /api/v1/billing/logout             -- clear the session cookie

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
        # explicitly so the apex domain (luciel.ai) sees it.
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
            BillingWebhookService(db)._record_unknown_event(
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
        result = BillingWebhookService(db).handle(event_dict)
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

      * if the webhook already minted the subscription, mint a fresh
        magic link and email it (idempotent: each claim re-sends the
        link, so a buyer who closed the email tab can re-trigger from
        the marketing site without sales involvement).
      * if the webhook has not arrived yet, return 'pending' so the
        marketing site can show "we'll email you shortly" and the
        webhook drives the eventual email send.
      * if Stripe does not recognize the session_id, return 'unknown'.
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

    # Subscription exists -- send a fresh magic link.
    user = db.get(User, sub.user_id)
    if user is None:  # pragma: no cover - referential integrity guarantees this
        return OnboardingClaimResponse(state="unknown", email_sent_to=None)

    try:
        token = mint_magic_link_token(user_id=user.id, email=user.email, tenant_id=sub.tenant_id)
        url = build_magic_link_url(token)
        # Reuse the email service from the webhook path.
        from app.services.email_service import send_magic_link_email
        send_magic_link_email(to_email=user.email, magic_link_url=url, display_name=user.display_name)
    except MagicLinkError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=str(exc),
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
    """
    cookie = request.cookies.get(settings.session_cookie_name)
    user = _resolve_cookied_user(db=db, session_cookie=cookie)

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
    snapshot = sub.provider_snapshot if isinstance(sub.provider_snapshot, dict) else {}
    snapshot_meta = snapshot.get("metadata") if isinstance(snapshot.get("metadata"), dict) else {}
    is_pilot = (
        str(snapshot_meta.get("luciel_intro_applied", "")).lower() == "true"
        and sub.trial_end is not None
    )
    pilot_window_end = sub.trial_end if is_pilot else None

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
