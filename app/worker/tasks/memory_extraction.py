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
    actor_user_id         str | None # Step 24.5b -- platform User UUID
                                     # serialized as string for JSON
                                     # transport. Worker parses to UUID
                                     # at task entry. None for legacy /
                                     # pre-backfill rows.

Pre-flight gates (Invariant 8 -- defense in depth):
    1. Payload shape validation               -> Reject to DLQ on fail
    2. API key still active                   -> Reject to DLQ on fail
    3. Session.tenant_id == payload.tenant_id -> Reject to DLQ on fail
       (Invariant 13 -- mandatory tenant predicate; also catches
        cross-tenant enqueue attempts)
    4. LucielInstance.active is True (when luciel_instance_id present)
                                              -> Reject to DLQ on fail
    5. User.active is True (when actor_user_id present, Step 24.5b)
                                              -> Reject to DLQ on fail
       Pillar 12 (Commit 3) asserts this gate fires when a User is
       deactivated mid-flight after enqueue.
    6. Agent.user_id == payload.actor_user_id (Step 24.5b -- Q6
       cross-tenant identity-spoof guard)     -> Reject to DLQ on fail
       Pillar 13 (Commit 3) asserts a malicious payload claiming
       (user_id=U, tenant_id=T1, agent_id=A2_under_T2) lands in DLQ
       because A2's row has tenant_id=T2 and user_id=U2, not the
       payload's claimed values.

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
import uuid

from celery import shared_task
from celery.exceptions import Reject, Retry

# Step 29.y Cluster 4 (E-3): the canonical set of transient
# exception classes that should trigger a retry. Anything not in
# this tuple is permanent and routes through _reject_with_audit
# to DLQ deterministically. See findings_phase1e.md E-3 for the
# rationale -- pre-29.y autoretry_for=(Exception,) caught Reject
# itself in some Celery versions, producing 3-4 audit rows per
# rejection instead of 1.
import redis.exceptions as _redis_exc
from sqlalchemy.exc import OperationalError as _SAOperationalError

_TRANSIENT_EXC = (_SAOperationalError, _redis_exc.ConnectionError)
from sqlalchemy import select

from app.db.session import SessionLocal
from app.integrations.llm.router import ModelRouter
from app.memory.service import MemoryService
from app.models.admin_audit_log import (
    ACTION_WORKER_USER_INACTIVE,
    ACTION_WORKER_IDENTITY_SPOOF_REJECT,
    ACTION_WORKER_PERMANENT_FAILURE,
)
from app.models.agent import Agent
from app.models.api_key import ApiKey
from app.models.luciel_instance import LucielInstance
from app.models.message import MessageModel
from app.models.session import SessionModel
from app.models.user import User
from app.worker.audit_failure_counter import (
    WORKER_AUDIT_WRITE_FAILED,
    record_audit_write_failure,
)
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
        ctx = AuditContext.worker(task_id=f"memory_extraction:{task_id}", actor_key_prefix=actor_key_prefix)
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
            # Step 29.y Cluster 4 (E-2 fix): a worker killed between
            # this audit.record() commit and the Reject() raise below
            # leaves the SQS message visible after timeout. The
            # redelivery would otherwise rewrite this same audit row
            # (resource_natural_id is deterministic on session/message
            # ids). The DB-level partial unique index
            # ux_admin_audit_logs_worker_reject_idem (migration
            # d8e2c4b1a0f3) catches the duplicate; skip_on_conflict
            # tells the repo to swallow the IntegrityError and
            # return None instead of crashing the rejection path.
            skip_on_conflict=True,
        )
    except Exception:
        # Step 29.y gap-fix C4
        # (D-worker-audit-write-failure-not-alerted-2026-05-07):
        # never let audit-write failure mask the original rejection,
        # but make the failure structured and countable. The
        # WORKER_AUDIT_WRITE_FAILED marker is the stable string an
        # operability layer (CloudWatch metric filter / Prom exporter)
        # pins on. record_audit_write_failure() ticks a process-local
        # counter so a future health endpoint or test can observe
        # "this worker has seen N audit-write failures since boot."
        failure_count = record_audit_write_failure()
        logger.exception(
            "%s audit-write failed during rejection "
            "action=%s task=%s process_failure_count=%d",
            WORKER_AUDIT_WRITE_FAILED, action, task_id, failure_count,
        )

    raise Reject(note, requeue=False)


# ---------- task ----------
@shared_task(
    name="app.worker.tasks.memory_extraction.extract_memory_from_turn",
    bind=True,
    max_retries=3,
    default_retry_delay=2,
    # Step 29.y Cluster 4 (E-3): autoretry_for is empty here.
    # Retries are dispatched manually via self.retry() inside the
    # task body, only for the narrow _TRANSIENT_EXC tuple defined
    # above. Permanent exceptions route through
    # _reject_with_audit to DLQ. autoretry_for=(Exception,) caught
    # Reject in some Celery 5.x versions producing duplicate
    # rejection audit rows.
    autoretry_for=(),
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
    actor_user_id: str | None = None,  # Step 24.5b: platform User UUID as str
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
        
        # ---------- Gate 5: actor_user_id parse + User.active (Step 24.5b) ----------
        actor_user_uuid: uuid.UUID | None = None
        if actor_user_id is not None:
            # Parse string -> UUID. Malformed actor_user_id is treated as
            # gate 1 (malformed payload) -- the enqueue side serialized
            # this from a real uuid.UUID, so a parse failure here means
            # the payload was tampered with in transit.
            try:
                actor_user_uuid = uuid.UUID(actor_user_id)
            except (ValueError, TypeError):
                logger.error(
                    "gate5 actor_user_id unparseable task=%s tenant=%s",
                    task_id, tenant_id,
                )
                _reject_with_audit(
                    db,
                    action=ACTION_WORKER_MALFORMED_PAYLOAD,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    message_id=message_id,
                    actor_key_prefix=actor_key_prefix,
                    note="actor_user_id failed UUID parse",
                    task_id=task_id,
                    trace_id=trace_id,
                )

            # User.active check. Deactivated users cannot have memory
            # written for them mid-flight after enqueue.
            user_row = db.get(User, actor_user_uuid)
            if user_row is None or not user_row.active:
                logger.warning(
                    "gate5 user inactive/missing task=%s actor_user_id=%s",
                    task_id, actor_user_uuid,
                )
                _reject_with_audit(
                    db,
                    action=ACTION_WORKER_USER_INACTIVE,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    message_id=message_id,
                    actor_key_prefix=actor_key_prefix,
                    note=(
                        f"actor_user inactive or missing: "
                        f"{actor_user_uuid}"
                    ),
                    task_id=task_id,
                    trace_id=trace_id,
                )

        # ---------- Gate 6: cross-tenant identity-spoof guard (Step 24.5b -- Q6) ----------
        # When both agent_id and actor_user_id are present, the Agent row
        # at (tenant_id, agent_id) MUST have user_id == actor_user_uuid.
        # This catches a malicious payload that claims an actor identity
        # whose Agent lives in a different tenant. Pillar 13 in Commit 3
        # asserts this gate fires.
        if actor_user_uuid is not None and agent_id is not None:
            spoof_agent = db.scalars(
                select(Agent).where(
                    Agent.tenant_id == tenant_id,
                    Agent.agent_id == agent_id,
                ).limit(1)
            ).first()
            if spoof_agent is None or spoof_agent.user_id != actor_user_uuid:
                logger.warning(
                    "gate6 identity spoof task=%s payload_actor_user=%s "
                    "agent_user=%s tenant=%s agent_id=%s",
                    task_id,
                    actor_user_uuid,
                    spoof_agent.user_id if spoof_agent else None,
                    tenant_id,
                    agent_id,
                )
                _reject_with_audit(
                    db,
                    action=ACTION_WORKER_IDENTITY_SPOOF_REJECT,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    message_id=message_id,
                    actor_key_prefix=actor_key_prefix,
                    note=(
                        f"actor_user_id mismatch: payload claims "
                        f"{actor_user_uuid}, agent ({tenant_id},"
                        f"{agent_id}) has "
                        f"{spoof_agent.user_id if spoof_agent else 'None'}"
                    ),
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
            message_id=message_id,
            luciel_instance_id=luciel_instance_id,
            actor_user_id=actor_user_uuid,  # Step 24.5b File 2.6d
        )

        # ---------- Audit row in same txn (Invariant 4) ----------
        # Content digest only — never raw content in audit.
        content_digest = _content_sha256(
            "\n".join(f"{m['role']}:{m['content']}" for m in messages_payload)
        )
        ctx = AuditContext.worker(
            task_id=f"memory_extraction:{task_id}",
            actor_key_prefix=actor_key_prefix,
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
                "actor_user_id": (
                    str(actor_user_uuid) if actor_user_uuid else None
                ),  # Step 24.5b File 2.6d
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
    except Retry:
        # self.retry() raises Retry; let Celery handle the redelivery.
        raise
    except _TRANSIENT_EXC as exc:
        # Step 29.y Cluster 4 (E-3): transient-class failures are
        # explicitly retried via self.retry(). Anything outside
        # _TRANSIENT_EXC is treated as permanent and routes through
        # the rejection path below so the message lands in DLQ
        # immediately instead of cycling through 3 retries that
        # cannot succeed.
        db.rollback()
        logger.warning(
            "transient failure task=%s type=%s attempt=%d/%d",
            task_id,
            type(exc).__name__,
            self.request.retries + 1,
            self.max_retries + 1,
        )
        raise self.retry(exc=exc)
    except Exception as exc:
        # Step 29.y Cluster 4 (E-3): permanent failure. Do NOT retry.
        # Route to DLQ via the same audit-then-reject path that
        # malformed-payload rejections use. The
        # ACTION_WORKER_PERMANENT_FAILURE action lands a single
        # audit row keyed on (action, tenant_id, resource_natural_id)
        # so a worker-crash redelivery is idempotent (E-2 partial
        # unique index).
        db.rollback()
        logger.exception(
            "permanent failure task=%s type=%s",
            task_id,
            type(exc).__name__,
        )
        _reject_with_audit(
            db=db,
            action=ACTION_WORKER_PERMANENT_FAILURE,
            tenant_id=tenant_id,
            session_id=session_id,
            message_id=message_id,
            actor_key_prefix=actor_key_prefix,
            note=f"permanent failure: {type(exc).__name__}",
            task_id=task_id,
            trace_id=trace_id,
        )
    finally:
        db.close()