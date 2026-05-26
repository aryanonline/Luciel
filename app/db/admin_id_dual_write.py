"""Arc 9.2 PR #96 dual-write hook: keep admin_id in lock-step with tenant_id.

Background
==========
Arc 9.2 (Option A) collapses ``tenant_id`` into ``admin_id`` on every
tenant-scoped table.  PR #96 added the additive ``admin_id`` column
(NOT NULL on 12 tables, NULLABLE on 4 platform-rows tables) and
backfilled it from ``tenant_id``.

The rest of the application code still writes ``tenant_id`` only.
Until PR #98 finishes the backend read switch and the service layer
starts writing ``admin_id`` natively, we need an automatic shim that
populates ``admin_id`` from ``tenant_id`` on every INSERT.

This module installs a single SQLAlchemy ``before_insert`` listener
per model that copies ``tenant_id -> admin_id`` whenever ``admin_id``
is unset.  It is intentionally idempotent and free of side-effects so
PR #101 (drop ``tenant_id``) can simply delete this file.

Why a listener, not a column default?
-------------------------------------
A SQLAlchemy ``default=`` callable would have to introspect the
mapper state to read the sibling ``tenant_id`` value, which is awkward
to express on the column declaration itself.  A ``before_insert``
listener has a clean handle to both attributes and runs once per row,
inside the same transaction as the INSERT.

The hook is conservative:
  * It NEVER overwrites a non-None ``admin_id``.  Services that already
    populate it (post PR #98) keep their value.
  * It does nothing when ``tenant_id`` is also None.  On the four
    NULLABLE tables this is the legitimate platform-row case.
"""
from __future__ import annotations

from typing import Iterable

from sqlalchemy import event
from sqlalchemy.orm import Mapper

from app.models.admin_audit_log import AdminAuditLog
from app.models.api_key import ApiKey
from app.models.conversation import Conversation
from app.models.identity_claim import IdentityClaim
from app.models.knowledge import KnowledgeEmbedding
from app.models.memory import MemoryItem
from app.models.message import MessageModel
from app.models.retention import DeletionLog, RetentionPolicy
from app.models.scope_assignment import ScopeAssignment
from app.models.session import SessionModel
from app.models.subscription import Subscription
from app.models.trace import Trace
from app.models.user_consent import UserConsent
from app.models.user_invite import UserInvite

# 15 models whose tables now carry admin_id (agent_configs excluded --
# the underlying table was dropped in Arc 5 Revision C).  Listed by
# Python class because event.listens_for needs the mapped class.
DUAL_WRITE_MODELS: tuple[type, ...] = (
    AdminAuditLog,
    ApiKey,
    Conversation,
    DeletionLog,
    IdentityClaim,
    KnowledgeEmbedding,
    MemoryItem,
    MessageModel,
    RetentionPolicy,
    ScopeAssignment,
    SessionModel,
    Subscription,
    Trace,
    UserConsent,
    UserInvite,
)


def _copy_tenant_to_admin(mapper: Mapper, connection, target) -> None:
    """Populate ``target.admin_id`` from ``target.tenant_id`` if unset."""
    if getattr(target, "admin_id", None) is None:
        tenant_id = getattr(target, "tenant_id", None)
        if tenant_id is not None:
            target.admin_id = tenant_id


_INSTALLED = False


def install_admin_id_dual_write(models: Iterable[type] = DUAL_WRITE_MODELS) -> None:
    """Attach the ``before_insert`` shim to every model in ``models``.

    Idempotent: a module-level guard prevents double-attach if called
    twice (e.g. by a reload during tests).
    """
    global _INSTALLED
    if _INSTALLED:
        return
    for model in models:
        event.listen(model, "before_insert", _copy_tenant_to_admin)
    _INSTALLED = True
