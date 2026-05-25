"""Step 30a.3: password-auth API routes.

Public surface for the mandatory-at-signup password flow. Three routes:

  POST /api/v1/auth/login            -- email + password -> session cookie
  POST /api/v1/auth/set-password     -- redeem set/reset token -> session
  POST /api/v1/auth/forgot-password  -- mint reset link (always 200)

Auth model:

  * /login              -- anonymous; rate limiting at the edge.
  * /set-password       -- token-gated; the bootstrap-token (set_password
                           or reset_password class) is the credential.
                           The route tries the set class first then falls
                           back to reset; both terminate in the same
                           ``AuthService.set_password`` write and the same
                           session-cookie mint.
  * /forgot-password    -- anonymous; ALWAYS returns 200 with a generic
                           body regardless of whether the email maps to
                           a real account, so a probing client cannot
                           enumerate emails.

Cookie semantics are identical to the magic-link path -- ``mint_session_token``
+ ``_set_session_cookie`` produce the same payload shape the Step 31.2
middleware already understands ({sub, scope: "session", iat, exp}); the
middleware does not care which surface minted the cookie.

Full architecture: see docs/ARCHITECTURE.md §3.2.13 (Billing surface --
Step 30a.3 password sub-surface).
Roadmap row: docs/CANONICAL_RECAP.md §12 Step 30a.3 (closing tag
``step-30a-3-password-auth-mandatory-at-signup-complete``).
Drift closure: docs/DRIFTS.md §3
``D-magic-link-only-auth-no-password-fallback-2026-05-16``.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Cookie, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field

from app.api.deps import DbSession
from app.api.v1.billing import _set_session_cookie  # reuse the canonical cookie stamp
from app.core.config import settings
from app.services.auth_service import (
    AuthError,
    PasswordTooShortError,
    request_password_reset,
    set_password as auth_set_password,
    verify_password,
)
from app.services.billing_service import BillingService
from app.services.magic_link_service import (
    MagicLinkError,
    consume_reset_password_token,
    consume_set_password_token,
    mint_session_token,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


# ---------------------------------------------------------------------
# Request / response schemas (local -- these are auth-flow-specific
# and not shared with any other surface, so they live next to the routes
# rather than in app/schemas/).
# ---------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class LoginResponse(BaseModel):
    ok: bool = True
    redirect: str = "/app"
    email: str
    tenant_id: str


class SetPasswordRequest(BaseModel):
    token: str = Field(min_length=1)
    password: str = Field(min_length=1)


class SetPasswordResponse(BaseModel):
    ok: bool = True
    redirect: str = "/app"


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ForgotPasswordResponse(BaseModel):
    ok: bool = True
    message: str = "If an account exists, a reset link has been sent."


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _resolve_tenant_for_user(db, user_id) -> str:
    """Return the tenant_id the cookied user belongs to, else "".

    Tenant binding is owned by ScopeAssignment, NOT by Stripe billing.
    Owners satisfy both (a Stripe subscription on file *and* an
    owner-role ScopeAssignment created by tier_provisioning_service),
    so the pre-30a.5 billing-only resolver coincidentally returned the
    right tenant_id for the paying user. The moment a non-paying role
    (teammate / department_lead / tenant_admin) logged in after
    redeeming an invite, the billing lookup returned ``None`` and the
    cookie carried ``tenant_id=""``. The frontend then surfaced the
    "No subscription on file" empty state to a fully-provisioned
    teammate. Caught in the Step 30a.5 $1,000 live smoke walk.
    Drift id: D-invite-redeemed-user-sees-no-subscription-on-file-2026-05-18.
    Symmetric to the May-17 inviter-side fix at
    tier_provisioning_service.py::_ensure_owner_scope_assignment.

    Resolution order:
      1. Active ScopeAssignment(s) for ``user_id`` via
         ``ScopeAssignmentRepository.list_for_user``. Prefer
         ``role='owner'`` (canonical primary scope), else fall back to
         the most-recently-started active assignment among the rest.
         Returns its ``tenant_id``. This is the path every
         redeemed-invite login takes.
      2. Fallback: ``BillingService.get_active_subscription_for_user``.
         Preserves the narrow race window where a Stripe subscription
         row exists but ``tier_provisioning_service`` has not yet
         written the owner ScopeAssignment (mid-checkout, before the
         post-payment provisioning leg runs). Without this, an owner
         logging in during that ~1s window would see the same empty
         state we just fixed.
      3. Else ``""``. Downstream session middleware re-resolves on the
         next request when a scope finally lands.
    """
    # Arc 9 C20 — RLS-aware fast path via SECURITY DEFINER function.
    #
    # Direct ORM reads on scope_assignments are blocked by FORCE RLS
    # at login time because we have not yet set app.admin_id — that
    # GUC is precisely what we're trying to discover. Calling the
    # SECURITY DEFINER function ``arc9_c20_resolve_tenant_for_user``
    # (owned by luciel_ops, BYPASSRLS) executes the lookup with the
    # owner's privileges and returns ONLY a tenant_id string. The
    # function applies the same (owner-first, else most-recent
    # active) priority that the legacy ORM path below would have
    # used, so behaviour is unchanged for tenants where RLS happens
    # to be permissive.
    #
    # If the function returns a non-empty string we trust it and
    # return immediately. If it returns NULL/empty we fall through
    # to the mid-checkout Stripe race fallback (step 2 below).
    #
    # See alembic/versions/arc9_c20_resolve_tenant_secdef.py for the
    # full doctrine of why a SECURITY DEFINER function is the right
    # tool here (vs BYPASSRLS ops session, vs row_security=off, etc.).
    from sqlalchemy import text as _sa_text

    secdef_tid = db.execute(
        _sa_text(
            "SELECT public.arc9_c20_resolve_tenant_for_user(:uid)"
        ),
        {"uid": str(user_id)},
    ).scalar()
    if secdef_tid:
        return secdef_tid

    # 2. Mid-checkout race fallback (preserves pre-30a.5 owner-only
    #    behavior for that narrow window). BillingService reads
    #    subscriptions, which has its own RLS policy keyed on
    #    app.admin_id too — but the post-Stripe-webhook code path
    #    that lands a Subscription row before any ScopeAssignment
    #    exists is exceedingly rare in V2 (Free has no Subscription;
    #    Pro/Enterprise mint ScopeAssignment in the same provisioning
    #    txn). Leaving this fallback in place preserves the historical
    #    contract; if it ever fires under prod RLS it will return
    #    None and we fall through to "" which is the existing
    #    "no tenant yet" sentinel.
    svc = BillingService(db=db, stripe_client=None)  # type: ignore[arg-type]
    sub = svc.get_active_subscription_for_user(user_id=user_id)
    return sub.tenant_id if sub is not None else ""


def _mint_and_set_session(
    *,
    response: Response,
    db,
    user_id,
    email: str,
) -> str:
    """Mint a session JWT for ``user_id`` and stamp it as a cookie.

    Returns the resolved tenant_id (for the response body).
    """
    tenant_id = _resolve_tenant_for_user(db, user_id)
    token = mint_session_token(user_id=user_id, email=email, tenant_id=tenant_id)
    _set_session_cookie(response, token)
    return tenant_id


# ---------------------------------------------------------------------
# POST /login
# ---------------------------------------------------------------------


@router.post(
    "/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
)
def login(
    payload: LoginRequest,
    response: Response,
    db: DbSession,
) -> LoginResponse:
    """Verify password and mint a session cookie.

    On any failure mode (user not found, password mismatch, user
    inactive, password not yet set) returns a single generic 401 with
    a constant-time verify path inside ``AuthService.verify_password``
    so a probing client cannot enumerate emails or distinguish failure
    classes by timing.
    """
    try:
        user = verify_password(
            db=db,
            email=str(payload.email),
            password=payload.password,
        )
    except AuthError:
        logger.info("auth: login failed email=%s", payload.email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    tenant_id = _mint_and_set_session(
        response=response, db=db, user_id=user.id, email=user.email,
    )
    logger.info(
        "auth: login ok user_id=%s email=%s tenant_id=%s",
        user.id, user.email, tenant_id or "<none>",
    )
    return LoginResponse(email=user.email, tenant_id=tenant_id)


# ---------------------------------------------------------------------
# POST /set-password
# ---------------------------------------------------------------------


@router.post(
    "/set-password",
    response_model=SetPasswordResponse,
    status_code=status.HTTP_200_OK,
)
def set_password(
    payload: SetPasswordRequest,
    response: Response,
    db: DbSession,
) -> SetPasswordResponse:
    """Redeem a set-password or reset-password token and write the hash.

    Tries the ``set_password`` class first, falls back to
    ``reset_password``; both terminate in the same
    ``AuthService.set_password`` call. On success commits, mints a
    session cookie, and returns ``/app`` as the redirect target.

    Failure modes:
      * Token invalid / expired / wrong class -> 401.
      * Password under the 8-char floor -> 422 with code
        ``password_too_short`` so the frontend can render an inline
        form-validation message.
      * User not found / inactive (token-user mismatch) -> 401.
    """
    # Try set-password first, then reset-password. Both consume_* helpers
    # raise MagicLinkError on any decode failure (wrong typ, expired,
    # bad signature). The dual-attempt is cheap (JWT decode is in-process
    # only) and lets the marketing site use a single page for both flows.
    claims: dict
    token_class: str
    try:
        claims = consume_set_password_token(payload.token)
        token_class = "set_password"
    except MagicLinkError:
        try:
            claims = consume_reset_password_token(payload.token)
            token_class = "reset_password"
        except MagicLinkError as exc:
            logger.info("auth: set-password token rejected: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired link.",
            ) from exc

    # Step 30a.4 -- if the token's purpose is "invite", route to the
    # InviteService.redeem_invite path rather than the bare set-password
    # path. That path provisions User + Agent + ScopeAssignment from the
    # UserInvite row, sets the password, marks the invite accepted, and
    # emits ACTION_INVITE_REDEEMED -- all in one transaction. We then
    # mint the session cookie against the freshly-provisioned User.
    purpose = claims.get("purpose")
    if token_class == "set_password" and purpose == "invite":
        from app.repositories.admin_audit_repository import AuditContext
        from app.services import invite_service as _invite_service

        audit_ctx = AuditContext.system(label="invite_redemption")
        try:
            _invite, user = _invite_service.redeem_invite(
                db=db,
                token=payload.token,
                payload=claims,
                password=payload.password,
                audit_ctx=audit_ctx,
            )
        except _invite_service.InviteExpiredError as exc:
            logger.info("auth: invite redemption expired: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="Invite has expired. Ask your admin to resend.",
            ) from exc
        except _invite_service.InviteNotPendingError as exc:
            logger.info("auth: invite redemption non-pending: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This invite has already been used or revoked.",
            ) from exc
        except _invite_service.InviteNotFoundError as exc:
            logger.info("auth: invite redemption not found: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired link.",
            ) from exc
        except PasswordTooShortError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"message": str(exc), "code": "password_too_short"},
            ) from exc

        _mint_and_set_session(
            response=response, db=db, user_id=user.id, email=user.email,
        )
        logger.info(
            "auth: invite redeemed via set-password token user_id=%s",
            user.id,
        )
        return SetPasswordResponse()

    user_id = claims.get("sub")
    email = claims.get("email")
    if not user_id or not email:
        logger.warning("auth: set-password token missing sub/email: %s", claims)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired link.",
        )

    try:
        auth_set_password(db=db, user_id=user_id, password=payload.password)
    except PasswordTooShortError as exc:
        # 422 with a machine-readable code so the React form can render
        # an inline "Password must be at least 8 characters" message
        # rather than a generic alert.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": str(exc), "code": "password_too_short"},
        ) from exc
    except LookupError as exc:
        logger.info("auth: set-password user not found user_id=%s", user_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired link.",
        ) from exc

    db.commit()
    _mint_and_set_session(
        response=response, db=db, user_id=user_id, email=email,
    )
    logger.info(
        "auth: password set via %s token user_id=%s purpose=%s",
        token_class, user_id, claims.get("purpose", "n/a"),
    )
    return SetPasswordResponse()


# ---------------------------------------------------------------------
# POST /forgot-password
# ---------------------------------------------------------------------


@router.post(
    "/forgot-password",
    response_model=ForgotPasswordResponse,
    status_code=status.HTTP_200_OK,
)
def forgot_password(
    payload: ForgotPasswordRequest,
    db: DbSession,
) -> ForgotPasswordResponse:
    """Mint a reset-password link and email it.

    ALWAYS returns 200 with the same generic body regardless of whether
    the email maps to a real account or whether the SES delivery
    succeeded -- a probing client cannot infer either signal. The
    service-level log row records the actual outcome.
    """
    try:
        request_password_reset(db=db, email=str(payload.email))
    except Exception:  # noqa: BLE001 -- generic 200 contract
        # The service swallows EmailDeliveryError internally. Any
        # surprise exception is logged and we still return the
        # generic 200 -- the user experience must be identical to
        # the success path.
        logger.exception(
            "auth: forgot-password unexpected error email=%s",
            payload.email,
        )

    return ForgotPasswordResponse()
