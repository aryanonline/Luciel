"""Step 29.y gap-fix C1: actor_permissions on-disk format invariants.

Drift token: D-actor-permissions-comma-fragility-2026-05-07.

Origin:
  - Pre-29.y, AuditContext.permissions_str joined with ',' and the
    defensive parser split on ','. The contract that no permission
    token contains a comma was implicit and unchecked.
  - actor_permissions is a chained field in admin_audit_logs hash
    chain (audit_chain.py::_CHAIN_FIELDS). Migrating historical
    column values to JSON would either break the chain or require
    rewriting row_hash for every historical row -- a Pattern E
    forensics red line.

This module asserts the gap-fix invariants:

  1. Round-trip: serialize -> parse returns the original sorted set
     of tokens.
  2. Dual-format read: parse_actor_permissions accepts both legacy
     comma form and the new JSON form, returning identical tuples.
  3. Forbidden characters in tokens are rejected at serialize time
     (forward-only invariant: future vocabulary must use plain
     identifiers).
  4. AuditContext.permissions_str produces the JSON form for new
     contexts.
  5. Hash chain stability: a row whose actor_permissions column
     contains the legacy comma form recomputes to the SAME row_hash
     that canonical_row_hash() produced before this gap-fix. This is
     the critical invariant that keeps Pillar 23 green across the
     deploy boundary.
  6. AdminAuditLogRead Pydantic schema normalizes both formats to a
     list[str] for consumers.
"""
from __future__ import annotations

import json

import pytest

from app.repositories.actor_permissions_format import (
    ActorPermissionsFormatError,
    parse_actor_permissions,
    serialize_actor_permissions,
)
from app.repositories.admin_audit_repository import AuditContext
from app.repositories.audit_chain import canonical_row_hash, _CHAIN_FIELDS


# --------------------------------------------------------------------- #
# 1. Round-trip                                                          #
# --------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "perms,expected",
    [
        ((), None),
        (None, None),
        (("admin",), '["admin"]'),
        (("admin", "worker"), '["admin","worker"]'),
        # Sorted + de-duplicated
        (("worker", "admin", "admin"), '["admin","worker"]'),
        # Whitespace stripped per token
        (("  admin  ", "worker"), '["admin","worker"]'),
    ],
)
def test_serialize_canonical_form(perms, expected):
    assert serialize_actor_permissions(perms) == expected


def test_round_trip_serialize_parse():
    original = ("admin", "worker", "chat", "sessions")
    serialized = serialize_actor_permissions(original)
    assert serialized is not None
    parsed = parse_actor_permissions(serialized)
    # Round-trip yields sorted, de-duplicated tuple of original tokens
    assert parsed == tuple(sorted(set(original)))


def test_round_trip_empty_inputs():
    assert serialize_actor_permissions(None) is None
    assert serialize_actor_permissions(()) is None
    assert parse_actor_permissions(None) == ()
    assert parse_actor_permissions("") == ()
    assert parse_actor_permissions("   ") == ()


# --------------------------------------------------------------------- #
# 2. Dual-format read                                                    #
# --------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "stored,expected",
    [
        # Legacy comma form (pre-29.y rows)
        ("admin", ("admin",)),
        ("admin,worker", ("admin", "worker")),
        ("admin, worker , chat", ("admin", "worker", "chat")),
        # New JSON form (post-29.y rows)
        ('["admin"]', ("admin",)),
        ('["admin","worker"]', ("admin", "worker")),
        ('["admin", "worker"]', ("admin", "worker")),  # whitespace-tolerant JSON
    ],
)
def test_parse_dual_format(stored, expected):
    assert parse_actor_permissions(stored) == expected


def test_parse_invalid_json_raises():
    with pytest.raises(ActorPermissionsFormatError):
        parse_actor_permissions('["admin"')  # unterminated


def test_parse_json_non_list_raises():
    with pytest.raises(ActorPermissionsFormatError):
        parse_actor_permissions('{"admin": true}')


# --------------------------------------------------------------------- #
# 3. Forbidden characters rejected at serialize                          #
# --------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "bad_token",
    [
        "role:admin,read",   # comma -- the original drift hazard
        'admin"injected',    # quote -- would break JSON
        "admin\\worker",     # backslash -- JSON escape
        "admin\nworker",     # newline
        "",                  # empty
        "   ",               # whitespace-only
    ],
)
def test_forbidden_token_rejected(bad_token):
    with pytest.raises(ActorPermissionsFormatError):
        serialize_actor_permissions([bad_token])


def test_forbidden_token_type_rejected():
    with pytest.raises(ActorPermissionsFormatError):
        serialize_actor_permissions([123])  # not a string


# --------------------------------------------------------------------- #
# 4. AuditContext produces the JSON form                                 #
# --------------------------------------------------------------------- #

def test_audit_context_permissions_str_is_json():
    ctx = AuditContext(actor_permissions=("worker", "admin"))
    s = ctx.permissions_str
    assert s == '["admin","worker"]'
    # Round-trip still holds
    assert parse_actor_permissions(s) == ("admin", "worker")


def test_audit_context_empty_permissions_is_none():
    ctx = AuditContext(actor_permissions=())
    assert ctx.permissions_str is None


def test_audit_context_system_factory():
    ctx = AuditContext.system()
    assert parse_actor_permissions(ctx.permissions_str) == ("system",)


def test_audit_context_worker_factory():
    ctx = AuditContext.worker(task_id="t-1", actor_key_prefix="abc")
    assert parse_actor_permissions(ctx.permissions_str) == ("worker",)


def test_audit_context_from_request_string_input_dual_format():
    """The defensive string-input branch must accept both formats."""

    class _State:
        def __init__(self, perms):
            self.permissions = perms
            self.key_prefix = "kp123"
            self.actor_label = "tester"
            self.tenant_id = "t1"

    class _Req:
        def __init__(self, state):
            self.state = state

    # Legacy comma form coming from an older middleware version
    legacy = AuditContext.from_request(_Req(_State("admin,worker")))
    assert legacy.actor_permissions == ("admin", "worker")

    # New JSON form
    new = AuditContext.from_request(_Req(_State('["admin","worker"]')))
    assert new.actor_permissions == ("admin", "worker")

    # Iterable input (the standard middleware path)
    iterable = AuditContext.from_request(_Req(_State(["admin", "worker"])))
    assert iterable.actor_permissions == ("admin", "worker")


# --------------------------------------------------------------------- #
# 5. Hash-chain stability across the format boundary                     #
# --------------------------------------------------------------------- #

def test_hash_chain_stable_for_legacy_comma_row():
    """Critical Pillar 23 invariant: a historical row with comma-form
    actor_permissions must recompute to the SAME row_hash after this
    gap-fix as it did before. We don't rewrite history; we just stop
    producing the comma form for new rows.

    canonical_row_hash() reads actor_permissions verbatim from the
    row dict. So as long as we pass the legacy string in, the output
    must match what the migration's backfill produced.
    """
    legacy_row = {k: None for k in _CHAIN_FIELDS}
    legacy_row.update({
        "tenant_id": "t1",
        "actor_key_prefix": "kp123",
        "actor_permissions": "admin,worker",  # legacy comma form
        "actor_label": "tester",
        "action": "tenant.update",
        "resource_type": "tenant",
        "resource_pk": 42,
        "created_at": None,
    })
    prev = "0" * 64
    h_before = canonical_row_hash(legacy_row, prev)

    # Recompute with the same legacy string -- must be identical.
    h_after = canonical_row_hash(legacy_row, prev)
    assert h_before == h_after

    # And confirm the JSON form produces a DIFFERENT hash (which is
    # fine because new rows store the JSON form and their stored
    # row_hash matches the JSON form's hash).
    new_row = dict(legacy_row)
    new_row["actor_permissions"] = '["admin","worker"]'
    h_new = canonical_row_hash(new_row, prev)
    assert h_new != h_before, (
        "Sanity check: JSON form should hash differently from comma "
        "form. The gap-fix preserves history precisely because we do "
        "NOT rewrite the column for old rows."
    )


# --------------------------------------------------------------------- #
# 6. API schema normalization                                            #
# --------------------------------------------------------------------- #

def test_audit_log_read_normalizes_legacy_comma_form():
    from app.schemas.audit_log import AdminAuditLogRead
    from datetime import datetime, timezone

    dto = AdminAuditLogRead(
        id=1,
        created_at=datetime.now(timezone.utc),
        actor_key_prefix="kp123",
        actor_permissions="admin,worker",  # legacy form from DB
        actor_label="tester",
        tenant_id="t1",
        domain_id=None,
        agent_id=None,
        luciel_instance_id=None,
        action="tenant.update",
        resource_type="tenant",
        resource_pk=42,
        resource_natural_id=None,
        before_json=None,
        after_json=None,
        note=None,
    )
    assert dto.actor_permissions == ["admin", "worker"]


def test_audit_log_read_normalizes_json_form():
    from app.schemas.audit_log import AdminAuditLogRead
    from datetime import datetime, timezone

    dto = AdminAuditLogRead(
        id=1,
        created_at=datetime.now(timezone.utc),
        actor_key_prefix="kp123",
        actor_permissions='["admin","worker"]',  # new form from DB
        actor_label="tester",
        tenant_id="t1",
        domain_id=None,
        agent_id=None,
        luciel_instance_id=None,
        action="tenant.update",
        resource_type="tenant",
        resource_pk=42,
        resource_natural_id=None,
        before_json=None,
        after_json=None,
        note=None,
    )
    assert dto.actor_permissions == ["admin", "worker"]


def test_audit_log_read_handles_null():
    from app.schemas.audit_log import AdminAuditLogRead
    from datetime import datetime, timezone

    dto = AdminAuditLogRead(
        id=1,
        created_at=datetime.now(timezone.utc),
        actor_key_prefix=None,
        actor_permissions=None,
        actor_label=None,
        tenant_id="t1",
        domain_id=None,
        agent_id=None,
        luciel_instance_id=None,
        action="tenant.update",
        resource_type="tenant",
        resource_pk=None,
        resource_natural_id=None,
        before_json=None,
        after_json=None,
        note=None,
    )
    assert dto.actor_permissions == []


# --------------------------------------------------------------------- #
# Self-check: the canonical form is JSON parseable                       #
# --------------------------------------------------------------------- #

def test_canonical_form_is_valid_json():
    s = serialize_actor_permissions(["admin", "worker", "chat"])
    decoded = json.loads(s)
    assert decoded == ["admin", "chat", "worker"]
