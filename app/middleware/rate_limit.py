from __future__ import annotations

import logging
import os
import time
from typing import Optional

from slowapi import Limiter
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import settings
from app.policy.entitlements import (
    ALL_TIERS_V2,
    TIER_FREE,
    per_key_api_rate_limit_rpm,
    resolve_entitlement,
)

logger = logging.getLogger("luciel.ratelimit")

# Arc 7 Commit 4 (WU-2): tier-aware rate limiting.
#
# Pre-Arc-7 the limiter ran on three hard-coded fixed strings
# (CHAT_RATE_LIMIT="20/minute", ADMIN_RATE_LIMIT="30/minute",
# KNOWLEDGE_UPLOAD_RATE_LIMIT="10/minute") that were the same for
# every tier, so the founder-locked Option-A api_rate_limit_rpm axis
# (Free=30, Pro=300, Enterprise=3000) at
# ``app/policy/entitlements.py`` was decorative -- it appeared in the
# matrix but nothing read it. Path A doctrine ("whatever we ship out
# in our code and prod and schema must be aligned with this vision")
# requires that the value Pro buyers pay for ACTUALLY apply.
#
# The wiring works because ApiKeyAuthMiddleware runs ahead of the
# route's ``@limiter.limit(...)`` decorator (the decorator fires
# inside the route, after auth has populated request.state). So the
# key-func + limit-provider pair can both read request.state.admin_id
# (== admin_id) and request.state.luciel_instance_id to form a
# per-(admin, instance) Redis bucket -- tenant cross-talk is
# impossible and one Instance going hot doesn't starve sibling
# Instances of the same Admin.
#
# SlowAPI's LimitGroup invokes the limit_provider callable with the
# result of key_func(request) if the callable's signature has a
# ``key`` parameter, else with no args. We encode the tier into the
# key (``tier:{tier}:admin:{admin_id}:inst:{inst_id}``) and parse it
# back in the limit provider -- one cached DB hit per request gets
# reused for both the bucket key and the cap lookup.
#
# Failure posture (fail-safe to Free=30rpm):
#   * No admin context (anonymous, widget, health) -> ``ip:{ip}`` key,
#     30rpm cap. The widget has its own EMBED_WIDGET_RATE_LIMIT path
#     in app/api/widget_deps.py and is not touched here.
#   * Admin tier lookup raises -> log warning + treat as Free. We do
#     NOT silently apply the highest cap; an unknown caller is the
#     LEAST trustworthy, not the MOST.
#   * Cache stale -> 60s TTL on the admin->tier map means a tier
#     upgrade is visible inside one minute. Buyers who just upgraded
#     may see one minute of the lower cap; that's an acceptable
#     trade for keeping the DB hit off the hot path.
_ADMIN_TIER_CACHE_TTL_SECONDS = 60.0
_ADMIN_TIER_CACHE_MAX_ENTRIES = 4096
# {admin_id: (tier, expires_at_monotonic)}
_admin_tier_cache: dict[str, tuple[str, float]] = {}


def _lookup_admin_tier(admin_id: str) -> str:
    """Resolve admin_id -> tier with 60s LRU+TTL cache.

    Fail-safe: any exception (DB down, admin row missing, unknown
    tier value) returns ``TIER_FREE`` so the caller is held to the
    most restrictive cap rather than the most permissive.
    """
    now = time.monotonic()
    cached = _admin_tier_cache.get(admin_id)
    if cached is not None and cached[1] > now:
        return cached[0]

    # Cheap eviction: if the cache grew past the max, drop the
    # oldest-expiring entry. This keeps memory bounded without
    # importing a heavier LRU.
    if len(_admin_tier_cache) >= _ADMIN_TIER_CACHE_MAX_ENTRIES:
        try:
            stale_key = min(_admin_tier_cache, key=lambda k: _admin_tier_cache[k][1])
            _admin_tier_cache.pop(stale_key, None)
        except ValueError:
            pass

    tier = TIER_FREE
    try:
        # Lazy import: this module is imported at app boot, but
        # SessionLocal needs the DB engine, which is built from
        # settings -- avoid the circular by deferring to call-time.
        from app.db.session import SessionLocal
        from app.models.admin import Admin

        db = SessionLocal()
        try:
            row = db.get(Admin, admin_id)
            if row is not None and row.tier in ALL_TIERS_V2:
                tier = row.tier
        finally:
            try:
                db.rollback()
            except Exception:
                pass
            db.close()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "tier-lookup failed for admin_id=%s falling back to free: %s",
            admin_id, exc,
        )
        tier = TIER_FREE

    _admin_tier_cache[admin_id] = (tier, now + _ADMIN_TIER_CACHE_TTL_SECONDS)
    return tier


def _reset_admin_tier_cache() -> None:
    """Test hook -- flush the in-process tier cache.

    Tests that mutate admin tiers in the same process need to
    invalidate the cache between assertions; production callers
    must NOT use this -- the 60s TTL is the only correct
    invalidation path so we never read stale across processes.
    """
    _admin_tier_cache.clear()

# Step 29.y Cluster 5 (B-1): correct env-var name. Pre-29.y this
# read os.getenv("REDISURL") (no underscore), which silently
# resolved to None in every environment that exports REDIS_URL --
# i.e. all of prod. SlowAPI then fell through to memory:// storage,
# making per-route rate limits per-process instead of shared, and
# every other component reads REDIS_URL with the underscore. The
# typo neutered our rate limits for the entire prod lifetime.
#
# Step 29.y close (D-redis-url-centralize-via-settings-2026-05-08):
# Read through `settings.redis_url` so this module shares the single
# source of truth defined in `app.core.config`. The empty-string
# fallback below preserves the prior behaviour where an unset
# REDIS_URL means "no shared backend, use in-memory storage" -- the
# Settings default is `redis://localhost:6379/0` for dev, but that
# default is ONLY appropriate when running locally. Prod ALWAYS
# injects REDIS_URL from SSM via the ECS task-def `secrets:` block,
# so prod never sees the localhost fallback. To force in-memory
# (e.g. unit tests), set REDIS_URL="" explicitly.
REDIS_URL = settings.redis_url or None

# Step 29.y Cluster 5 (B-1): hardened Redis pool. retry_on_timeout
# rides over single-RTT blips without raising; tight socket
# timeouts make the limiter fail FAST when the storage truly is
# unreachable so the fallback middleware can decide fail-open vs
# fail-closed (see WRITE_METHODS below) instead of stalling the
# request.
REDIS_SOCKET_CONNECT_TIMEOUT_S = float(
    os.getenv("RATE_LIMIT_REDIS_CONNECT_TIMEOUT", "1.5")
)
REDIS_SOCKET_TIMEOUT_S = float(
    os.getenv("RATE_LIMIT_REDIS_SOCKET_TIMEOUT", "1.5")
)
REDIS_HEALTH_CHECK_INTERVAL_S = int(
    os.getenv("RATE_LIMIT_REDIS_HEALTH_CHECK_INTERVAL", "30")
)

if REDIS_URL:
    storage_uri = REDIS_URL
    storage_options = {
        "socket_connect_timeout": REDIS_SOCKET_CONNECT_TIMEOUT_S,
        "socket_timeout": REDIS_SOCKET_TIMEOUT_S,
        "retry_on_timeout": True,
        "health_check_interval": REDIS_HEALTH_CHECK_INTERVAL_S,
    }
    storage_note = (
        f"Redis: {REDIS_URL} (timeouts {REDIS_SOCKET_TIMEOUT_S}s, "
        "retry_on_timeout=True)"
    )
else:
    storage_uri = "memory://"
    storage_options = {}
    storage_note = "In-memory local dev only, not shared across containers"

logger.info("Rate limit storage: %s", storage_note)


def _client_ip(request: Request) -> str:
    """Extract the best-available caller IP for anonymous buckets.

    Prefers the X-Forwarded-For first hop (ALB injects this), falls
    back to the direct client host, and finally to the literal
    ``unknown`` sentinel so the bucket key remains a well-defined
    string even when neither header is present (test client / unit
    tests).
    """
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        first = forwarded_for.split(",")[0].strip()
        if first:
            return first
    client = getattr(request, "client", None)
    if client and client.host:
        return client.host
    return "unknown"


def get_api_key_or_ip(request: Request) -> str:
    """Legacy key-func -- caller by raw API key, else by IP.

    Retained for the embed-widget path in ``app/api/v1/chat_widget.py``
    (its rate-limit is the per-embed-key ``rate_limit_per_minute``
    column, enforced separately from the tier matrix) and for any
    future caller that genuinely wants the raw-key bucket. All admin
    + chat routes have moved to :func:`get_tier_aware_key` below,
    which encodes tier + admin + instance into the bucket name so
    SlowAPI's limit-provider can recover the cap without an extra
    DB round-trip.
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        raw_key = auth_header.replace("Bearer ", "").strip()
        if raw_key:
            return raw_key

    x_api_key = request.headers.get("X-API-Key")
    if x_api_key:
        return x_api_key

    return _client_ip(request)


def get_tier_aware_key(request: Request) -> str:
    """Compose the tier-aware Redis bucket key for one request.

    Shape: ``tier:{tier}:admin:{admin_id}:inst:{instance_id|none}``
    when ``request.state.admin_id`` is populated by
    ``ApiKeyAuthMiddleware`` (or ``SessionCookieAuthMiddleware``).

    Shape: ``ip:{ip}`` when the request is unauthenticated -- this is
    the anonymous-bucket lane that gets the Free=30rpm cap from
    :func:`get_tier_rate_limit_for_key`. We deliberately do NOT fall
    back to the raw API key here even when ``Authorization: Bearer
    ...`` is present but state is unpopulated: that state-unpopulated
    code path means auth has not run yet, which means we have NO
    proof the key is valid -- bucketing by an unvalidated string
    would let a single attacker rotate forged keys to dodge the cap.
    The IP bucket is the strictly safer fallback.

    Per-instance isolation: an Enterprise admin running 50 Instances
    gets 50 independent 3000rpm buckets, not one shared 3000rpm cap.
    This matches the Option-A founder-lock that
    ``instance_count_cap=None`` means "unlimited" -- if all 50
    Instances shared a single cap, the seller's promise breaks the
    moment two Instances burst at the same time. Per-instance also
    closes the noisy-neighbour drift
    (D-pro-tier-rate-limit-abuse-surface-2026-05-23) called out in
    the Pro entitlement comment: one buggy Instance can no longer
    starve siblings under the same Admin.
    """
    state = getattr(request, "state", None)
    admin_id = getattr(state, "admin_id", None) if state is not None else None
    if not admin_id:
        return f"ip:{_client_ip(request)}"

    instance_id = getattr(state, "luciel_instance_id", None) if state is not None else None
    tier = _lookup_admin_tier(str(admin_id))
    inst_part = str(instance_id) if instance_id is not None else "none"
    return f"tier:{tier}:admin:{admin_id}:inst:{inst_part}"


def get_embed_key_aware_key(request: Request) -> str:
    """Compose the per-embed-key Redis bucket key for one widget request.

    Arc 8 Commit 3 (WU-3 abuse-surface) -- closes the per-key half of
    D-pro-tier-rate-limit-abuse-surface-2026-05-23.

    Shape: ``embed:tier:{tier}:admin:{admin_id}:key:{api_key_id}`` when
    the request carries a resolved embed-key on ``request.state``
    (populated by ``ApiKeyAuthMiddleware``). The widget endpoint's
    ``require_embed_key`` dependency has already asserted
    ``key_kind == 'embed'`` by the time the limiter consults this
    callable, but we re-check defensively here so an admin key that
    bypasses the dependency (impossible today, but cheap to guard)
    cannot land in the embed-key bucket.

    Shape: ``ip:{ip}`` when the request is unauthenticated -- the
    Free=30rpm anonymous lane, same as :func:`get_tier_aware_key`.
    We do NOT compose by raw Authorization header here for the same
    reason as :func:`get_tier_aware_key`: state-unpopulated means
    auth has not validated the key, and bucketing on an unvalidated
    string lets an attacker rotate forged keys to dodge the cap.

    Per-key isolation rationale: under the 2026-05-23 Option-A
    revision Pro carries 10 embed keys against a single 300rpm cap.
    Without a per-key bucket one leaked key can burn the whole
    allotment, starving the other 9 keys. The composition with
    :func:`get_embed_key_rate_limit_for_key` brings each key down to
    its derived ``per_key_api_rate_limit_rpm`` cap (Pro: 30rpm per
    key) while preserving the admin-level ceiling via the existing
    tier-aware admin bucket on admin-surface routes.
    """
    state = getattr(request, "state", None)
    admin_id = getattr(state, "admin_id", None) if state is not None else None
    key_kind = getattr(state, "key_kind", None) if state is not None else None
    api_key_id = getattr(state, "api_key_id", None) if state is not None else None

    # Only embed keys land in the per-key bucket. Admin keys reaching
    # this code path (shouldn't happen -- require_embed_key gates them
    # off) fall through to the IP bucket so they cannot inherit the
    # per-embed-key generous derivation.
    if not admin_id or key_kind != "embed" or not api_key_id:
        return f"ip:{_client_ip(request)}"

    tier = _lookup_admin_tier(str(admin_id))
    return f"embed:tier:{tier}:admin:{admin_id}:key:{api_key_id}"


def get_embed_key_rate_limit_for_key(key: str) -> str:
    """SlowAPI limit-provider for the per-embed-key bucket.

    Parses the ``embed:tier:{tier}:...`` prefix and returns the
    derived ``per_key_api_rate_limit_rpm`` cap. Anonymous (``ip:...``)
    and malformed keys fall back to Free per-key (30rpm), same
    fail-safe posture as :func:`get_tier_rate_limit_for_key`.
    """
    tier = TIER_FREE
    if isinstance(key, str) and key.startswith("embed:tier:"):
        # ``embed:tier:{tier}:admin:...`` -- third segment is the tier.
        parts = key.split(":", 3)
        if len(parts) >= 3 and parts[2] in ALL_TIERS_V2:
            tier = parts[2]

    try:
        rpm = per_key_api_rate_limit_rpm(tier=tier)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "per-key entitlement lookup failed for tier=%s falling back to free: %s",
            tier, exc,
        )
        rpm = per_key_api_rate_limit_rpm(tier=TIER_FREE)

    return f"{int(rpm)}/minute"


def _classify_bucket_scope(key: str) -> str:
    """Map a bucket key prefix back to a stable scope label.

    Used by :func:`rate_limit_exceeded_handler` to surface a
    ``bucket_scope`` field on the 429 response body so the client
    (and our own ops) can tell WHICH bucket emptied:

      * ``tier_admin_instance`` -- the per-(admin, instance) tier
        bucket built by :func:`get_tier_aware_key`. Hit when one
        Admin's combined load across one Instance exceeds the
        tier-aware cap.
      * ``embed_key`` -- the per-embed-key bucket built by
        :func:`get_embed_key_aware_key`. Hit when one embed key
        exceeds its derived ``per_key_api_rate_limit_rpm`` share.
      * ``ip`` -- the anonymous bucket. Hit when an unauthenticated
        caller burns Free=30rpm from a single IP.
      * ``unknown`` -- defensive default; never returned in practice
        because every key written by this module starts with one of
        the three prefixes above.
    """
    if not isinstance(key, str):
        return "unknown"
    if key.startswith("embed:tier:"):
        return "embed_key"
    if key.startswith("tier:"):
        return "tier_admin_instance"
    if key.startswith("ip:"):
        return "ip"
    return "unknown"


def get_tier_rate_limit_for_key(key: str) -> str:
    """SlowAPI limit-provider -- compute the per-minute cap from the key.

    The key string is whatever :func:`get_tier_aware_key` returned
    for this request. We parse the ``tier:`` prefix back out so we
    don't pay another DB hit; the cap value comes straight from
    :func:`resolve_entitlement` on the ``api_rate_limit_rpm`` axis,
    which means a future change to the founder-locked numbers (e.g.
    a Pro upsell) automatically propagates here with no edit to this
    file.

    Anonymous bucket (``ip:...`` keys) falls through to Free=30rpm.
    Malformed keys (shouldn't happen, but defence in depth) also map
    to Free=30rpm rather than raising -- a 429 is a worse user
    experience than a 500, but a 500 from the limit-provider is a
    catastrophic outage because every authenticated request burns on
    the same code path.
    """
    tier = TIER_FREE
    if isinstance(key, str) and key.startswith("tier:"):
        # ``tier:{tier}:admin:...`` -- second segment is the tier.
        parts = key.split(":", 2)
        if len(parts) >= 2 and parts[1] in ALL_TIERS_V2:
            tier = parts[1]

    try:
        rpm = resolve_entitlement(tier=tier, axis="api_rate_limit_rpm")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "entitlement lookup failed for tier=%s falling back to free: %s",
            tier, exc,
        )
        rpm = resolve_entitlement(tier=TIER_FREE, axis="api_rate_limit_rpm")

    return f"{int(rpm)}/minute"


# Step 29.y Cluster 5 (B-1): in_memory_fallback_enabled lets SlowAPI
# transparently fall back to per-process memory storage when the
# primary Redis backend dies. This is the FAIL-OPEN path for reads
# -- the limiter keeps working at degraded fidelity (per-process
# instead of cluster-wide) rather than 500ing every request. Writes
# are handled separately by the fallback middleware below, which
# returns 503 so write quota integrity is preserved.
# Arc 7 Commit 4 (WU-2): the limiter's default key-func is now the
# tier-aware composer. Routes that pass an explicit ``key_func=...``
# to ``@limiter.limit(...)`` (currently only the embed-widget path
# in chat_widget.py, which uses ``get_api_key_or_ip``) keep their
# bucket shape. The default-limits string remains the pre-Arc-7
# 60/minute floor and applies ONLY to routes with no explicit
# ``@limiter.limit`` decorator -- there are none on the admin/chat
# surfaces today, but the value is kept conservative as a defence
# in case a future route gets added without an explicit decorator.
limiter = Limiter(
    key_func=get_tier_aware_key,
    default_limits=["60/minute"],
    storage_uri=storage_uri,
    storage_options=storage_options,
    in_memory_fallback_enabled=True,
)

# Pre-Arc-7 static rate-limit constants RETIRED at Arc 7 Commit 4
# (2026-05-24). Every admin + chat route now decorates with
# ``@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)``
# which resolves the cap from the founder-locked
# ``api_rate_limit_rpm`` axis (Free=30, Pro=300, Enterprise=3000)
# at request time. Path A doctrine forbids keeping the fixed
# strings as aliases -- a stale constant is a foot-gun the next
# time someone decorates a new route.
#
#   CHAT_RATE_LIMIT             (was "20/minute")  -> retired
#   KNOWLEDGE_UPLOAD_RATE_LIMIT (was "10/minute")  -> retired
#   ADMIN_RATE_LIMIT            (was "30/minute")  -> retired
#
# The widget surface keeps its own ``EMBED_WIDGET_RATE_LIMIT`` in
# ``app/api/widget_deps.py`` because that path enforces the
# per-embed-key ``rate_limit_per_minute`` column, which is a
# different abstraction (admin sets the cap when they mint the
# embed key) and is not driven by the tier matrix.

# Step 29.y Cluster 5 (B-1): write methods fail CLOSED when the
# rate-limit backend bubbles an exception that escapes the
# in_memory_fallback. A 503 with Retry-After lets the ALB route the
# request to a healthy task or surface a clean retryable error to
# the client. Read methods fail OPEN -- the in-memory fallback in
# the Limiter handles them, and any exception that still escapes is
# allowed to surface (we do not silently turn it into a 200).
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_last_fallback_warning = 0.0
FALLBACK_LOG_INTERVAL_SECONDS = 60


def rate_limit_exceeded_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Clean JSON response when a request exceeds its configured limit.

    Arc 8 Commit 3 (WU-3) surfaces a stable ``bucket_scope`` field so
    clients (and ops dashboards) can distinguish which bucket fired
    the 429:

      * ``tier_admin_instance`` -- per-(admin, instance) tier bucket
        (admin/chat surface).
      * ``embed_key`` -- per-embed-key bucket (widget surface).
      * ``ip`` -- anonymous lane.
      * ``unknown`` -- defensive default.

    Recovering the scope: SlowAPI raises a ``RateLimitExceeded``
    exception that exposes the offending ``limit`` whose
    ``key_func(request)`` we can call to recover the bucket key. We
    fall back through the two known key-funcs (tier-aware first,
    then embed-key-aware) so we still classify cleanly even if
    SlowAPI's internal exception shape changes.
    """
    detail = getattr(exc, "detail", str(exc))

    bucket_scope = "unknown"
    try:
        limit = getattr(exc, "limit", None)
        # SlowAPI's exception sometimes nests the LimitGroup as
        # ``limit.limit`` (newer slowapi) or directly as ``limit``.
        key_func = getattr(limit, "key_func", None) or getattr(
            getattr(limit, "limit", None), "key_func", None
        )
        if callable(key_func):
            try:
                bucket_key = key_func(request)
                bucket_scope = _classify_bucket_scope(bucket_key)
            except Exception:
                bucket_scope = "unknown"
        else:
            # Fallback: try both known key-funcs. The embed-key one
            # only returns a non-IP shape when state.key_kind ==
            # 'embed', so a chat/admin request flows to the tier
            # bucket and a widget request to the embed-key bucket.
            try:
                bucket_key = get_embed_key_aware_key(request)
                if bucket_key.startswith("embed:tier:"):
                    bucket_scope = "embed_key"
                else:
                    bucket_key = get_tier_aware_key(request)
                    bucket_scope = _classify_bucket_scope(bucket_key)
            except Exception:
                bucket_scope = "unknown"
    except Exception:  # pragma: no cover - defensive
        bucket_scope = "unknown"

    return JSONResponse(
        status_code=429,
        content={
            "error": "rate_limit_exceeded",
            "detail": str(detail),
            "message": "You have exceeded the allowed request rate. Please wait and try again.",
            "bucket_scope": bucket_scope,
        },
    )


def _is_rate_limit_backend_error(exc: BaseException) -> bool:
    """
    Heuristic: was this exception raised by the rate-limit storage
    backend (Redis) rather than the application itself? Substring
    match on the exception text catches the redis-py exception
    families (ConnectionError, TimeoutError, BusyLoadingError) as
    well as slowapi/limits storage errors without forcing a hard
    import dependency on the exception classes.

    The match is intentionally narrow: "redis" or specific
    connection-failure phrases. Generic words like "timeout" alone
    are not enough -- a route handler raising ValueError("request
    timed out for user") would otherwise be misclassified as a
    backend error.
    """
    error_text = str(exc).lower()
    # First try the strong signal: the literal token "redis".
    if "redis" in error_text:
        return True
    # Then phrases that, taken together with the exception class
    # names redis-py raises, are the clear backend-failure surface.
    backend_phrases = (
        "connection refused",
        "connection reset",
        "connectionerror",
        "broken pipe",
        "no connection available",
    )
    return any(phrase in error_text for phrase in backend_phrases)


def create_rate_limit_middleware():
    """
    Return middleware that handles rate-limit storage outages.

    Step 29.y Cluster 5 (B-1) split fail-modes:
      * write methods (POST/PUT/PATCH/DELETE): fail CLOSED -> 503,
        so the caller backs off and the ALB routes around the box.
        Quota integrity matters more than availability for writes
        because writes mutate state.
      * read methods (GET/HEAD/OPTIONS/etc.): the SlowAPI
        in_memory_fallback handles the fall-through. Anything that
        still escapes is re-raised so it surfaces as a real error
        rather than being silently swallowed.

    Pre-29.y this middleware tried to fail-open by re-calling
    call_next() with the limiter disabled. That path is broken in
    modern Starlette because BaseHTTPMiddleware streams cannot be
    re-consumed within a single request; the retried call_next
    silently produced a 500. The new posture relies on the
    in_memory_fallback in the Limiter for the read fail-open and
    only intervenes for writes.
    """
    from starlette.middleware.base import BaseHTTPMiddleware

    class RateLimitFallbackMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            global _last_fallback_warning

            try:
                return await call_next(request)
            except Exception as exc:
                if not _is_rate_limit_backend_error(exc):
                    raise

                now = time.time()
                if now - _last_fallback_warning >= FALLBACK_LOG_INTERVAL_SECONDS:
                    logger.warning(
                        "Rate-limit backend unavailable. method=%s path=%s err=%s",
                        request.method,
                        request.url.path,
                        exc,
                    )
                    _last_fallback_warning = now

                method = (request.method or "GET").upper()
                if method in WRITE_METHODS:
                    # Fail closed for writes. 503 + Retry-After so
                    # the client and the ALB both treat this as
                    # transient and route around / back off.
                    return JSONResponse(
                        status_code=503,
                        headers={"Retry-After": "5"},
                        content={
                            "error": "rate_limit_backend_unavailable",
                            "detail": (
                                "Rate-limit storage is temporarily "
                                "unreachable. Write requests are "
                                "rejected to preserve quota integrity. "
                                "Please retry shortly."
                            ),
                        },
                    )

                # Read path: re-raise so the caller sees the real
                # error. The in_memory_fallback in the Limiter is
                # supposed to have caught the storage death before
                # we got here; if it did not, we do NOT silently
                # mask it as a 200.
                raise

    return RateLimitFallbackMiddleware
