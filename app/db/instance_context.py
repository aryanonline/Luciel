"""
Arc 9 C4 -- Instance context for in-app RLS connection-pool wrapper.

This module is the Wall-3 (instance_id) sibling of the Wall-1 module
``app.db.tenant_context``. Together they form the two-GUC contract
that the C4 per-table RLS policies read at query time:

    Wall 1 (Account): GUC ``app.admin_id``   (set by tenant_context)
    Wall 3 (Instance): GUC ``app.instance_id`` (set by THIS module)

Why a separate ContextVar and not a tuple-keyed dict:

  Two reasons:

  (1) Background paths only know one of the two at a time. The C2
      tenant-context wrapper is wired from auth middleware (which
      always resolves admin_id) AND from worker tasks (which may or
      may not have an instance scope). Decoupling lets a Celery
      task run as ``(admin=X, instance=None)`` without forcing a
      synthetic instance value.

  (2) ContextVars copy-on-task, so two of them cost the same as one
      for the propagation path. No measured perf difference.

IMPORTANT TYPE NOTE
-------------------
Unlike ``admin_id`` (which is a String(100) slug, see the matching
type note in tenant_context.py), ``instances.id`` is an Integer
primary key. We carry it through the in-process ContextVar as
``Optional[int]`` and serialise it to its decimal string form at
``SET LOCAL`` time. The matching RLS policies (C4.3) cast the column
to text before comparing:

    USING (luciel_instance_id::text = current_setting('app.instance_id', true)
           OR luciel_instance_id IS NULL)

This is the canonical Postgres pattern for integer-vs-GUC equality
and avoids the SQL-injection-shaped fragility of building the
predicate string from the GUC integer at parse time.

Why ``SET LOCAL`` and not ``SET``:
  Same rationale as tenant_context.py -- transaction-scoped so the
  connection returns to the pool clean.

Feature flag:
  ``settings.rls_tenant_context_enabled`` (the SAME master flag as
  Wall 1). C4.3 RLS policies AND the C4.1 listener both honour the
  flag. Flipping the flag in ECS enables Wall 1 + Wall 3 RLS
  together. There is no per-wall flag; the runbook's deploy gate
  requires them to ship as a coupled bundle.

NULL semantics:
  ``None`` means "no instance scope bound to this context". This is
  the legitimate state for:
    - Admin-level API keys (api_keys.luciel_instance_id IS NULL)
    - Cross-instance memory queries (memory_items rows where the
      memory is account-scoped, not instance-scoped)
    - Background tasks that span multiple instances (e.g. an
      admin-wide nightly summary)
  All 6 Wall-3 tables permit ``luciel_instance_id IS NULL`` rows,
  so the RLS policies use the C3.3-shape asymmetric NULL-permissive
  pattern: USING allows NULL-or-matching reads, WITH CHECK gates
  NULL writes on having no instance scope set (a 'platform-or-
  admin-wide' write context).
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Optional


# Module-level ContextVar. Naming convention mirrors tenant_context.
# Default is None -- unset context means "no instance scope", which
# at the RLS layer means "can read NULL-tagged rows or rows
# explicitly scoped to no instance", same posture as the L1
# service-layer behaviour today.
_current_instance_id: ContextVar[Optional[int]] = ContextVar(
    "luciel_current_instance_id",
    default=None,
)


def set_current_instance_id(instance_id: Optional[int]) -> object:
    """Set the current instance_id for this async context.

    Returns the ContextVar token so the caller can pass it to
    ``reset_current_instance_id`` for nested-scope restoration.
    Most callers will use ``clear_current_instance_id()`` at scope
    exit instead.

    Passing ``None`` explicitly clears the context (equivalent to
    ``clear_current_instance_id()``). This is the right behaviour
    for request paths that legitimately have no instance scope
    (admin-level API key, admin dashboard at /api/v1/admin/* with
    no instance-id query parameter).
    """
    return _current_instance_id.set(instance_id)


def get_current_instance_id() -> Optional[int]:
    """Return the instance_id bound to the current async context, or None.

    Repository and service layers SHOULD NOT use this for filtering
    -- L1 still passes instance_id explicitly through the call graph
    (see C4 service-layer audit for the few residual gaps).

    This getter is for diagnostic logging, audit-context capture,
    and the engine listener that issues the SET LOCAL.
    """
    return _current_instance_id.get()


def clear_current_instance_id() -> None:
    """Clear the instance_id from the current async context.

    Idempotent. Safe to call when no value is set. The FastAPI
    dependency calls this in ``finally`` to ensure no leak into the
    next request that happens to land on the same worker coroutine
    after the response is sent.
    """
    _current_instance_id.set(None)


def reset_current_instance_id(token: object) -> None:
    """Restore the previous instance_id value using a token from ``set``.

    Use this when temporarily switching instance scope (e.g. an
    admin_owner running a per-instance summary across all their
    instances). Pattern:

        token = set_current_instance_id(other_instance_id)
        try:
            ...do work scoped to other_instance_id...
        finally:
            reset_current_instance_id(token)

    Prefer this over set/clear/set when nesting is involved.
    """
    if token is None:
        return
    _current_instance_id.reset(token)  # type: ignore[arg-type]


__all__ = [
    "set_current_instance_id",
    "get_current_instance_id",
    "clear_current_instance_id",
    "reset_current_instance_id",
]
