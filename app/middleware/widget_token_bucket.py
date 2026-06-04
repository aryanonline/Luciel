"""Widget token-bucket rate limiter — §3.1.5 abuse controls.

RESCAN TIER-DE(widget+export): replaces the per-embed-key fixed-rpm
SlowAPI limiter for the widget chat path with a Redis TOKEN-BUCKET that
enforces:

  * Per-SESSION bucket: burst capacity 5, refill 1 token / 3 s.
    Keyed by session_id when present; falls back to embed-key ID so a
    session-less first turn shares a bucket with its follow-ups.

  * Per-IP bucket: burst capacity 20, refill 1 token / 3 s.  Acts as an
    independent outer guard so a single IP launching many sessions
    cannot trivially bypass the per-session cap.

  * Auto-block: when a source (session or IP) exhausts its bucket and
    continues hammering (sustained abuse counter), a Redis marker is
    written for a cooldown window and a widget_abuse_blocked audit event
    is emitted.

This check runs BEFORE the budget gate and BEFORE the LLM path so
abuse-traffic never increments the tenant's conversation budget or
reaches the model.  The existing per-tenant rpm ceiling (§4.7, enforced
by SlowAPI on the outer Limiter) is preserved as an additional outer
bound — we ADD this as the per-session inner control.

Design: uses a standard token-bucket Lua approach on Redis (EVALSHA /
EVAL for atomic compare-and-set).  Falls back to fail-open (allows the
request) when Redis is unavailable — same posture as the existing budget
meter, where losing Redis degraded-serves rather than hard-blocks widget
visitors.

Redis key shapes
----------------
  luciel:wtb:sess:{session_key}        — per-session token bucket
  luciel:wtb:ip:{ip}                   — per-IP token bucket
  luciel:wtb:abuse:sess:{session_key}  — per-session abuse counter
  luciel:wtb:abuse:ip:{ip}             — per-IP abuse counter
  luciel:wtb:block:sess:{session_key}  — per-session block marker (exists = blocked)
  luciel:wtb:block:ip:{ip}             — per-IP block marker (exists = blocked)

All keys expire automatically; no background purge needed.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("luciel.widget_token_bucket")

# -----------------------------------------------------------------------
# Tunables (platform-managed, tier-uniform per §3.1.5).
# -----------------------------------------------------------------------

# Per-session bucket.
_SESSION_BURST = 5          # tokens in a fresh bucket
_SESSION_REFILL_EVERY_S = 3  # +1 token every N seconds
_SESSION_BUCKET_TTL_S = 3600  # bucket key TTL (1h — covers a conversation)

# Per-IP bucket (coarser, higher burst — guards against IP-level floods).
_IP_BURST = 20
_IP_REFILL_EVERY_S = 3
_IP_BUCKET_TTL_S = 3600

# Abuse auto-block parameters.
_ABUSE_THRESHOLD = 10       # consecutive rejections before auto-block
_ABUSE_COUNTER_TTL_S = 120  # how long the abuse counter lives
_BLOCK_COOLDOWN_S = 300     # block duration (5 min)


# -----------------------------------------------------------------------
# Lua token-bucket script.
# -----------------------------------------------------------------------
# Arguments (KEYS[1]=bucket_key, ARGV[1]=burst, ARGV[2]=refill_every_s,
#             ARGV[3]=ttl_s, ARGV[4]=now_int):
#   Returns 1  → token consumed (allowed)
#   Returns 0  → no tokens left (rejected)
#
# Bucket value format: "{tokens}:{last_refill_ts}"
# Stored as a plain Redis string; atomic via EVAL single-key.
_LUA_TOKEN_BUCKET = """
local key       = KEYS[1]
local burst     = tonumber(ARGV[1])
local refill_s  = tonumber(ARGV[2])
local ttl_s     = tonumber(ARGV[3])
local now       = tonumber(ARGV[4])

local raw = redis.call('GET', key)
local tokens, last_ts

if raw == false then
    tokens  = burst
    last_ts = now
else
    local sep = string.find(raw, ':')
    tokens  = tonumber(string.sub(raw, 1, sep - 1))
    last_ts = tonumber(string.sub(raw, sep + 1))
end

-- Refill based on elapsed time.
local elapsed = now - last_ts
if elapsed > 0 then
    local added = math.floor(elapsed / refill_s)
    tokens  = math.min(burst, tokens + added)
    last_ts = last_ts + added * refill_s
end

if tokens > 0 then
    tokens = tokens - 1
    redis.call('SET', key, tokens .. ':' .. last_ts, 'EX', ttl_s)
    return 1
else
    redis.call('SET', key, '0:' .. last_ts, 'EX', ttl_s)
    return 0
end
"""

# SHA digest is registered lazily on first use; avoids startup failures
# when Redis is down.
_LUA_SHA: Optional[str] = None


def _eval_bucket(redis_client, key: str, burst: int, refill_s: int, ttl_s: int) -> bool:
    """Run the token-bucket script atomically.

    Returns True  → token consumed, request is allowed.
    Returns False → bucket empty, request is rejected.
    Raises if Redis is unavailable (caller should catch and fail-open).
    """
    global _LUA_SHA
    now = int(time.time())
    args = [burst, refill_s, ttl_s, now]

    if _LUA_SHA is None:
        _LUA_SHA = redis_client.script_load(_LUA_TOKEN_BUCKET)

    try:
        result = redis_client.evalsha(_LUA_SHA, 1, key, *args)
    except Exception:
        # SHA may have been flushed (SCRIPT FLUSH); reload once.
        _LUA_SHA = redis_client.script_load(_LUA_TOKEN_BUCKET)
        result = redis_client.evalsha(_LUA_SHA, 1, key, *args)

    return bool(result)


# -----------------------------------------------------------------------
# Result type.
# -----------------------------------------------------------------------

@dataclass
class BucketResult:
    allowed: bool
    source: str          # "session" | "ip"
    source_key: str      # the specific session/ip value
    abuse_blocked: bool  # True when auto-block fired THIS request


# -----------------------------------------------------------------------
# Public API.
# -----------------------------------------------------------------------

def check_widget_request(
    *,
    redis_client,
    session_key: Optional[str],   # session_id or embed-key fingerprint
    client_ip: str,
    admin_id: Optional[str] = None,
    instance_id: Optional[str] = None,
    audit_repository=None,
    audit_ctx=None,
) -> BucketResult:
    """Evaluate both the per-session and per-IP token buckets.

    Called at the widget edge BEFORE the budget gate.

    Returns a BucketResult. When allowed=False, the caller MUST return
    a 429 (or 403 if abuse_blocked=True) without touching the budget
    counter or the LLM path.

    When Redis is unavailable the call fails OPEN (returns allowed=True)
    to avoid a Redis outage hard-blocking all widget traffic. This is
    the same degraded-serve posture as budget_meter.
    """
    if redis_client is None:
        # No Redis configured — fail open.
        return BucketResult(
            allowed=True, source="session",
            source_key=session_key or "unknown", abuse_blocked=False
        )

    try:
        return _check(
            redis_client=redis_client,
            session_key=session_key,
            client_ip=client_ip,
            admin_id=admin_id,
            instance_id=instance_id,
            audit_repository=audit_repository,
            audit_ctx=audit_ctx,
        )
    except Exception as exc:
        logger.warning(
            "widget_token_bucket: Redis error — failing open. err=%s", exc
        )
        return BucketResult(
            allowed=True, source="session",
            source_key=session_key or "unknown", abuse_blocked=False
        )


def _check(
    *,
    redis_client,
    session_key: Optional[str],
    client_ip: str,
    admin_id: Optional[str],
    instance_id: Optional[str],
    audit_repository,
    audit_ctx,
) -> BucketResult:
    """Inner implementation — may raise on Redis errors (caller catches)."""
    sess_key_part = session_key or f"nokey:{client_ip}"

    # Redis bucket key names.
    r_sess_bucket  = f"luciel:wtb:sess:{sess_key_part}"
    r_ip_bucket    = f"luciel:wtb:ip:{client_ip}"
    r_sess_abuse   = f"luciel:wtb:abuse:sess:{sess_key_part}"
    r_ip_abuse     = f"luciel:wtb:abuse:ip:{client_ip}"
    r_sess_block   = f"luciel:wtb:block:sess:{sess_key_part}"
    r_ip_block     = f"luciel:wtb:block:ip:{client_ip}"

    # ---- Check existing block markers first ----
    if redis_client.exists(r_sess_block):
        return BucketResult(
            allowed=False, source="session",
            source_key=sess_key_part, abuse_blocked=False,
        )
    if redis_client.exists(r_ip_block):
        return BucketResult(
            allowed=False, source="ip",
            source_key=client_ip, abuse_blocked=False,
        )

    # ---- Evaluate per-session bucket ----
    sess_ok = _eval_bucket(
        redis_client, r_sess_bucket,
        _SESSION_BURST, _SESSION_REFILL_EVERY_S, _SESSION_BUCKET_TTL_S,
    )

    if not sess_ok:
        abuse_blocked = _handle_abuse(
            redis_client=redis_client,
            abuse_counter_key=r_sess_abuse,
            block_key=r_sess_block,
            source="session",
            source_key=sess_key_part,
            admin_id=admin_id,
            instance_id=instance_id,
            audit_repository=audit_repository,
            audit_ctx=audit_ctx,
        )
        return BucketResult(
            allowed=False, source="session",
            source_key=sess_key_part, abuse_blocked=abuse_blocked,
        )

    # ---- Evaluate per-IP bucket ----
    ip_ok = _eval_bucket(
        redis_client, r_ip_bucket,
        _IP_BURST, _IP_REFILL_EVERY_S, _IP_BUCKET_TTL_S,
    )

    if not ip_ok:
        abuse_blocked = _handle_abuse(
            redis_client=redis_client,
            abuse_counter_key=r_ip_abuse,
            block_key=r_ip_block,
            source="ip",
            source_key=client_ip,
            admin_id=admin_id,
            instance_id=instance_id,
            audit_repository=audit_repository,
            audit_ctx=audit_ctx,
        )
        return BucketResult(
            allowed=False, source="ip",
            source_key=client_ip, abuse_blocked=abuse_blocked,
        )

    # Both buckets have tokens — allow.
    # Reset abuse counters on a clean pass (sustained-abuse window
    # resets when the requester backs off and refills a token).
    try:
        redis_client.delete(r_sess_abuse)
        redis_client.delete(r_ip_abuse)
    except Exception:
        pass  # non-fatal

    return BucketResult(
        allowed=True, source="session",
        source_key=sess_key_part, abuse_blocked=False,
    )


def _handle_abuse(
    *,
    redis_client,
    abuse_counter_key: str,
    block_key: str,
    source: str,
    source_key: str,
    admin_id: Optional[str],
    instance_id: Optional[str],
    audit_repository,
    audit_ctx,
) -> bool:
    """Increment the abuse counter; auto-block when threshold is crossed.

    Returns True if the block was just applied this call.
    """
    try:
        pipe = redis_client.pipeline(transaction=False)
        pipe.incr(abuse_counter_key)
        pipe.expire(abuse_counter_key, _ABUSE_COUNTER_TTL_S)
        results = pipe.execute()
        abuse_count = int(results[0])
    except Exception:
        return False

    if abuse_count >= _ABUSE_THRESHOLD:
        try:
            # SET NX so concurrent workers don't double-emit the audit row.
            blocked = redis_client.set(
                block_key, "1", ex=_BLOCK_COOLDOWN_S, nx=True
            )
            if blocked:
                logger.warning(
                    "widget_abuse_blocked: source=%s source_key=%s "
                    "abuse_count=%d admin_id=%s instance_id=%s",
                    source, source_key, abuse_count, admin_id, instance_id,
                )
                _emit_abuse_audit(
                    audit_repository=audit_repository,
                    audit_ctx=audit_ctx,
                    admin_id=admin_id,
                    instance_id=instance_id,
                    source=source,
                    source_key=source_key,
                    abuse_count=abuse_count,
                )
                return True
        except Exception as exc:
            logger.warning(
                "widget_token_bucket: abuse block write failed: %s", exc
            )
    return False


def _emit_abuse_audit(
    *,
    audit_repository,
    audit_ctx,
    admin_id: Optional[str],
    instance_id: Optional[str],
    source: str,
    source_key: str,
    abuse_count: int,
) -> None:
    """Write the widget_abuse_blocked audit event.

    Fails silently — an audit write failure must never block the 429
    response path.
    """
    if audit_repository is None or audit_ctx is None or admin_id is None:
        # No audit context available (anonymous traffic or test). Log
        # the event at WARNING but don't crash.
        logger.warning(
            "widget_abuse_blocked audit skipped (no context): "
            "source=%s source_key=%s", source, source_key
        )
        return

    try:
        from app.models.admin_audit_log import (
            ACTION_WIDGET_ABUSE_BLOCKED,
            RESOURCE_SESSION,
        )
        audit_repository.record(
            ctx=audit_ctx,
            admin_id=admin_id,
            action=ACTION_WIDGET_ABUSE_BLOCKED,
            resource_type=RESOURCE_SESSION,
            resource_natural_id=source_key,
            after={
                "source": source,
                "source_key": source_key,
                "abuse_count": abuse_count,
                "instance_id": instance_id,
                "block_cooldown_seconds": _BLOCK_COOLDOWN_S,
            },
            note=(
                f"Widget abuse auto-block: {source}={source_key} "
                f"({abuse_count} consecutive rejections)."
            ),
            autocommit=True,
        )
    except Exception as exc:
        logger.warning(
            "widget_abuse_blocked audit write failed: %s", exc
        )
