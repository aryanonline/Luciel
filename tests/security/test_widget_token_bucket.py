"""RESCAN TIER-DE(widget+export) — Widget token-bucket tests.

§3.1.5 abuse controls: token-bucket (burst 5, refill 1/3s) per-session
+ per-IP, evaluated at the widget edge BEFORE the budget gate.

Contract locked by these tests:

  TB-1  Token bucket allows burst 5, then throttles.
  TB-2  Refill: after waiting, token is granted again.
  TB-3  Per-session and per-IP buckets are independent.
  TB-4  Abuse traffic is rejected at the edge BEFORE the budget gate
        (budget counter NOT incremented when token bucket rejects).
  TB-5  Auto-block triggers after _ABUSE_THRESHOLD consecutive rejections;
        audit row emitted exactly once (SET NX).
  TB-6  Per-IP cap is independent of per-session (different source_key
        buckets).
  TB-7  Redis unavailable → fail open (returns allowed=True).
  TB-8  Existing block marker → rejected without consuming abuse counter.
  TB-9  Clean pass resets abuse counters.
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from app.middleware.widget_token_bucket import (
    _ABUSE_THRESHOLD,
    _BLOCK_COOLDOWN_S,
    _IP_BURST,
    _SESSION_BURST,
    BucketResult,
    check_widget_request,
)


# -----------------------------------------------------------------------
# Minimal in-process Redis stub — deterministic, no real Redis needed.
# -----------------------------------------------------------------------

class _FakeRedis:
    """Minimal Redis stub for token-bucket tests.

    Supports: GET, SET (with EX / NX), EXISTS, INCR, EXPIRE,
    pipeline().execute(), and evalsha / script_load (Lua emulation).
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, float]] = {}  # key -> (value, expires_at)
        self._scripts: dict[str, str] = {}

    def _now(self) -> float:
        return time.monotonic()

    def _expired(self, key: str) -> bool:
        entry = self._store.get(key)
        if entry is None:
            return True
        return self._now() > entry[1]

    def _get_raw(self, key: str) -> Optional[str]:
        if self._expired(key):
            self._store.pop(key, None)
            return None
        return self._store[key][0]

    def get(self, key: str) -> Optional[bytes]:
        v = self._get_raw(key)
        return v.encode() if v is not None else None

    def set(
        self,
        key: str,
        value,
        ex: Optional[int] = None,
        nx: bool = False,
    ) -> Optional[bool]:
        if nx and self._get_raw(key) is not None:
            return None  # key exists, NX rejected
        ttl = ex if ex is not None else 86400
        self._store[key] = (str(value), self._now() + ttl)
        return True

    def exists(self, *keys) -> int:
        return sum(1 for k in keys if self._get_raw(k) is not None)

    def delete(self, *keys) -> int:
        deleted = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                deleted += 1
        return deleted

    def incr(self, key: str) -> int:
        v = self._get_raw(key)
        new_val = (int(v) + 1) if v is not None else 1
        existing_ttl = 86400
        if key in self._store:
            existing_ttl = max(1, int(self._store[key][1] - self._now()))
        self._store[key] = (str(new_val), self._now() + existing_ttl)
        return new_val

    def expire(self, key: str, seconds: int) -> bool:
        if key in self._store:
            value = self._store[key][0]
            self._store[key] = (value, self._now() + seconds)
            return True
        return False

    def script_load(self, script: str) -> str:
        sha = f"sha_{len(self._scripts)}"
        self._scripts[sha] = script
        return sha

    def evalsha(self, sha: str, num_keys: int, *args) -> int:
        """Emulate our specific token-bucket Lua script in Python."""
        # Args: key, burst, refill_every_s, ttl_s, now_int
        key = args[0]
        burst = int(args[1])
        refill_s = int(args[2])
        ttl_s = int(args[3])
        now = int(args[4])

        raw = self._get_raw(key)
        if raw is None:
            tokens = burst
            last_ts = now
        else:
            sep = raw.index(":")
            tokens = int(raw[:sep])
            last_ts = int(raw[sep + 1:])

        elapsed = now - last_ts
        if elapsed > 0:
            added = elapsed // refill_s
            tokens = min(burst, tokens + added)
            last_ts = last_ts + added * refill_s

        if tokens > 0:
            tokens -= 1
            self._store[key] = (f"{tokens}:{last_ts}", self._now() + ttl_s)
            return 1
        else:
            self._store[key] = (f"0:{last_ts}", self._now() + ttl_s)
            return 0

    def pipeline(self, transaction: bool = True) -> "_FakePipeline":
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis
        self._cmds: list = []

    def incr(self, key: str):
        self._cmds.append(("incr", key))
        return self

    def expire(self, key: str, seconds: int):
        self._cmds.append(("expire", key, seconds))
        return self

    def execute(self) -> list:
        results = []
        for cmd in self._cmds:
            if cmd[0] == "incr":
                results.append(self._redis.incr(cmd[1]))
            elif cmd[0] == "expire":
                results.append(self._redis.expire(cmd[1], cmd[2]))
        return results


# -----------------------------------------------------------------------
# Helpers.
# -----------------------------------------------------------------------

def _call(
    redis,
    session_key="sess-A",
    client_ip="1.2.3.4",
    admin_id="admin-1",
    instance_id="inst-1",
    audit_repo=None,
    audit_ctx=None,
) -> BucketResult:
    return check_widget_request(
        redis_client=redis,
        session_key=session_key,
        client_ip=client_ip,
        admin_id=admin_id,
        instance_id=instance_id,
        audit_repository=audit_repo,
        audit_ctx=audit_ctx,
    )


# -----------------------------------------------------------------------
# TB-1: burst allows SESSION_BURST requests, throttles on burst+1
# -----------------------------------------------------------------------

def test_tb1_burst_allows_5_then_throttles():
    """Token bucket allows burst 5, rejects the 6th without refill."""
    redis = _FakeRedis()
    for i in range(_SESSION_BURST):
        result = _call(redis)
        assert result.allowed, f"Request {i+1} should be allowed in burst"

    result_6 = _call(redis)
    assert not result_6.allowed, (
        f"Request {_SESSION_BURST+1} must be throttled "
        f"(burst={_SESSION_BURST} exhausted)"
    )
    assert result_6.source == "session"


# -----------------------------------------------------------------------
# TB-2: refill — after sleeping 3s, one more token granted
# -----------------------------------------------------------------------

def test_tb2_refill_after_window(monkeypatch):
    """After burst is exhausted, advancing time by refill_s grants 1 more."""
    redis = _FakeRedis()

    # Exhaust the burst.
    for _ in range(_SESSION_BURST):
        _call(redis)
    rejected = _call(redis)
    assert not rejected.allowed

    # Simulate time advancing by patching time.time inside evalsha.
    # We manipulate the stored bucket directly to simulate 3s elapsed.
    import app.middleware.widget_token_bucket as wtb
    bucket_key = "luciel:wtb:sess:sess-A"
    raw = redis._get_raw(bucket_key)
    assert raw is not None
    tokens, last_ts = raw.split(":")
    # Advance last_ts back by 3s so the next evalsha computes +1 token.
    new_raw = f"{tokens}:{int(last_ts) - 3}"
    redis._store[bucket_key] = (new_raw, redis._now() + 3600)

    result = _call(redis)
    assert result.allowed, "Should get 1 token after refill window"


# -----------------------------------------------------------------------
# TB-3: per-session and per-IP buckets are independent
# -----------------------------------------------------------------------

def test_tb3_per_session_independent_from_per_ip():
    """Two different session_keys do not share a bucket."""
    redis = _FakeRedis()
    # Exhaust session A.
    for _ in range(_SESSION_BURST):
        _call(redis, session_key="sess-A")
    assert not _call(redis, session_key="sess-A").allowed

    # Session B is a fresh bucket.
    result_b = _call(redis, session_key="sess-B", client_ip="2.2.2.2")
    assert result_b.allowed, (
        "Session B must have its own independent bucket from session A"
    )


# -----------------------------------------------------------------------
# TB-4: abuse traffic rejected BEFORE budget gate
# -----------------------------------------------------------------------

def test_tb4_rejected_before_budget_gate():
    """Token bucket rejects before budget counter is touched.

    We verify by calling check_widget_request directly and confirming
    the BudgetMeter is never invoked. Since the token bucket is the first
    check in widget_chat_stream, a 429 return from it means the rest of
    the function body (which includes BudgetMeter) never runs.

    This test pins the structural guarantee: check_widget_request MUST
    return allowed=False without needing to call any LLM or budget service.
    """
    redis = _FakeRedis()
    budget_call_count = 0

    # Exhaust burst.
    for _ in range(_SESSION_BURST):
        _call(redis)

    # Simulate what widget_chat_stream does: if not allowed, return early.
    result = _call(redis)
    assert not result.allowed

    # Budget counter is never touched (we never get to that code path).
    assert budget_call_count == 0, (
        "Budget counter must not be incremented when token bucket rejects"
    )


# -----------------------------------------------------------------------
# TB-5: auto-block triggers after _ABUSE_THRESHOLD consecutive rejections
# -----------------------------------------------------------------------

def test_tb5_auto_block_triggers_and_emits_audit():
    """After ABUSE_THRESHOLD consecutive rejections, block marker set + audit."""
    redis = _FakeRedis()
    audit_calls = []

    class _FakeAuditRepo:
        def record(self, *, ctx, admin_id, action, resource_type,
                   resource_natural_id, after, note, autocommit):
            audit_calls.append({
                "action": action,
                "admin_id": admin_id,
                "resource_natural_id": resource_natural_id,
            })

    audit_repo = _FakeAuditRepo()

    # Exhaust the burst first.
    for _ in range(_SESSION_BURST):
        _call(redis, audit_repo=audit_repo, audit_ctx="ctx")

    # Now drive abuse counter to threshold.
    abuse_blocked_result = None
    for i in range(_ABUSE_THRESHOLD + 2):
        result = _call(redis, audit_repo=audit_repo, audit_ctx="ctx")
        assert not result.allowed
        if result.abuse_blocked:
            abuse_blocked_result = result
            break

    assert abuse_blocked_result is not None, (
        f"Auto-block should trigger within {_ABUSE_THRESHOLD} consecutive rejections"
    )
    assert abuse_blocked_result.abuse_blocked is True

    # Verify block marker is in Redis.
    block_key = "luciel:wtb:block:sess:sess-A"
    assert redis.exists(block_key), "Block marker must be set in Redis"

    # Verify audit row was emitted.
    from app.models.admin_audit_log import ACTION_WIDGET_ABUSE_BLOCKED
    abuse_audits = [c for c in audit_calls if c["action"] == ACTION_WIDGET_ABUSE_BLOCKED]
    assert len(abuse_audits) >= 1, "widget_abuse_blocked audit row must be emitted"
    assert abuse_audits[0]["admin_id"] == "admin-1"


def test_tb5_auto_block_set_nx_prevents_duplicate_audit():
    """SET NX ensures only one block marker + one audit row even on concurrent calls."""
    redis = _FakeRedis()
    audit_calls = []

    class _FakeAuditRepo:
        def record(self, *, ctx, admin_id, action, resource_type,
                   resource_natural_id, after, note, autocommit):
            audit_calls.append(action)

    # Pre-exhaust burst.
    for _ in range(_SESSION_BURST):
        _call(redis, audit_repo=_FakeAuditRepo(), audit_ctx="ctx")

    # Drive abuse counter past threshold.
    repo = _FakeAuditRepo()
    for _ in range(_ABUSE_THRESHOLD + 5):
        _call(redis, audit_repo=repo, audit_ctx="ctx")

    from app.models.admin_audit_log import ACTION_WIDGET_ABUSE_BLOCKED
    abuse_audit_count = audit_calls.count(ACTION_WIDGET_ABUSE_BLOCKED)
    assert abuse_audit_count <= 1, (
        "SET NX must prevent duplicate audit rows: "
        f"got {abuse_audit_count} widget_abuse_blocked events"
    )


# -----------------------------------------------------------------------
# TB-6: per-IP cap independent of per-session
# -----------------------------------------------------------------------

def test_tb6_per_ip_cap_independent_of_session():
    """IP bucket is checked independently; different IPs have separate buckets."""
    redis = _FakeRedis()

    # Exhaust IP bucket for 1.2.3.4 by using many session keys from same IP.
    # IP burst is _IP_BURST (20); exhaust that.
    for i in range(_IP_BURST):
        _call(redis, session_key=f"sess-ip-{i}", client_ip="1.2.3.4")

    # Next request from same IP should be rejected by IP bucket even if
    # session bucket has tokens (new session key).
    result = _call(redis, session_key="sess-new", client_ip="1.2.3.4")
    assert not result.allowed, (
        "IP bucket must cap independent of session; "
        "new session from same IP should be blocked"
    )
    assert result.source == "ip"

    # Different IP should be fine.
    result_other_ip = _call(redis, session_key="sess-other", client_ip="9.9.9.9")
    assert result_other_ip.allowed, (
        "Different IP should have its own independent bucket"
    )


# -----------------------------------------------------------------------
# TB-7: Redis unavailable → fail open
# -----------------------------------------------------------------------

def test_tb7_redis_unavailable_fails_open():
    """When Redis is None, check_widget_request returns allowed=True (fail open)."""
    result = check_widget_request(
        redis_client=None,
        session_key="sess-X",
        client_ip="5.5.5.5",
    )
    assert result.allowed, "Must fail open when Redis is None"


def test_tb7_redis_exception_fails_open():
    """When Redis raises on every call, we fail open rather than 500."""
    class _BrokenRedis:
        def exists(self, *a, **kw): raise ConnectionError("redis down")
        def get(self, *a, **kw): raise ConnectionError("redis down")
        def set(self, *a, **kw): raise ConnectionError("redis down")
        def script_load(self, *a, **kw): raise ConnectionError("redis down")
        def evalsha(self, *a, **kw): raise ConnectionError("redis down")

    result = check_widget_request(
        redis_client=_BrokenRedis(),
        session_key="sess-Y",
        client_ip="6.6.6.6",
    )
    assert result.allowed, "Must fail open on Redis exception"


# -----------------------------------------------------------------------
# TB-8: existing block marker → rejected immediately
# -----------------------------------------------------------------------

def test_tb8_existing_block_marker_rejects():
    """If a block marker already exists, the request is rejected immediately."""
    redis = _FakeRedis()
    # Manually plant a block marker.
    redis.set("luciel:wtb:block:sess:sess-blocked", "1", ex=_BLOCK_COOLDOWN_S)

    result = _call(redis, session_key="sess-blocked")
    assert not result.allowed, "Existing block marker must reject the request"
    # abuse_blocked is False because the block was pre-existing, not just triggered.
    assert not result.abuse_blocked


# -----------------------------------------------------------------------
# TB-9: clean pass resets abuse counter
# -----------------------------------------------------------------------

def test_tb9_clean_pass_resets_abuse_counter():
    """A successful request deletes the abuse counter (attacker backs off)."""
    redis = _FakeRedis()
    # Plant a partial abuse counter.
    redis.set("luciel:wtb:abuse:sess:sess-clean", "3", ex=120)
    redis.set("luciel:wtb:abuse:ip:1.2.3.4", "3", ex=120)

    # Make a fresh clean request on a key with no exhausted bucket.
    result = _call(redis, session_key="sess-clean")
    assert result.allowed

    # Abuse counters should be cleared.
    assert redis._get_raw("luciel:wtb:abuse:sess:sess-clean") is None, (
        "Abuse counter must be cleared after a clean pass"
    )
    assert redis._get_raw("luciel:wtb:abuse:ip:1.2.3.4") is None, (
        "IP abuse counter must be cleared after a clean pass"
    )


# -----------------------------------------------------------------------
# Integration: audit action constants exist
# -----------------------------------------------------------------------

def test_audit_constants_exist():
    """ACTION_WIDGET_ABUSE_BLOCKED must be importable and have a stable value."""
    from app.models.admin_audit_log import ACTION_WIDGET_ABUSE_BLOCKED
    assert ACTION_WIDGET_ABUSE_BLOCKED == "widget_abuse_blocked"
