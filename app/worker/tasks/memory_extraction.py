"""
Async memory extraction task (Step 27b).

Moves MemoryService.extract_and_save off the chat request path.

Contract reference: docs/runbooks/step-27b-security-contract.md

Payload (opaque ids only; NO user content):
    session_id            int
    user_id               str
    tenant_id             str
    message_id            int        # idempotency key; FK to messages.id
    actor_key_prefix      str        # enqueuing api_key.key_prefix (audit linkage)
    agent_id              str | None
    luciel_instance_id    int | None

Pre-flight gates (Invariant 8 — defense in depth):
    1. Payload shape validation               -> Reject to DLQ on fail
    2. API key still active                   -> Reject to DLQ on fail
    3. Session.tenant_id == payload.tenant_id -> Reject to DLQ on fail
       (Invariant 13 — mandatory tenant predicate; also catches
        cross-tenant enqueue attempts)
    4. LucielInstance.active is True (when luciel_instance_id present)
                                              -> Reject to DLQ on fail

Execution:
    - Re-read turn window from DB via join(MessageModel -> SessionModel)
      scoped to (session_id, tenant_id, up_to message_id).
    - Call MemoryService.extract_and_save with re-read messages.
    - Audit row (action=MEMORY_EXTRACTED) lands in same txn as memory
      upsert (Invariant 4). Content is NEVER placed in audit row
      — only SHA256.

Failure modes (see contract table):
    - Malformed payload / gate failure -> Reject (no retry, DLQ)
    - LLM/embedding transient error    -> Retry 3x, then DLQ
    - DB transient error               -> Retry 3x, then DLQ
    - Duplicate message_id             -> No-op idempotent success
"""
from __future__ import annotations

import hashlib
import logging

from celery import shared_task
from celery.exceptions import Reject
from sqlalchemy import select

from app.db.session import SessionLocal
from app.integrations.llm.router import ModelRouter
from app.memory.service import MemoryService
from app.models.api_key import ApiKey
from app.models.luciel_instance import LucielInstance
from app.models.message import MessageModel
from app.models.session import SessionModel
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)
from app.repositories.memory_repository import MemoryRepository

logger = logging.getLogger(__name__)

# ---------- constants ----------
TURN_WINDOW_SIZE = 20
ACTION_MEMORY_EXTRACTED = "memory_extracted"
ACTION_WORKER_MALFORMED_PAYLOAD = "worker_malformed_payload"
ACTION_WORKER_KEY_REVOKED = "worker_key_revoked"
ACTION_WORKER_CROSS_TENANT_REJECT = "worker_cross_tenant_reject"
ACTION_WORKER_INSTANCE_DEACTIVATED = "worker_instance_deactivated"
RESOURCE_MEMORY = "memory"


# ---------- helpers ----------
def _content_sha256(text: str) -> str:
    """SHA256 hex of content; stored in audit rows instead of raw text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _reject_with_audit(
    db,
    *,
    action: str,
    tenant_id: str | None,
    session_id: str | None,
    message_id: int | None,
    actor_key_prefix: str | None,
    note: str,
    task_id: str,
    trace_id: str | None = None,
) -> None:
    """
    Write a rejection audit row and raise Reject(requeue=False).

    Uses AuditContext.system() with a worker label. Commits synchronously
    so the audit row lands even though the task itself is rejected.
    Never retries; rejection path is terminal.
    """
    try:
        ctx = AuditContext.system(label=f"worker:memory_extraction:{task_id}")
        audit = AdminAuditRepository(db)
        audit.record(
            ctx=ctx,
            tenant_id=tenant_id or "unknown",
            action=action,
            resource_type=RESOURCE_MEMORY,
            resource_pk=None,
            resource_natural_id=(
                f"session={session_id};message={message_id}"
                if session_id is not None
                else None
            ),
            before=None,
            after={
                "actor_key_prefix": actor_key_prefix,
                "session_id": session_id,
                "message_id": message_id,
                "trace_id": trace_id,
            },
            note=note,
            autocommit=True,
        )
    except Exception:
        # Never let audit-write failure mask the original rejection.
        logger.exception(
            "audit-write failed during rejection (action=%s task=%s)",
            action, task_id,
        )

    raise Reject(note, requeue=False)


# ---------- task ----------
@shared_task(
    name="app.worker.tasks.memory_extraction.extract_memory_from_turn",
    bind=True,
    max_retries=3,
    default_retry_delay=2,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=8,
    retry_jitter=True,
    acks_late=True,
    ignore_result=True,
)
def extract_memory_from_turn(
    self,
    *,
    session_id: str,
    user_id: str,
    tenant_id: str,
    message_id: int,
    actor_key_prefix: str,
    agent_id: str | None = None,
    luciel_instance_id: int | None = None,
    trace_id: str | None = None,
) -> None:
    """
    Extract durable memories from a just-completed chat turn and persist them.

    Idempotent on (tenant_id, message_id) via composite partial unique index.
    Re-execution (replay, DLQ redrive) is a safe no-op.
    """
    task_id = self.request.id or "no-task-id"

    db = SessionLocal()
    try:
        # ---------- Gate 1: payload shape ----------
        if not (
            isinstance(session_id, str)
            and isinstance(user_id, str) and user_id
            and isinstance(tenant_id, str) and tenant_id
            and isinstance(message_id, int)
            and isinstance(actor_key_prefix, str) and actor_key_prefix
        ):
            logger.error(
                "gate1 malformed payload task=%s tenant=%s session=%s",
                task_id, tenant_id, session_id,
            )
            _reject_with_audit(
                db,
                action=ACTION_WORKER_MALFORMED_PAYLOAD,
                tenant_id=tenant_id if isinstance(tenant_id, str) else None,
                session_id=session_id if isinstance(session_id, str) else None,
                message_id=message_id if isinstance(message_id, int) else None,
                actor_key_prefix=actor_key_prefix
                if isinstance(actor_key_prefix, str) else None,
                note="malformed payload",
                task_id=task_id,
                trace_id=trace_id if isinstance(trace_id, str) else None,
            )

        # ---------- Gate 2: API key still active ----------
        key_row = db.scalars(
            select(ApiKey).where(ApiKey.key_prefix == actor_key_prefix).limit(1)
        ).first()
        if key_row is None or not key_row.active:
            logger.warning(
                "gate2 key revoked task=%s prefix=%s",
                task_id, actor_key_prefix,
            )
            _reject_with_audit(
                db,
                action=ACTION_WORKER_KEY_REVOKED,
                tenant_id=tenant_id,
                session_id=session_id,
                message_id=message_id,
                actor_key_prefix=actor_key_prefix,
                note="enqueuing key no longer active",
                task_id=task_id,
                trace_id=trace_id,
            )

        # ---------- Gate 3: session.tenant_id == payload.tenant_id ----------
        session_row = db.get(SessionModel, session_id)
        if session_row is None or session_row.tenant_id != tenant_id:
            logger.warning(
                "gate3 cross-tenant reject task=%s payload_tenant=%s session=%s",
                task_id, tenant_id, session_id,
            )
            _reject_with_audit(
                db,
                action=ACTION_WORKER_CROSS_TENANT_REJECT,
                tenant_id=tenant_id,
                session_id=session_id,
                message_id=message_id,
                actor_key_prefix=actor_key_prefix,
                note="session.tenant_id mismatch or session missing",
                task_id=task_id,
                trace_id=trace_id,
            )

        # ---------- Gate 4: LucielInstance active ----------
        if luciel_instance_id is not None:
            instance = db.get(LucielInstance, luciel_instance_id)
            if instance is None or not instance.active:
                logger.warning(
                    "gate4 instance deactivated task=%s instance=%s",
                    task_id, luciel_instance_id,
                )
                _reject_with_audit(
                    db,
                    action=ACTION_WORKER_INSTANCE_DEACTIVATED,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    message_id=message_id,
                    actor_key_prefix=actor_key_prefix,
                    note="luciel_instance deactivated between enqueue and dequeue",
                    task_id=task_id,
                    trace_id=trace_id,
                )

        # ---------- Re-read turn window from DB (Invariant 13: tenant-scoped) ----------
        stmt = (
            select(MessageModel)
            .join(SessionModel, SessionModel.id == MessageModel.session_id)
            .where(
                SessionModel.id == session_id,
                SessionModel.tenant_id == tenant_id,
                MessageModel.id <= message_id,
            )
            .order_by(MessageModel.id.desc())
            .limit(TURN_WINDOW_SIZE)
        )
        rows = list(db.scalars(stmt).all())
        rows.reverse()  # chronological order for the extractor

        if not rows:
            logger.warning(
                "empty turn window task=%s session=%s message=%s",
                task_id, session_id, message_id,
            )
            # Not a rejection — just nothing to extract. Idempotent no-op.
            return

        messages_payload = [
            {"role": r.role, "content": r.content}
            for r in rows
        ]

        # ---------- Execute extraction ----------
        repository = MemoryRepository(db)
        service = MemoryService(
            repository=repository,
            model_router=ModelRouter(),
        )

        saved_count = service.extract_and_save(
            user_id=user_id,
            tenant_id=tenant_id,
            session_id=session_id,
            agent_id=agent_id,
            messages=messages_payload,
            # message_id / luciel_instance_id passed through to repository
            # once File 2.6 / 2.8 patches land.
        )

        # ---------- Audit row in same txn (Invariant 4) ----------
        # Content digest only — never raw content in audit.
        content_digest = _content_sha256(
            "\n".join(f"{m['role']}:{m['content']}" for m in messages_payload)
        )
        ctx = AuditContext.system(
            label=f"worker:memory_extraction:{task_id}"
        )
        AdminAuditRepository(db).record(
            ctx=ctx,
            tenant_id=tenant_id,
            action=ACTION_MEMORY_EXTRACTED,
            resource_type=RESOURCE_MEMORY,
            resource_pk=None,   # memory_items are append-only; no single pk
            resource_natural_id=f"session={session_id};message={message_id}",
            domain_id=session_row.domain_id,
            agent_id=agent_id,
            luciel_instance_id=luciel_instance_id,
            before=None,
            after={
                "actor_key_prefix": actor_key_prefix,
                "user_id": user_id,
                "session_id": session_id,
                "message_id": message_id,
                "trace_id": trace_id,
                "turn_window_size": len(messages_payload),
                "saved_count": saved_count,
                "content_sha256": content_digest,
            },
            note="async memory extraction",
            autocommit=False,
        )

        # Commit memory rows + audit row together.
        db.commit()

        logger.info(
            "extraction ok task=%s tenant=%s session=%s message=%s saved=%d",
            task_id, tenant_id, session_id, message_id, saved_count,
        )

    except Reject:
        # Rejection path already wrote its own audit row; do not retry.
        raise
    except Exception as exc:
        # Transient failure — let autoretry_for=(Exception,) handle retry.
        # Log exception CLASS only; never str(exc) which may echo payload.
        db.rollback()
        logger.warning(
            "transient failure task=%s type=%s attempt=%d/%d",
            task_id,
            type(exc).__name__,
            self.request.retries + 1,
            self.max_retries + 1,
        )
        raise
    finally:
        db.close()