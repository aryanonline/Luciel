"""
Arc 9 C4.4 -- Background / non-HTTP tenant scope binding.

Why this module exists
======================
The FastAPI dependency ``get_tenant_scoped_db`` (C2 + C4.2) is the
ONLY production binding path for the admin_id and instance_id
ContextVars. It works because every HTTP request runs through the
dependency before any DB session opens.

But not every code path is an HTTP request:

  * Celery tasks (memory_extraction, retention) receive their tenant
    + instance ids via the task payload and open SessionLocal() to
    do their work.
  * Audit-log side sessions (memory/service.py) open a fresh
    SessionLocal() outside the parent request's transaction in
    order to keep the audit chain advancing on extractor failures.
  * Scheduled jobs and CLI tooling (future) will have the same shape.

Under the Arc 9 master flag ``rls_tenant_context_enabled``, the
engine-level after_begin listener emits ``set_config('app.admin_id',
'', true)`` and ``set_config('app.instance_id', '', true)`` on every
BEGIN when no ContextVar is bound. That is fail-closed at Wall 1
strict policies (every C3.x policy except C3.3 / C3.6 / C4.3 denies
all reads/writes when the GUC is ''). Workers would therefore start
failing the moment the flag flips -- unless they bind the scope
explicitly.

This module is that binding primitive for non-HTTP callers.

Contract
========
``bind_tenant_scope(admin_id, instance_id)`` is a context manager
that:

  1. Binds the admin_id ContextVar via set_current_admin_id().
  2. Binds the instance_id ContextVar via set_current_instance_id().
  3. Yields to the caller.
  4. Resets BOTH ContextVars in a finally block, INDEPENDENTLY --
     a failure to reset one MUST NOT prevent the other from being
     reset (matches get_tenant_scoped_db's independent-reset rule).

Both arguments are required positional. Pass admin_id=None /
instance_id=None explicitly for unbound paths (no implicit
defaults). This forces the caller to think about each wall's
scope per-invocation.

Usage in a Celery task
======================

    @celery_app.task
    def extract_memories(task_id, tenant_id, session_id, ..., luciel_instance_id):
        with bind_tenant_scope(admin_id=tenant_id, instance_id=luciel_instance_id):
            db = SessionLocal()
            try:
                # ... do work; all SELECTs/INSERTs on this session
                # see RLS policies enforced against the bound scope.
                ...
            finally:
                db.close()

The order matters: bind scope FIRST, then open the session. The
listener fires on BEGIN, and BEGIN happens lazily on first query.
But if the session is opened BEFORE the scope binding, an
intervening implicit BEGIN (e.g. a healthcheck ping) would emit
empty GUCs that linger on the pooled connection for the rest of
the work. Always open SessionLocal INSIDE the with-block.

Async note
==========
ContextVar is async-safe. ``bind_tenant_scope`` is a SYNCHRONOUS
context manager. For async callers (FastAPI middleware, async
Celery workers), wrap the entire async function body in the with-
block; ContextVar copies into spawned tasks per asyncio semantics.

Why not reuse get_tenant_scoped_db?
===================================
get_tenant_scoped_db takes a FastAPI Request. Workers have no
Request -- forcing them to synthesise a fake one would tightly
couple unrelated layers and obscure intent. A purpose-built helper
for non-HTTP callers reads cleaner at call sites and tests cleaner
in isolation.

Refs ARC9_RUNBOOK §C4.4, _arc9/C4_service_audit.md.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Generator, Optional

from app.db.instance_context import (
    clear_current_instance_id,
    reset_current_instance_id,
    set_current_instance_id,
)
from app.db.tenant_context import (
    clear_current_admin_id,
    reset_current_admin_id,
    set_current_admin_id,
)


@contextmanager
def bind_tenant_scope(
    *,
    admin_id: Optional[str],
    instance_id: Optional[int],
) -> Generator[None, None, None]:
    """Bind both Wall-1 (admin_id) and Wall-3 (instance_id) scopes.

    Yields nothing; the caller does its DB work inside the with-block
    and the bindings are reset on exit.

    Both kwargs are required (no defaults) to force every caller to
    declare intent for both walls. Pass None explicitly for unbound
    paths; pass real values for scoped paths.
    """
    admin_token = set_current_admin_id(admin_id)
    instance_token = set_current_instance_id(instance_id)
    try:
        yield
    finally:
        # Reset both ContextVars INDEPENDENTLY. If either reset
        # raises (corrupt token, ContextVar machinery glitch) we
        # fall back to clear() so the other still runs. Matches
        # the discipline in get_tenant_scoped_db.
        try:
            reset_current_admin_id(admin_token)
        except Exception:
            clear_current_admin_id()
        try:
            reset_current_instance_id(instance_token)
        except Exception:
            clear_current_instance_id()


__all__ = ["bind_tenant_scope"]
