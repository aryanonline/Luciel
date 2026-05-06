"""
AdminAuditLog — durable, per-tenant audit trail for admin mutations.

Step 24.5 (File 6.5a). Every admin-layer create / update / deactivate
across Agent, LucielInstance, DomainConfig, TenantConfig, ApiKey, and
(from Step 25 onward) KnowledgeEmbedding writes exactly one row here,
inside the same DB transaction as the mutation itself.

Why a dedicated table (vs. reusing Trace or CloudWatch logs):
- Trace is for chat turns — mixing admin events pollutes analytics
  and breaks retention rules (Step 21 has different cutoffs per
  category).
- CloudWatch logs are ephemeral, expensive to query, and not
  tenant-scoped — a tenant admin can't inspect their own audit trail
  without giving them AWS access.
- PIPEDA "reasonable safeguards" expects auditability of access to
  and modification of personal information. Prompts, tools, and
  contact emails qualify.

What gets captured:
- WHO:   actor_key_prefix + actor_permissions (never the raw key)
- WHEN:  created_at (TimestampMixin)
- WHERE: tenant_id / domain_id / agent_id / luciel_instance_id
         (whichever apply to the resource)
- WHAT:  action verb + resource_type + resource_pk + resource_natural_id
- DIFF:  before / after JSON — only the fields that actually changed

Not a general event bus — strictly admin mutations. Chat events stay
in Trace. Retention purges stay in DeletionLog. Consent events stay
in UserConsent.

Domain-agnostic: no vertical enums, no imports from app/domain/.
"""
from __future__ import annotations

from sqlalchemy import CHAR, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


# ---------------------------------------------------------------------
# Action verb constants — kept as module-level strings (not a PG enum)
# so new actions can be added without a schema migration. The allowed
# values are advisory: any writer is expected to use one of these.
# ---------------------------------------------------------------------

ACTION_CREATE = "create"
ACTION_UPDATE = "update"
ACTION_DEACTIVATE = "deactivate"
ACTION_REACTIVATE = "reactivate"
ACTION_CASCADE_DEACTIVATE = "cascade_deactivate"
ACTION_REPLACE = "replace"       # Step 25 knowledge versioning
ACTION_DELETE_HARD = "delete_hard"  # reserved — not used by Step 24.5
RESOURCE_KNOWLEDGE = "knowledge"
ACTION_KNOWLEDGE_INGEST = "knowledge_ingest"
ACTION_KNOWLEDGE_REPLACE = "knowledge_replace"
ACTION_KNOWLEDGE_DELETE = "knowledge_delete"

# Step 27b: async memory extraction worker
ACTION_MEMORY_EXTRACTED = "memory_extracted"
ACTION_WORKER_MALFORMED_PAYLOAD = "worker_malformed_payload"
ACTION_WORKER_KEY_REVOKED = "worker_key_revoked"
ACTION_WORKER_CROSS_TENANT_REJECT = "worker_cross_tenant_reject"
ACTION_WORKER_INSTANCE_DEACTIVATED = "worker_instance_deactivated"
# Step 24.5b: worker defense-in-depth gates for User identity layer
ACTION_WORKER_USER_INACTIVE = "worker_user_inactive"
ACTION_WORKER_IDENTITY_SPOOF_REJECT = "worker_identity_spoof_reject"
# Step 24.5b: Q6 resolution -- mandatory key rotation cascade on role change.
# Distinct from ACTION_DEACTIVATE because the key was active+valid; this
# captures "key was good but the User's scope assignment ended, so we
# rotated as a security cascade." Auditors filter on this to find all
# role-change-induced revocations.
ACTION_KEY_ROTATED_ON_ROLE_CHANGE = "key_rotated_on_role_change"

ALLOWED_ACTIONS = (
    ACTION_CREATE,
    ACTION_UPDATE,
    ACTION_DEACTIVATE,
    ACTION_REACTIVATE,
    ACTION_CASCADE_DEACTIVATE,
    ACTION_REPLACE,
    ACTION_DELETE_HARD,
    ACTION_KNOWLEDGE_INGEST,
    ACTION_KNOWLEDGE_REPLACE,
    ACTION_KNOWLEDGE_DELETE,

    # Step 27b
    ACTION_MEMORY_EXTRACTED,
    ACTION_WORKER_MALFORMED_PAYLOAD,
    ACTION_WORKER_KEY_REVOKED,
    ACTION_WORKER_CROSS_TENANT_REJECT,
    ACTION_WORKER_INSTANCE_DEACTIVATED,
    # Step 24.5b
    ACTION_KEY_ROTATED_ON_ROLE_CHANGE,
    ACTION_WORKER_USER_INACTIVE,
    ACTION_WORKER_IDENTITY_SPOOF_REJECT,
)



# ---------------------------------------------------------------------
# Resource type constants — the kind of thing that got mutated.
# Same rationale as actions: strings, not a PG enum, advisory.
# ---------------------------------------------------------------------

RESOURCE_TENANT = "tenant_config"
RESOURCE_DOMAIN = "domain_config"
RESOURCE_AGENT = "agent"
RESOURCE_LUCIEL_INSTANCE = "luciel_instance"
RESOURCE_API_KEY = "api_key"
RESOURCE_KNOWLEDGE = "knowledge_embedding"  # Step 25
RESOURCE_RETENTION_POLICY = "retention_policy"
# Step 27b: async memory extraction worker writes to memory_items
RESOURCE_MEMORY = "memory"
# Step 24.5b: User identity layer (Q6 resolution)
RESOURCE_USER = "user"
RESOURCE_SCOPE_ASSIGNMENT = "scope_assignment"
# Step 25b — knowledge ingestion
RESOURCE_KNOWLEDGE = "knowledge"

ACTION_KNOWLEDGE_INGEST = "knowledge_ingest"
ACTION_KNOWLEDGE_REPLACE = "knowledge_replace"
ACTION_KNOWLEDGE_DELETE = "knowledge_delete"

ALLOWED_RESOURCE_TYPES = (
    RESOURCE_TENANT,
    RESOURCE_DOMAIN,
    RESOURCE_AGENT,
    RESOURCE_LUCIEL_INSTANCE,
    RESOURCE_API_KEY,
    RESOURCE_KNOWLEDGE,
    RESOURCE_RETENTION_POLICY,
    # Step 27b: async memory extraction worker writes to memory_items
    RESOURCE_MEMORY,
    # Step 24.5b
    RESOURCE_USER,
    RESOURCE_SCOPE_ASSIGNMENT,
)


class AdminAuditLog(Base, TimestampMixin):
    __tablename__ = "admin_audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # -----------------------------------------------------------------
    # WHO — actor identity captured safely
    # -----------------------------------------------------------------

    # 12-character prefix of the API key that performed the action.
    # NEVER the raw key. Matches the same prefix we store on ApiKey
    # rows, so admins can correlate without any secret exposure.
    # Nullable for system-initiated actions (e.g. retention cascade
    # cleanups, background jobs).
    actor_key_prefix: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        index=True,
        comment="12-char API key prefix (e.g. 'lucskCX3R...'), never the raw key. "
                "NULL for system actions.",
    )

    # Comma-separated permission list at the time of the action.
    # Captured verbatim so an auditor can see 'the caller had admin
    # at this moment' even if the key is later re-scoped or deleted.
    actor_permissions: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="Comma-separated permissions held by the actor at action time.",
    )

    # Optional — free-form actor hint. Populated from ApiKey.created_by
    # if available ('aryan', 'remax-it', etc.). Purely informational;
    # actor_key_prefix is the authoritative identity.
    actor_label: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # -----------------------------------------------------------------
    # WHERE — scope the action happened in
    # -----------------------------------------------------------------

    # tenant_id is always set. System actions with no tenant (unusual)
    # use the literal string 'platform' to preserve the NOT NULL
    # invariant and make 'WHERE tenant_id = :x' queries cheap.
    tenant_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
    )
    domain_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True
    )
    agent_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True
    )
    luciel_instance_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True,
        comment="PK of the luciel_instances row, when applicable.",
    )

    # -----------------------------------------------------------------
    # WHAT — action + resource
    # -----------------------------------------------------------------

    action: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        index=True,
        comment="See ALLOWED_ACTIONS.",
    )
    resource_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        comment="See ALLOWED_RESOURCE_TYPES.",
    )

    # Primary key of the affected row in its own table (e.g.
    # luciel_instances.id). Nullable because some actions — bulk
    # cascades — don't target a single row.
    resource_pk: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Natural identifier of the resource when it has one
    # (agent_id slug, tenant_id slug, instance_id slug, key_prefix).
    # Makes log queries human-readable without joins.
    resource_natural_id: Mapped[str | None] = mapped_column(
        String(200), nullable=True, index=True
    )

    # -----------------------------------------------------------------
    # DIFF — what changed
    # -----------------------------------------------------------------

    # For create: before=NULL, after = full row snapshot.
    # For update: before = old values of changed fields only,
    #             after  = new values of changed fields only.
    # For deactivate: before = {"active": true}, after = {"active": false}.
    # For cascade:   before=NULL, after = {"count": N, "reason": "..."}.
    # Size stays small because we never snapshot unchanged fields.
    before_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Optional human note -- e.g. "cascade from domain deactivate",
    # "replace by sourceId=foo.pdf v=3", etc.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # -----------------------------------------------------------------
    # CHAIN -- Step 28 P3-E.2 (Pillar 23). Tamper-evidence.
    # -----------------------------------------------------------------
    #
    # row_hash       = sha256(canonical_content + prev_row_hash)
    # prev_row_hash  = the row_hash of the row with the next-lower id;
    #                  genesis row uses '0' * 64.
    #
    # Both columns are populated by the SQLAlchemy session event in
    # app.repositories.audit_chain (registered at app startup). They
    # are NULLABLE in the schema to tolerate the deploy window where
    # an old container image without the event still runs; Pillar 23
    # FAILs if any row has NULL hashes after deploy completion.
    #
    # Never modify these columns after insert. The migration that
    # added them (8ddf0be96f44) deliberately did NOT grant UPDATE on
    # admin_audit_logs to luciel_worker, so a compromised worker
    # cannot rewrite the chain.
    row_hash: Mapped[str | None] = mapped_column(
        CHAR(64), nullable=True, unique=True,
        comment="sha256 hex of canonical_content + prev_row_hash; "
                "NULLABLE for deploy-window tolerance.",
    )
    prev_row_hash: Mapped[str | None] = mapped_column(
        CHAR(64), nullable=True,
        comment="row_hash of the prior row in id ASC order; "
                "genesis = '0'*64.",
    )

    __table_args__ = (
        # The query we care about most: "show me all admin actions for
        # tenant X in time range Y" for a tenant audit dashboard.
        Index(
            "ix_admin_audit_logs_tenant_time",
            "tenant_id",
            "created_at",
        ),
        # "Show me everything this actor has done" — key prefix + time.
        Index(
            "ix_admin_audit_logs_actor_time",
            "actor_key_prefix",
            "created_at",
        ),
        # "What happened to this specific resource over time."
        Index(
            "ix_admin_audit_logs_resource",
            "resource_type",
            "resource_pk",
            "created_at",
        ),
        {"comment": "Step 24.5 — durable admin mutation audit trail."},
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return (
            f"<AdminAuditLog id={self.id} actor={self.actor_key_prefix} "
            f"tenant={self.tenant_id} {self.action}:{self.resource_type} "
            f"pk={self.resource_pk}>"
        )