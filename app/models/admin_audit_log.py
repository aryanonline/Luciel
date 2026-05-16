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
- WHO:   actor_key_prefix + actor_permissions JSON (never the raw key)
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
# Step 29.y Cluster 4 (E-3): permanent (non-transient) task
# failures route through _reject_with_audit using this action,
# so they land in DLQ deterministically and emit a single
# rejection audit row covered by the
# ux_admin_audit_logs_worker_reject_idem partial unique index.
ACTION_WORKER_PERMANENT_FAILURE = "worker_permanent_failure"
# Step 24.5b: Q6 resolution -- mandatory key rotation cascade on role change.
# Distinct from ACTION_DEACTIVATE because the key was active+valid; this
# captures "key was good but the User's scope assignment ended, so we
# rotated as a security cascade." Auditors filter on this to find all
# role-change-induced revocations.
ACTION_KEY_ROTATED_ON_ROLE_CHANGE = "key_rotated_on_role_change"

# Step 28 C8 (P3-O): durable record for memory extractor save-time failures.
# The chat turn must NOT fail when memory persistence fails (fail-open
# contract), but fail-open is not the same as fail-silent. Every save-time
# exception writes one of these rows so compliance has a record beyond the
# transient warning log. exc_type, exc_repr, session_id, message_id,
# category go into after_json. resource_type=RESOURCE_MEMORY.
ACTION_EXTRACTOR_SAVE_FAIL = "extractor_save_fail"

# Step 29 Commit C.5: forensic toggle of luciel_instances.active via
# the platform_admin POST endpoint at
#   /api/v1/admin/forensics/luciel_instances_step29c/{instance_id}/toggle_active
# This is intentionally distinct from ACTION_DEACTIVATE / ACTION_REACTIVATE
# (which are the operational deactivate/reactivate verbs used across
# tenants, api_keys, memory items, scope assignments, etc., disambiguated
# only by resource_type). The forensic toggle is NOT an operational
# admin action -- it is a verify-harness fixture lever exposed through
# the same platform_admin gate as the C.1-C.4 forensic GETs, used by
# pillar_11_async_memory.py F10 to set up its instance-liveness Gate-4
# assertion and to restore the prior state when the assertion finishes.
# Giving it its own ACTION constant keeps the audit trail honest: a
# compliance auditor scanning admin_audit_log for operational
# deactivations of LucielInstance rows will not see harness traffic
# mixed in, and a future incident response that needs to find harness
# fingerprints can filter on this exact action.
ACTION_LUCIEL_INSTANCE_FORENSIC_TOGGLE = "luciel_instance_forensic_toggle"

# Step 29.y Cluster 1 (G-3 resolution): consent grant/withdraw/status are
# PIPEDA-significant mutations. Every state change must leave an audit row
# so a regulator can reconstruct who consented to what and when. Distinct
# verbs (rather than reusing ACTION_CREATE/ACTION_DEACTIVATE) keep the
# audit log searchable by the actual user-facing semantic ("the user
# granted/withdrew consent to memory persistence"), not the implementation
# detail ("a row was created/updated in user_consents"). Status reads do
# NOT audit -- they are read-only and the volume would noise the trail,
# matching the same convention applied to GET /admin/verification.
ACTION_CONSENT_GRANT = "consent_grant"
ACTION_CONSENT_WITHDRAW = "consent_withdraw"

# Step 29.y Cluster 1 (G-4 resolution): a platform_admin key creating a
# session under a tenant other than its own (or with no tenant binding
# of its own) is a privileged cross-tenant operation. It is rare and
# legitimate (verify suite, support tooling), but it must always leave
# an audit row so the privileged use is auditable. Tenant-scoped callers
# never trigger this -- their session creation is constrained to their
# own tenant by the same scope check that guards every other write route.
ACTION_SESSION_CREATE_CROSS_TENANT = "session_create_cross_tenant"

# Step 29.y Cluster 1 (G-5 resolution): retention policies are PIPEDA
# legal-compliance artifacts. Mutations to them MUST leave an audit row;
# enforcement runs (the periodic delete-aged-rows job) ALSO leave one
# per policy applied. ACTION_CREATE/ACTION_UPDATE/ACTION_DELETE_HARD are
# reused for the CRUD verbs against RESOURCE_RETENTION_POLICY -- only
# the enforcement and manual-purge verbs need new constants because
# they are operationally distinct from "a human edited a policy."
ACTION_RETENTION_ENFORCE = "retention_enforce"
ACTION_RETENTION_MANUAL_PURGE = "retention_manual_purge"

# Step 30a: subscription billing.
#
# These verbs are deliberately separate from ACTION_CREATE / ACTION_UPDATE /
# ACTION_DEACTIVATE so that a compliance auditor scanning the audit chain
# for billing events can filter on the verb alone. Subscription mutations
# are PIPEDA-significant (financial state + entitlement) AND are emitted
# almost exclusively by the Stripe webhook path -- so the verb captures
# the operational origin even when the actor key prefix is the system
# webhook context.
#
# ACTION_SUBSCRIPTION_CREATE     -- first row written when a Stripe
#                                   checkout.session.completed event
#                                   mints a tenant + subscription pair.
# ACTION_SUBSCRIPTION_UPDATE     -- customer.subscription.updated
#                                   (plan change, trial->active, cancel-
#                                   at-period-end flip).
# ACTION_SUBSCRIPTION_CANCEL     -- customer.subscription.deleted; pairs
#                                   with the existing tenant deactivation
#                                   cascade in ARCHITECTURE §4.5.
# ACTION_SUBSCRIPTION_REACTIVATE -- not emitted at v1 (Stripe treats
#                                   reactivation as a new subscription),
#                                   reserved so a future flow can use it
#                                   without a follow-up migration.
# ACTION_BILLING_WEBHOOK_REPLAY_REJECTED
#                                -- a Stripe event whose stripe_event_id
#                                   was already applied. Recorded so the
#                                   replay attempt is visible (and
#                                   distinguishable from a successful
#                                   apply) but does not mutate state.
ACTION_SUBSCRIPTION_CREATE = "subscription_create"
ACTION_SUBSCRIPTION_UPDATE = "subscription_update"
ACTION_SUBSCRIPTION_CANCEL = "subscription_cancel"
ACTION_SUBSCRIPTION_REACTIVATE = "subscription_reactivate"
ACTION_BILLING_WEBHOOK_REPLAY_REJECTED = "billing_webhook_replay_rejected"

# Step 30a.2-pilot -- self-serve refund of the one-time $100 CAD intro
# fee charged at signup. Distinct from ACTION_SUBSCRIPTION_CANCEL
# because:
#   (a) the actor is the buyer (a cookied User), NOT the Stripe webhook,
#       so an auditor can answer "who initiated this teardown -- the
#       customer or Stripe?" by filtering on actor_label;
#   (b) the financial side effect is a refund row in Stripe AND a
#       cancel, not just a cancel -- the after_json carries
#       stripe_refund_id + intro_charge_id + refunded_amount_cents +
#       currency so the audit trail is the single source of truth for
#       the $100 returning to the buyer's card;
#   (c) only the FIRST-TIME path is eligible (see
#       BillingService.process_pilot_refund eligibility predicate); the
#       distinct verb makes that policy auditable at the verb level
#       rather than only inside note/JSON.
# The action string is 28 chars and fits in the existing
# admin_audit_logs.action String(64) column (Step 29x widened from 30
# to 64). No schema migration required.
ACTION_SUBSCRIPTION_PILOT_REFUNDED = "subscription_pilot_refunded"

# Step 30a.2-pilot Commit 3j -- post-refund customer-email send failure.
# Written as a follow-up audit row by BillingService.process_pilot_refund
# when the courtesy SES send to the buyer's customer_email raises
# RefundEmailError. The refund / cancel / cascade have ALREADY been
# committed at this point -- the email is the out-of-band confirmation
# leg, NOT a transactional leg -- so this row exists to give an operator
# a paper trail to manually retry the email without ambiguity about
# whether the financial refund itself fired. after_json carries
# {stripe_refund_id, error_class, error_message_truncated, to_email}.
ACTION_PILOT_REFUND_EMAIL_SEND_FAILED = "pilot_refund_email_send_failed"

# Step 30a.2 -- retention worker hard-purge action. See ALLOWED_ACTIONS
# entry for full rationale. Defined here (above ALLOWED_ACTIONS) so the
# whitelist tuple can reference it.
ACTION_TENANT_HARD_PURGED = "tenant_hard_purged"

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
    ACTION_WORKER_PERMANENT_FAILURE,  # Step 29.y Cluster 4 (E-3)
    # Step 28 C8 (P3-O): extractor save-time failure record.
    ACTION_EXTRACTOR_SAVE_FAIL,
    # Step 29 Commit C.5: forensic-only toggle of luciel_instances.active.
    ACTION_LUCIEL_INSTANCE_FORENSIC_TOGGLE,
    # Step 29.y Cluster 1
    ACTION_CONSENT_GRANT,
    ACTION_CONSENT_WITHDRAW,
    ACTION_SESSION_CREATE_CROSS_TENANT,
    ACTION_RETENTION_ENFORCE,
    ACTION_RETENTION_MANUAL_PURGE,
    # Step 30a -- subscription billing
    ACTION_SUBSCRIPTION_CREATE,
    ACTION_SUBSCRIPTION_UPDATE,
    ACTION_SUBSCRIPTION_CANCEL,
    ACTION_SUBSCRIPTION_REACTIVATE,
    ACTION_BILLING_WEBHOOK_REPLAY_REJECTED,
    # Step 30a.2-pilot -- self-serve refund of the one-time intro fee.
    ACTION_SUBSCRIPTION_PILOT_REFUNDED,
    # Step 30a.2-pilot Commit 3j -- best-effort courtesy email failure row.
    ACTION_PILOT_REFUND_EMAIL_SEND_FAILED,
    # Step 30a.2 -- retention worker hard-purge action. Distinct from
    # ACTION_RETENTION_ENFORCE (which the existing retention policy code
    # uses for per-record TTL expiry on memory_items) because tenant-
    # level hard-purge crosses ten tables atomically and the row count
    # + table manifest belongs in a structurally distinct action so
    # dashboards can tell them apart at-a-glance.
    ACTION_TENANT_HARD_PURGED,
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
# Step 29.y Cluster 1: user_consents row is the auditable resource for
# consent grant/withdraw. Distinct from RESOURCE_USER (which is the
# platform User identity row, not the per-feature consent record).
RESOURCE_CONSENT = "consent"
# Step 29.y Cluster 1: sessions table -- scoped audit trail for the
# privileged cross-tenant creation case (G-4). The session itself is
# not normally audited (ordinary chat traffic is not an admin action),
# only the cross-tenant creation by a platform_admin key is.
RESOURCE_SESSION = "session"
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

# Step 30a: subscription rows on the new `subscriptions` table. Distinct
# from RESOURCE_TENANT because a single tenant lifecycle can span multiple
# subscription rows (Pattern E -- cancel + resubscribe leaves an inactive
# row + an active row, both pointing at the same tenant_id).
RESOURCE_SUBSCRIPTION = "subscription"

# Step 30a.2 -- cascade extension closes
# D-cancellation-cascade-incomplete-conversations-claims-2026-05-14.
# Both rows are PII-bearing under PIPEDA Principle 5: a conversation
# carries the full chat history surface; an identity_claim carries
# (claim_type, claim_value) where claim_value may be an email address
# or phone number. Soft-deactivation must be audited per the same
# rules as luciel_instances and api_keys; hard-purge at retention
# time emits ACTION_RETENTION_ENFORCE rows referencing these resource
# types so the audit chain remains complete after the data itself is
# gone.
RESOURCE_CONVERSATION = "conversation"
RESOURCE_IDENTITY_CLAIM = "identity_claim"

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
    # Step 29.y Cluster 1
    RESOURCE_CONSENT,
    RESOURCE_SESSION,
    # Step 30a -- subscription billing
    RESOURCE_SUBSCRIPTION,
    # Step 30a.2 -- cascade extension + retention worker
    RESOURCE_CONVERSATION,
    RESOURCE_IDENTITY_CLAIM,
)

# Step 29.y gap-fix C2 (D-audit-note-length-unbounded-2026-05-07):
# AdminAuditLog.note is a Text column, so the database does not
# bound its length. An accidental dump (e.g. an exception message,
# a user-supplied string concatenated into a note, a debug payload)
# would bloat audit rows and -- because note is part of the hash
# chain canonical content (audit_chain.py::_CHAIN_FIELDS) -- it
# also factors into row_hash. Unbounded inputs make the hash input
# size unpredictable, which makes per-row hash latency unpredictable.
#
# This cap is enforced at the AdminAuditRepository.record() boundary
# (the single chokepoint for all audit writes; the rotate-keys script
# constructs AdminAuditLog directly and is in scope of the chain
# event handler but writes a fixed short note). 256 chars matches
# the existing convention in app/api/v1/admin.py:1845 (end_note=500)
# scaled down for the more frequent operational note path. Callers
# that legitimately need to attach a large payload should use the
# before_json / after_json diff fields, which are JSON-typed and
# already bounded by Postgres jsonb size limits.
#
# We do NOT migrate the column to String(256) in this code-only
# session: that would require a NULL/length backfill against prod
# data and is sequenced for Step 30b alongside the recap rewrite.
MAX_NOTE_LENGTH = 256


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

    # Permission list at the time of the action, captured verbatim so an
    # auditor can see 'the caller had admin at this moment' even if the
    # key is later re-scoped or deleted.
    #
    # On-disk format (Step 29.y gap-fix C1,
    # D-actor-permissions-comma-fragility-2026-05-07):
    #   - NEW rows: JSON of a sorted list of strings,
    #     e.g. '["admin","worker"]'.
    #   - OLD rows (pre-29.y gap-fix): legacy comma form,
    #     e.g. 'admin,worker'. NOT rewritten -- preserves audit hash
    #     chain (audit_chain.py / Pillar 23).
    # All read paths MUST go through
    # app.repositories.actor_permissions_format.parse_actor_permissions
    # which accepts both formats.
    actor_permissions: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment=(
            "Permissions held by the actor at action time. "
            "New rows: JSON list (e.g. '[\"admin\",\"worker\"]'); "
            "pre-29.y rows: legacy comma form. "
            "Read via actor_permissions_format.parse_actor_permissions."
        ),
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
        String(64),
        nullable=False,
        index=True,
        comment=(
            "See ALLOWED_ACTIONS. Widened from 30 to 64 in step29x "
            "(migration a1f29c7e4b08) after Pillar 24 caught a "
            "StringDataRightTruncation on the 31-char forensic toggle action."
        ),
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
    #
    # Length is capped at MAX_NOTE_LENGTH (256) at the
    # AdminAuditRepository.record() boundary (Step 29.y gap-fix C2,
    # D-audit-note-length-unbounded-2026-05-07). The column itself
    # is Text so historical rows that exceed the cap are still
    # readable; the cap is forward-only.
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
    # Step 29.y Cluster 3 (D-8): both columns are NOT NULL at the
    # DB layer post-migration c5d8a1e7b3f9. Pillar 23 probes column
    # nullability and switches to STRICT mode (zero NULL tolerance)
    # when it sees the schema flip. The Mapped type drops Optional
    # to match.
    row_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, unique=True,
        comment="sha256 hex of canonical_content + prev_row_hash; "
                "NOT NULL post Step 29.y Cluster 3 (D-8).",
    )
    prev_row_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False,
        comment="row_hash of the prior row in id ASC order; "
                "genesis = '0'*64. NOT NULL post Step 29.y "
                "Cluster 3 (D-8).",
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