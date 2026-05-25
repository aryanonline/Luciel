"""
Arc 9 C2 — Tenant context for in-app RLS connection-pool wrapper.

This module is the Layer-3 of the three-layer Wall 1 (admin_id) tenant
isolation model defined in ARC9_RUNBOOK (Drive, canonical):

  L1  Service-layer filtering — every repository SELECT/UPDATE includes
      an explicit ``WHERE tenant_id = :admin_id`` clause. Already in
      place (see C1 audit: 18/19 customer-data tables carry tenant_id).
      First line of defence.

  L2  PostgreSQL Row-Level-Security (RLS) — per-table policies that
      compare ``tenant_id`` to ``current_setting('app.admin_id')`` and
      reject rows that don't match. Backstop in case L1 is forgotten.
      Lands in C3 (per-table feature-flagged rollout).

  L3  In-app connection-pool wrapper (this file + checkout listener in
      app.db.session) — every request's DB connection has its
      ``app.admin_id`` GUC SET LOCAL to the authenticated admin's UUID
      before any query runs. Without this, the L2 RLS policies have
      no value to compare against and would either deny everything
      (FORCE ROW LEVEL SECURITY + default-deny) or be trivially
      bypassed.

This file owns the in-process ContextVar that carries the current
admin_id across async boundaries (FastAPI request handlers, Celery
tasks, repository methods). The session-level wiring that actually
issues ``SET LOCAL app.admin_id`` to PostgreSQL lives in
``app.db.session`` (engine checkout listener) and the FastAPI
dependency ``app.api.deps.get_tenant_scoped_db``.

Why ContextVar and not threading.local:
  FastAPI runs request handlers in an asyncio event loop.
  threading.local is shared across all coroutines on the same thread,
  so coroutine A's admin_id would leak into coroutine B's stack while
  A awaits I/O. ContextVar copies-on-task, giving us per-coroutine
  isolation that survives ``asyncio.create_task`` and
  ``run_in_executor`` boundaries.

Why ``SET LOCAL`` and not ``SET``:
  ``SET LOCAL`` is scoped to the current transaction. When the
  connection returns to the pool, the GUC is automatically cleared.
  ``SET`` would persist for the lifetime of the connection, so
  request N's admin_id would still be set when request N+1 checks the
  same connection out of the pool -- a tenant-leak vector if request
  N+1 forgets to SET it. ``SET LOCAL`` is the structurally safer
  primitive.

Feature flag:
  ``settings.rls_tenant_context_enabled`` (default False at v1) gates
  both the engine checkout listener AND the FastAPI dependency.
  C3 commits flip the flag per-environment as RLS policies land
  per-table. C9 flips the master in prod.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Optional


# IMPORTANT TYPE NOTE (Arc 9 C2 hot-fix 2026-05-24):
#
# admin_id is a STRING SLUG, not a UUID. The original C2 commit typed
# this as Optional[UUID] under the mistaken belief that admins.id was
# a uuid.UUID. Inspection of app/models/admin.py shows:
#
#     id: Mapped[str] = mapped_column(String(100), primary_key=True)
#
# Every customer-data table's ``tenant_id`` column is correspondingly
# String(100), and the literal string ``'platform'`` is used as the
# system-actions sentinel in admin_audit_logs.tenant_id. The middleware
# at app/middleware/auth.py:227 writes ``request.state.tenant_id =
# apikey.tenant_id`` where the source is also String(100).
#
# A UUID type annotation here would have crashed every request whose
# admin slug is not a valid UUID (which is most of them). The hot-fix
# changes the type to ``Optional[str]`` and preserves the rest of the
# C2 contract unchanged. The engine listener already calls
# ``str(admin_id)`` defensively so its behaviour is unchanged.


# Module-level ContextVar. The leading underscore is convention to
# signal "do not poke this directly -- use the get/set helpers".
# Default is None: an unset context means "no tenant scope, deny
# customer-data reads at the RLS layer".
_current_admin_id: ContextVar[Optional[str]] = ContextVar(
    "luciel_current_admin_id",
    default=None,
)


def set_current_admin_id(admin_id: Optional[str]) -> object:
    """Set the current admin_id for this async context.

    Returns the ContextVar token so the caller can pass it to
    ``reset_current_admin_id`` for nested-scope restoration. Most
    callers will not need to use the token; they should call
    ``clear_current_admin_id()`` at scope exit instead.

    Passing ``None`` explicitly clears the context (equivalent to
    ``clear_current_admin_id()``). This is the right behaviour for
    request paths that legitimately have no admin (health checks,
    public widget bootstrap before auth resolves).
    """
    return _current_admin_id.set(admin_id)


def get_current_admin_id() -> Optional[str]:
    """Return the admin_id bound to the current async context, or None.

    Repository and service layers SHOULD NOT use this for filtering
    -- L1 still passes admin_id explicitly through the call graph.
    This getter is for diagnostic logging, audit-context capture,
    and the engine checkout listener that issues the SET LOCAL.
    """
    return _current_admin_id.get()


def clear_current_admin_id() -> None:
    """Clear the admin_id from the current async context.

    Idempotent. Safe to call when no value is set. The FastAPI
    dependency calls this in ``finally`` to ensure no leak into the
    next request that happens to land on the same worker coroutine
    after the response is sent.
    """
    _current_admin_id.set(None)


def reset_current_admin_id(token: object) -> None:
    """Restore the previous admin_id value using a token from ``set``.

    Use this when temporarily impersonating a different admin (e.g.
    an admin_owner running a cross-tenant background job that must
    briefly run as a managed admin to populate their per-tenant
    summary). The pattern is:

        token = set_current_admin_id(other_admin_id)
        try:
            ...do work as other_admin_id...
        finally:
            reset_current_admin_id(token)

    Prefer this over set/clear/set when nesting is involved.
    """
    if token is None:
        return
    _current_admin_id.reset(token)  # type: ignore[arg-type]


__all__ = [
    "set_current_admin_id",
    "get_current_admin_id",
    "clear_current_admin_id",
    "reset_current_admin_id",
]
