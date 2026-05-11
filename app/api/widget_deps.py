"""Step 30b commit (c): widget-endpoint dependencies.

Why this module exists
----------------------

The chat widget endpoint (POST /api/v1/chat/widget) shares the
existing ApiKeyAuthMiddleware for credential resolution but layers
extra constraints on top that DO NOT apply to admin/server-to-server
keys:

  1. The resolved key_kind MUST be 'embed'. Admin keys cannot drive
     widget traffic; if they could, an admin-key leak (much higher
     blast radius) would also be a widget abuse vector.
  2. The permissions array MUST be exactly ['chat']. The widget at
     v1 is conversational only -- no tool calls, no admin surface.
     Step 30c will introduce the three-tier action classification
     (routine / notify-and-proceed / approval-required) before any
     tool path is wired through the widget. This guard makes the
     lockstep mechanical, not policy-only.
  3. The Origin header MUST exact-match (scheme + host + port) at
     least one entry in the embed key's allowed_origins. This
     binds the public credential to the customer site that
     installed it; copying the key onto a different origin fails.

Each constraint raises a 4xx with a stable error code so the
widget bundle and the customer's debug surface can distinguish
configuration errors from bad requests.

Rate limiting on the widget endpoint is enforced via a static
slowapi limit string ("30/minute") applied at decoration time. The
per-embed-key dynamic cap that this module previously exposed via
``embed_per_minute_limit_string`` shipped broken: slowapi calls a
zero-arg or one-arg-keyed provider (slowapi/wrappers.py:85-94) and
never passes a Request, so a provider signed as
``(request: Request) -> str`` raised TypeError before any limit
was computed. Rather than paper over the abstraction mismatch with
a contextvar hack or a per-request DB hit inside the slowapi
callback, the per-key dynamic cap is deferred to a real feature
(tracked in DRIFTS as D-embed-key-dynamic-rate-limit-deferred); the
v1 widget surface uses the conservative global cap, which is the
same cap admin chat uses and which we already validated under
load.

Pattern E note
--------------

No row mutations. The dependencies are pure read-side checks on
fields already populated by auth middleware. A failed origin check
emits NO admin_audit row -- audit emission for embed-key auth
failures is a noted Step 30c follow-up (it ties into the action
classifier's "notify-and-proceed" surface) and is tracked in
DRIFTS only when (e) lands and reveals the gap is real.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status

# Step 30b commit (a) of step-30b-embed-key-issuance: import the
# embed-permission contract from the schema layer so the runtime gate
# (this module) and the issuance validator (app.api.v1.admin via the
# EmbedKeyCreate schema) read from the same frozenset object. Pre-30b
# this constant lived here as a private '_EMBED_REQUIRED_PERMISSIONS';
# the schema layer is the canonical home for permission vocabulary
# (alongside ALLOWED_PERMISSIONS).
from app.schemas.api_key import EMBED_REQUIRED_PERMISSIONS

logger = logging.getLogger(__name__)


# Stable error codes the widget bundle reads to render the right UI
# state (e.g. silently retry vs. tell the embedder to fix their
# install). Strings are part of the public contract; do not rename
# without bumping a version flag visible to the embedder.
ERR_KEY_NOT_EMBED = "embed_key_required"
ERR_PERMISSIONS_MISMATCH = "embed_permissions_mismatch"
ERR_ORIGIN_MISSING = "origin_header_missing"
ERR_ORIGIN_NOT_ALLOWED = "origin_not_allowed"
ERR_ORIGIN_LIST_EMPTY = "embed_key_origin_list_empty"


def _normalize_origin(value: str) -> str:
    """Lowercase scheme and host; preserve port and path-stripping.

    Origin headers per RFC 6454 are scheme + host + port with no path
    or trailing slash. We strip any trailing whitespace and normalize
    case on the scheme/host segment so 'HTTPS://Example.com' and
    'https://example.com' compare equal. Port is case-insensitive
    naturally (digits only); we leave it intact.
    """
    return value.strip().lower()


def require_embed_key(request: Request) -> dict:
    """FastAPI dependency: gate widget traffic behind embed-key constraints.

    Returns the resolved widget_config dict (or {} if NULL on the row)
    so the endpoint can echo greeting_message / display_name back to
    the widget on the first SSE frame. The dict is treated as opaque
    here; schema validation of widget_config keys lives at issuance
    time (admin endpoint, future commit) so a malformed row never
    reaches this code path in practice.

    Raises HTTPException(403) for credential-class / permission /
    origin failures. The widget bundle distinguishes these via the
    JSON detail.code field, never via free-form text.
    """
    key_kind = getattr(request.state, "key_kind", None)
    permissions = getattr(request.state, "permissions", None) or []
    allowed_origins = getattr(request.state, "allowed_origins", None)
    widget_config = getattr(request.state, "widget_config", None) or {}

    if key_kind != "embed":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": ERR_KEY_NOT_EMBED,
                "message": (
                    "This endpoint requires an embed key. Admin keys "
                    "cannot drive widget traffic."
                ),
            },
        )

    if frozenset(permissions) != EMBED_REQUIRED_PERMISSIONS:
        # Lockstep with Step 30c: until the action classifier ships,
        # embed keys are conversational only. A future commit relaxes
        # this gate by adding tier-scoped permission tokens.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": ERR_PERMISSIONS_MISMATCH,
                "message": (
                    "Embed keys at v1 must have permissions == ['chat']. "
                    "Tool paths require Step 30c."
                ),
            },
        )

    if not allowed_origins:
        # An embed key with no allowed_origins should never have been
        # issued (the issuance path will reject it in a future commit),
        # but if it slips through we fail closed here.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": ERR_ORIGIN_LIST_EMPTY,
                "message": (
                    "This embed key has no configured allowed origins. "
                    "Re-issue the key with at least one origin."
                ),
            },
        )

    origin_header = request.headers.get("Origin")
    if not origin_header:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": ERR_ORIGIN_MISSING,
                "message": (
                    "Origin header is required on widget requests. "
                    "Browsers attach it automatically; if you are "
                    "calling from a non-browser client this endpoint "
                    "is the wrong one."
                ),
            },
        )

    incoming = _normalize_origin(origin_header)
    allowed_normalized = {_normalize_origin(o) for o in allowed_origins if o}
    if incoming not in allowed_normalized:
        # Log the failure server-side so the operator can correlate
        # widget install issues without leaking the allowlist back to
        # the caller (which would help an attacker enumerate it).
        logger.warning(
            "embed-key origin rejected: key_prefix=%s incoming=%s",
            getattr(request.state, "key_prefix", None),
            incoming,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": ERR_ORIGIN_NOT_ALLOWED,
                "message": (
                    "This origin is not allowed for the supplied embed "
                    "key. Update the key's allowed_origins or install "
                    "the widget on the registered origin."
                ),
            },
        )

    return widget_config


# Static rate limit applied via @limiter.limit at decoration time on
# the widget endpoint. See module docstring above for why we no
# longer expose a per-request callable. Keeping the value as a
# module-level constant (rather than inlined in chat_widget.py)
# preserves a single place to revisit when the dynamic-cap feature
# is built for real.
EMBED_WIDGET_RATE_LIMIT = "30/minute"


def cors_response_headers(request: Request, widget_config: dict | None) -> dict[str, str]:
    """Build the CORS response headers for the widget SSE response.

    The widget runs on customer origins, so the response must echo
    back the request's Origin (which we have already validated against
    the embed key's allowlist via require_embed_key). We do NOT use
    the wildcard '*' because (a) credentials are not used and (b)
    echoing the validated origin keeps the surface auditable.
    """
    origin = request.headers.get("Origin", "")
    headers = {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
        # Vary on Origin so caches do not bleed responses across
        # customer sites.
        "Vary": "Origin",
    }
    return headers
