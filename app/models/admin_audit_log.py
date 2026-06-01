"""
AdminAuditLog — durable, per-tenant audit trail for admin mutations.

Step 24.5 (File 6.5a). Every admin-layer create / update / deactivate
across Agent, LucielInstance, DomainConfig, TenantConfig, ApiKey, and
(from Step 25 onward) KnowledgeChunk writes exactly one row here,
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
- WHERE: admin_id / luciel_instance_id (the v2 customer-data scoping
         per Architecture §3.7.3 Wall 3; the legacy domain_id /
         agent_id columns were excised by Arc 12 EX4 via a controlled
         audit-chain reseal).
- WHAT:  action verb + resource_type + resource_pk + resource_natural_id
- DIFF:  before / after JSON — only the fields that actually changed

Not a general event bus — strictly admin mutations. Chat events stay
in Trace. Retention purges stay in DeletionLog. Consent events stay
in UserConsent.

Domain-agnostic: no vertical enums, no imports from app/domain/.
"""
from __future__ import annotations

from datetime import datetime  # Arc 10: cold_archived_at column

from sqlalchemy import CHAR, DateTime, ForeignKey, Index, Integer, String, Text
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

# -----------------------------------------------------------------
# Arc 10 (Alembic arc10_lifecycle_subsystem) — lifecycle action verbs.
# -----------------------------------------------------------------
# One verb per distinct user-visible (or audit-trail-relevant)
# lifecycle event. The verbs are advisory (no DB-side ENUM) so future
# additions land without a schema migration. Forensics queries filter
# on these strings.
ACTION_ACCOUNT_CLOSURE_INITIATED = "account_closure_initiated"
ACTION_ACCOUNT_REACTIVATED = "account_reactivated"
ACTION_ACCOUNT_HARD_DELETED = "account_hard_deleted"
ACTION_DATA_EXPORT_REQUESTED = "data_export_requested"
ACTION_DATA_EXPORT_GENERATED = "data_export_generated"
ACTION_DATA_EXPORT_FAILED = "data_export_failed"
ACTION_DATA_EXPORT_EXPIRED = "data_export_expired"
ACTION_DOWNGRADE_INITIATED = "downgrade_initiated"
ACTION_DOWNGRADE_GRACE_ENFORCED = "downgrade_grace_enforced"
ACTION_AUDIT_LOG_TIER_ARCHIVED = "audit_log_tier_archived"
ACTION_INSTANCE_DEACTIVATED = "instance_deactivated"
ACTION_KNOWLEDGE_SOFT_DELETED = "knowledge_soft_deleted"
ACTION_KNOWLEDGE_HARD_DELETED = "knowledge_hard_deleted"

# Arc 11 Closeout PR-A -- instance lifecycle verbs per Customer Journey
# §4.5 Phase 8 (Pause / Delete / Restore as three distinct affordances).
# Distinct from ACTION_DEACTIVATE / ACTION_REACTIVATE so a regulator
# scanning the audit chain can answer "did the admin pause this or
# delete this" at the verb level without inspecting the diff. The
# verbs are advisory strings (no DB-side ENUM) per the same convention
# as every other Arc 10 / Arc 11 lifecycle action above.
#
# ACTION_INSTANCE_PAUSED       -- operational quiet. Widget renders
#                                 empty <div>; data retained; reactivat-
#                                 able instantly via /resume.
# ACTION_INSTANCE_RESUMED      -- exit Pause; widget begins serving
#                                 again. No key rotation.
# ACTION_INSTANCE_DELETED      -- destructive intent. soft_deleted_at
#                                 stamped; 30-day grace window opens.
#                                 Restorable via /restore.
# ACTION_INSTANCE_RESTORED     -- exit Delete within the 30-day window.
#                                 Per Vision §6.4 embed keys are re-
#                                 minted (new keys, old keys stay
#                                 revoked). The audit row's after_json
#                                 carries both the revoked key prefixes
#                                 and the new key prefix.
# ACTION_INSTANCE_HARD_PURGED  -- retention worker hard-deleted an
#                                 instance + its cascade (knowledge,
#                                 conversations, leads, traces, api_keys)
#                                 after the 30-day grace expired.
#                                 Distinct from ACTION_TENANT_HARD_PURGED
#                                 because the tenant survives.
ACTION_INSTANCE_PAUSED = "instance_paused"
ACTION_INSTANCE_RESUMED = "instance_resumed"
ACTION_INSTANCE_DELETED = "instance_deleted"
ACTION_INSTANCE_RESTORED = "instance_restored"
ACTION_INSTANCE_HARD_PURGED = "instance_hard_purged"

# Arc 11 Step 7 -- admin knowledge-base routes
# (/admin/instances/{instance_id}/knowledge/*). Distinct from the
# pre-Arc-11 ACTION_KNOWLEDGE_* actions above (which are emitted by
# the legacy /admin/knowledge/ingest path and stay in place until
# Arc 14 retires it). The new actions are emitted by the routes in
# app/api/v1/admin_knowledge.py — one per CRUD verb plus the
# affected-questions read and the crawl enqueue.
ACTION_KNOWLEDGE_SOURCE_CREATED = "knowledge_source_created"
ACTION_KNOWLEDGE_SOURCE_LISTED = "knowledge_source_listed"
ACTION_KNOWLEDGE_SOURCE_VIEWED = "knowledge_source_viewed"
ACTION_KNOWLEDGE_SOURCE_UPDATED = "knowledge_source_updated"
ACTION_KNOWLEDGE_SOURCE_DELETED = "knowledge_source_deleted"
ACTION_KNOWLEDGE_AFFECTED_QUESTIONS_VIEWED = "knowledge_affected_questions_viewed"
ACTION_KNOWLEDGE_CRAWL_ENQUEUED = "knowledge_crawl_enqueued"

# Step 28 C8 (P3-O): durable record for memory extractor save-time failures.
# The chat turn must NOT fail when memory persistence fails (fail-open
# contract), but fail-open is not the same as fail-silent. Every save-time
# exception writes one of these rows so compliance has a record beyond the
# transient warning log. exc_type, exc_repr, session_id, message_id,
# category go into after_json. resource_type=RESOURCE_MEMORY.
ACTION_EXTRACTOR_SAVE_FAIL = "extractor_save_fail"

# Step 29 Commit C.5: forensic toggle of luciel_instances.active via
# the platform_admin POST endpoint at
#   /api/v1/admin/forensics/instances_step29c/{instance_id}/toggle_active
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

# Arc 6 Commit 8.5b -- deferred-downgrade lifecycle. Two distinct verbs:
#
# ACTION_SUBSCRIPTION_DOWNGRADE_SCHEDULED
#   Emitted at schedule time by the request-path (BillingService
#   .schedule_downgrade), when the buyer clicks "downgrade" in the
#   Account UI. The actor is the cookied User (not the Stripe webhook),
#   the before_json carries the current tier, after_json carries the
#   pending_downgrade_target + Stripe's reported current_period_end
#   as the boundary. Stripe-side state is now
#   cancel_at_period_end=True; nothing on the entitlement surface has
#   changed yet -- the buyer keeps the tier they paid for until the
#   boundary fires.
#
# ACTION_SUBSCRIPTION_DOWNGRADE_APPLIED
#   Emitted by the webhook V2 branch of _on_subscription_deleted when
#   Stripe fires the boundary event. The actor is the synthetic
#   stripe_webhook context. The after_json carries the per-axis
#   overflow archive tally + the archived_at timestamp. Distinct from
#   ACTION_SUBSCRIPTION_CANCEL (V1 hard-cancel deactivate) so a
#   dashboard query can tell a paid-tier downgrade-to-Free apart from
#   an admin-deactivation cascade at the verb level.
ACTION_SUBSCRIPTION_DOWNGRADE_SCHEDULED = "subscription_downgrade_scheduled"
ACTION_SUBSCRIPTION_DOWNGRADE_APPLIED = "subscription_downgrade_applied"

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

# Step 30a.4 -- first-class invite lifecycle for Team and Company tiers.
#
# These verbs are deliberately separate from ACTION_CREATE / ACTION_UPDATE /
# ACTION_DEACTIVATE so that a compliance auditor scanning the audit chain
# for invite events can filter on the verb alone. The four-event invite
# arc -- issue, redeem, resend (rotate token), revoke -- is structurally
# distinct from the generic CRUD verbs:
#
# ACTION_USER_INVITED       -- a tenant admin minted a UserInvite row,
#                              triggering the welcome-set-password email.
#                              actor_label = the inviting User's email.
# ACTION_INVITE_REDEEMED    -- invitee redeemed the set_password token,
#                              provisioning User + Agent + ScopeAssignment
#                              in the same DB transaction.
# ACTION_INVITE_RESENT      -- the tenant admin clicked Resend on a
#                              still-pending invite; token_jti rotated,
#                              new 24h JWT minted, same invite row.
#                              Distinct from ACTION_UPDATE because the
#                              row mutation is purely a security rotation,
#                              not a content edit.
# ACTION_INVITE_REVOKED     -- the tenant admin cancelled a still-pending
#                              invite. Distinct from ACTION_DEACTIVATE
#                              because invites are single-use and have
#                              their own status enum (pending -> revoked
#                              is a terminal transition, not a soft-delete
#                              of an active record).
ACTION_USER_INVITED = "user_invited"
ACTION_INVITE_REDEEMED = "invite_redeemed"
ACTION_INVITE_RESENT = "invite_resent"
ACTION_INVITE_REVOKED = "invite_revoked"

# Step 30a.5 -- Company-tier org-building: Domain creation and
# deactivation from the cookied self-serve route. Distinct from the
# generic ACTION_CREATE/ACTION_DEACTIVATE on the admin-key route
# (POST/DELETE /admin/domains) because:
#   (a) the self-serve path enforces the per-tier Domain cap and the
#       audit row's details field records {cap, used} at write time so
#       dashboards can see cap pressure historically;
#   (b) the actor_label resolves to the cookied User's email rather
#       than an API-key id, and downstream dashboards filter on these
#       verbs to surface customer-driven (not operator-driven) org
#       changes.
ACTION_DOMAIN_CREATED = "domain_created"
ACTION_DOMAIN_DEACTIVATED = "domain_deactivated"

# Arc 8 WU-6 -- SES feedback / suppression cohort.
#
# These verbs are deliberately separate from ACTION_CREATE / ACTION_DELETE_HARD
# on a generic resource so a compliance auditor (and the AWS sandbox-exit
# reviewer) can filter the audit chain on the verb alone and see the full
# deliverability story:
#
# ACTION_EMAIL_SUPPRESSION_RECORDED  -- an address was added to the
#                                       application-layer suppression list.
#                                       after_json carries
#                                       {reason, source_event_id, address}.
#                                       Emitted by EmailSuppressionService
#                                       inside the same transaction as the
#                                       EmailSuppression INSERT so the chain
#                                       guarantees atomicity (Invariant 4).
# ACTION_EMAIL_SUPPRESSION_CLEARED   -- an operator-initiated removal of a
#                                       suppression row. The address is now
#                                       sendable again. before_json carries
#                                       the cleared row's {reason,
#                                       source_event_id, first_suppressed_at}
#                                       so the chain preserves the history
#                                       even though the working-state row is
#                                       hard-deleted.
# ACTION_EMAIL_SEND_EVENT_RECEIVED   -- the SES feedback route accepted an
#                                       SNS-delivered event and persisted it
#                                       to email_send_event. Not every event
#                                       triggers a suppression (only Bounce
#                                       with bounceType=Permanent and
#                                       Complaint do), but every event
#                                       received is auditable so a sandbox-
#                                       exit reviewer can verify the loop
#                                       is live. after_json carries
#                                       {event_type, event_id, address?}.
#
# Resource types accompany these verbs:
#   RESOURCE_EMAIL_SUPPRESSION pairs with ACTION_EMAIL_SUPPRESSION_RECORDED
#       and ACTION_EMAIL_SUPPRESSION_CLEARED.
#   RESOURCE_EMAIL_SEND_EVENT pairs with ACTION_EMAIL_SEND_EVENT_RECEIVED.
ACTION_EMAIL_SUPPRESSION_RECORDED = "email_suppression_recorded"
ACTION_EMAIL_SUPPRESSION_CLEARED = "email_suppression_cleared"
ACTION_EMAIL_SEND_EVENT_RECEIVED = "email_send_event_received"

# Arc 12 WU4 -- sibling-Luciel composition grant lifecycle.
#
# A sibling-call grant authorises one Instance to invoke
# ``call_sibling_luciel`` against another Instance under the same
# Admin (Architecture v1 §3.3.4). The four verbs below are the full
# auditable arc:
#
# ACTION_SIBLING_GRANT_AUTHORED  -- a user authored a new grant.
#   On Pro, the grant lands ``approval_state='live'`` immediately;
#   on Enterprise, ``approval_state='pending_approval'`` until an
#   admin_owner approves. The author MUST hold role+scope on BOTH
#   the caller and the callee Instance (Wall 2 at the sibling layer).
# ACTION_SIBLING_GRANT_APPROVED  -- an admin_owner approved a
#   pending Enterprise grant, flipping ``approval_state`` to
#   ``live`` and stamping approved_by_user_id + approved_at.
#   Distinct from ACTION_SIBLING_GRANT_AUTHORED so a regulator
#   scanning the chain by verb can distinguish "who authored" from
#   "who approved" -- the Wall-2 split between proposal and ratify.
# ACTION_SIBLING_GRANT_REJECTED  -- an admin_owner rejected a
#   pending grant. Distinct from ACTION_SIBLING_GRANT_REVOKED
#   because reject is a pre-live terminal transition (the grant
#   never went live), whereas revoke withdraws an already-live (or
#   pending) grant. Both flip ``approval_state`` to 'revoked' and
#   stamp revoked_at, but the verb captures the difference.
# ACTION_SIBLING_GRANT_REVOKED   -- a live or pending grant was
#   withdrawn. Emitted by the explicit revoke API AND by the
#   instance-deactivation cascade (§3.6.1 step 3): when an Instance
#   is deactivated, every grant where the Instance appears as
#   caller OR callee flips to 'revoked' in the same transaction
#   with one of these audit rows per grant. The before/after_json
#   carries the cascade source so an auditor can distinguish
#   operator-revoke from cascade-revoke.
ACTION_SIBLING_GRANT_AUTHORED = "sibling_grant_authored"
ACTION_SIBLING_GRANT_APPROVED = "sibling_grant_approved"
ACTION_SIBLING_GRANT_REJECTED = "sibling_grant_rejected"
ACTION_SIBLING_GRANT_REVOKED = "sibling_grant_revoked"

# Arc 12 WU2b -- per-instance tool authorization admin API
# (Architecture §3.3.1/§3.3.2 catalog + WU2 default-deny gate).
#
# ACTION_TOOL_AUTHORIZED -- a user authorised one of the 8 v1 catalog
#   tools on an Instance via the admin tools API. Emits a row whose
#   resource_type=RESOURCE_INSTANCE_TOOL_AUTHORIZATION, resource_pk
#   = the new instance_tool_authorizations.id, resource_natural_id
#   = "{instance_id}:{tool_id}" so an auditor can answer "every event
#   for tool X on Instance Y" with a single filter. after_json carries
#   {tool_id, enabled, authorized_by_user_id, tier_at_authorize}.
#   Wall-1 + Wall-3 are scoped via (admin_id, instance_id); Wall-2
#   (role gate) is enforced at the route layer (owner/manager only).
# ACTION_TOOL_REVOKED -- the symmetric verb for the soft-revoke of a
#   live authorization row. before_json carries the prior {enabled}
#   state; after_json carries {revoked_at}. The admin tools API is
#   idempotent against missing rows: a revoke against no-live-row is
#   a 404 surfaced to the operator (not an audit emission).
ACTION_TOOL_AUTHORIZED = "tool_authorized"
ACTION_TOOL_REVOKED = "tool_revoked"

# Arc 12 EX4 (founder-directed, LOCKED 2026-05-28) -- audit-chain reseal.
#
# ACTION_AUDIT_CHAIN_RESEALED -- emitted by the
#   ``arc12_ex4_reseal_audit_chain_drop_agent_domain`` migration AFTER it
#   recomputes row_hash/prev_row_hash for every historical
#   admin_audit_logs row under the new canonical _CHAIN_FIELDS set (which
#   omits the now-dropped domain_id/agent_id columns). The reseal record
#   is itself chained under the NEW field set so the rewrite is
#   traceable: actor = the migration runtime; after_json carries the
#   row-count, the OLD vs. NEW field-set diff, and the reseal rationale
#   ("Arc 12 EX4 founder decision; v1 three-layer scaffold excised").
#   Distinct from every other action because the "actor" is the
#   migration itself (no API caller), and because the row is the FIRST
#   row in the v2 chain (its prev_row_hash equals the row_hash of the
#   last v1 historical row AS RESEALED, not the v1 original).
ACTION_AUDIT_CHAIN_RESEALED = "audit_chain_resealed"

# Arc 12b — Enterprise custom-role authoring lifecycle (Architecture §3.7.2).
# Distinct verbs per lifecycle event so auditors can filter on each:
#   ACTION_CUSTOM_ROLE_AUTHORED — admin_owner created a new custom role.
#   ACTION_CUSTOM_ROLE_UPDATED  — display_name / description / permission
#                                 set changed on an existing custom role.
#   ACTION_CUSTOM_ROLE_REVOKED  — soft-deleted (revoked_at set).
#   ACTION_USER_ROLE_ASSIGNED   — user assigned to a role (locked or
#                                 custom) at instance or admin scope.
#   ACTION_USER_ROLE_REVOKED    — user_role_assignment soft-revoked.
ACTION_CUSTOM_ROLE_AUTHORED = "custom_role_authored"
ACTION_CUSTOM_ROLE_UPDATED = "custom_role_updated"
ACTION_CUSTOM_ROLE_REVOKED = "custom_role_revoked"
ACTION_USER_ROLE_ASSIGNED = "user_role_assigned"
ACTION_USER_ROLE_REVOKED = "user_role_revoked"

# Arc 12 WU5 -- sibling-Luciel composition runtime dispatch.
#
# ACTION_SIBLING_ACCESS -- emitted by ``app.tools.sibling_dispatch`` on
#   every authorised invocation of ``call_sibling_luciel`` AFTER the
#   five-check dispatch path has passed (cycle detection, fan-out
#   budget, master switch on both endpoints, live grant lookup). This
#   is the Wall-3 composition exception row (§3.7.3): a dispatch that
#   names BOTH the caller and the callee instance under one admin.
#   ``luciel_instance_id`` carries the CALLER instance (the originating
#   Luciel for this hop). ``after_json`` carries the callee, the
#   inbound_message_id chaining customer-message -> sibling-invocations
#   -> final-response, the depth in the call stack, the fan-out
#   counter after this hop, and the live grant id. The audit row is
#   written BEFORE the Arc-14 orchestrator round-trip plug-in seam
#   so a regulator scanning the chain can reconstruct the composition
#   tree even if the round-trip itself is interim-bodied.
ACTION_SIBLING_ACCESS = "sibling_access"

# Arc 5 B5 -- V2 Admin/Instance lifecycle verbs.
#
# Per the aggressive-cleanup amendment
# (D-arc5-aggressive-cleanup-doctrine-amendment-2026-05-23): once Arc 5
# Revisions A+B+C finish landing the Admin -> Instance collapse, all
# new-row provisioning emits ACTION_ADMIN_CREATED / ACTION_INSTANCE_CREATED
# instead of ACTION_CREATE on the generic tenant_config / luciel_instance
# resource. The legacy ACTION_CREATE rows in the audit chain remain
# walkable; new rows take the more specific verb so a regulator scanning
# the chain by verb can locate Admin lifecycle events without scanning
# every CREATE row.
#
# ACTION_TIER_RENAME_APPLIED -- emitted by Revision B for each Admin
#   row whose legacy tier (individual/solo/team/company) was rewritten to
#   the V2 vocabulary (pro/enterprise). after_json carries
#   {from_tier_source, to_tier, migration}. The audit chain therefore
#   preserves the legacy tier vocabulary visibility even after Revision C
#   tightens the CHECK constraint to ('free','pro','enterprise').
#
# ACTION_LEGACY_FIXTURE_PURGED -- emitted by Revision B (one bulk row per
#   legacy table) summarizing the inactive-fixture purge that Revision C
#   will execute. after_json carries
#   {table, inactive_count, active_count, earliest_created_at,
#    latest_created_at}. This is the forensic recoverability surface for
#   Revision C's wholesale table drops.
ACTION_ADMIN_CREATED = "admin_created"
ACTION_INSTANCE_CREATED = "instance_created"
ACTION_TIER_RENAME_APPLIED = "tier_rename_applied"
ACTION_LEGACY_FIXTURE_PURGED = "legacy_fixture_purged"

# Arc 13 — channel adapters (email + SMS) lifecycle + runtime verbs.
#
# Channel-config mutations (enable/disable email or SMS on an Instance),
# number provisioning/deprovisioning, and every store-and-forward
# inbound/outbound event are auditable. Distinct verbs so a regulator
# (and the deliverability reviewer) can reconstruct the channel story
# at the verb level:
#
# ACTION_CHANNEL_ENABLED / ACTION_CHANNEL_DISABLED
#     A user toggled a channel on/off for an Instance via the channel
#     admin API. after_json carries {channel, enabled_channels}.
# ACTION_CHANNEL_NUMBER_PROVISIONED / ACTION_CHANNEL_NUMBER_DEPROVISIONED
#     The provisioning service bought+wired (or released) an SMS number.
#     after_json carries {number, mode, provider}.
# ACTION_CHANNEL_INBOUND_RECEIVED
#     An authentic inbound email/SMS turn was accepted and routed.
# ACTION_CHANNEL_INBOUND_DROPPED
#     An authentic inbound turn could NOT be routed to a live Instance
#     (UnresolvableInboundError) — recorded so the drop is never silent.
# ACTION_CHANNEL_OUTBOUND_DELIVERED
#     A reply was dispatched to the provider; after_json carries the
#     DeliveryReceipt {provider_message_id, status, channel}.
ACTION_CHANNEL_ENABLED = "channel_enabled"
ACTION_CHANNEL_DISABLED = "channel_disabled"
ACTION_CHANNEL_NUMBER_PROVISIONED = "channel_number_provisioned"
ACTION_CHANNEL_NUMBER_DEPROVISIONED = "channel_number_deprovisioned"
ACTION_CHANNEL_INBOUND_RECEIVED = "channel_inbound_received"
ACTION_CHANNEL_INBOUND_DROPPED = "channel_inbound_dropped"
ACTION_CHANNEL_OUTBOUND_DELIVERED = "channel_outbound_delivered"

# Arc 14 U2 — §3.4.5 escalation judgment.
#
# ACTION_ESCALATION_FIRED -- emitted by EscalationService when the
#   §3.4.5 module decides one of the four fixed signals (explicit human
#   request / strong negative sentiment / cannot confidently answer /
#   high-value lead) warrants a human handoff. Written in the same
#   transaction as the escalation_events row so the audit chain and the
#   forensic event store can never drift. resource_type =
#   RESOURCE_ESCALATION_EVENT; resource_pk = escalation_events.id;
#   after_json carries {signal, gate, signal_confidence,
#   notify_channels, tier} so a regulator scanning the chain by verb can
#   reconstruct WHY each handoff happened without joining to the event
#   table. Distinct from every operational verb because escalation is a
#   runtime policy decision, not an admin mutation — but it is §5.1
#   audit-significant (a customer turn was handed to a human).
ACTION_ESCALATION_FIRED = "escalation_fired"

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
    # Arc 6 Commit 8.5b -- deferred-downgrade lifecycle.
    ACTION_SUBSCRIPTION_DOWNGRADE_SCHEDULED,
    ACTION_SUBSCRIPTION_DOWNGRADE_APPLIED,
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
    # Step 30a.4 -- first-class invite lifecycle for Team and Company tiers.
    ACTION_USER_INVITED,
    ACTION_INVITE_REDEEMED,
    ACTION_INVITE_RESENT,
    ACTION_INVITE_REVOKED,
    # Step 30a.5 -- Company-tier Domain self-serve verbs.
    ACTION_DOMAIN_CREATED,
    ACTION_DOMAIN_DEACTIVATED,
    # Arc 8 WU-6 -- SES feedback / suppression cohort.
    ACTION_EMAIL_SUPPRESSION_RECORDED,
    ACTION_EMAIL_SUPPRESSION_CLEARED,
    ACTION_EMAIL_SEND_EVENT_RECEIVED,
    # Arc 5 B5 -- V2 Admin / Instance lifecycle.
    ACTION_ADMIN_CREATED,
    ACTION_INSTANCE_CREATED,
    ACTION_TIER_RENAME_APPLIED,
    ACTION_LEGACY_FIXTURE_PURGED,
    # Arc 10 Gap 6 close (D-arc10-audit-archiver-action-not-in-allowed-actions-
    # 2026-05-27): the audit retention service
    # (app/services/audit_retention_service.py::_emit_batch_audit) emits this
    # action once per archived batch to record which (admin_id, tier_window)
    # rows were moved to cold storage. The constant was declared at line 101
    # but never wired into ALLOWED_ACTIONS, so AdminAuditRepository.record()
    # rejected the emission with ValueError and rolled back the archive
    # transaction, leaving cold_archived_at NULL after a successful S3 write
    # (partial-state bug). Closing here so the move-to-cold path is fully
    # transactional end-to-end.
    ACTION_AUDIT_LOG_TIER_ARCHIVED,
    # Arc 10 Gap 7 closure: both ClosureService.initiate_closure and
    # ReactivationService.complete_reactivation declare their action
    # constants in this module but were never wired into ALLOWED_ACTIONS.
    # The constants were defined; the membership wiring was forgotten.
    # Net effect: ClosureService.initiate_closure crashed with
    # ValueError on every close attempt, and ReactivationService would
    # have done the same on every reactivation attempt. Anchored to
    # Architecture v1 §3.6.2 (Account Closure Flow) which lists "Record
    # closure-initiation timestamp on the admin" as step 6 and the
    # 30-day grace reactivation as the recoverable path; both are
    # first-class audit events.
    ACTION_ACCOUNT_CLOSURE_INITIATED,
    ACTION_ACCOUNT_REACTIVATED,
    # Arc 11 Step 7 -- admin knowledge-base routes.
    ACTION_KNOWLEDGE_SOURCE_CREATED,
    ACTION_KNOWLEDGE_SOURCE_LISTED,
    ACTION_KNOWLEDGE_SOURCE_VIEWED,
    ACTION_KNOWLEDGE_SOURCE_UPDATED,
    ACTION_KNOWLEDGE_SOURCE_DELETED,
    ACTION_KNOWLEDGE_AFFECTED_QUESTIONS_VIEWED,
    ACTION_KNOWLEDGE_CRAWL_ENQUEUED,
    # Arc 11 Closeout PR-A -- instance lifecycle verbs.
    ACTION_INSTANCE_PAUSED,
    ACTION_INSTANCE_RESUMED,
    ACTION_INSTANCE_DELETED,
    ACTION_INSTANCE_RESTORED,
    ACTION_INSTANCE_HARD_PURGED,
    # Arc 12 WU4 -- sibling-Luciel composition grant lifecycle.
    ACTION_SIBLING_GRANT_AUTHORED,
    ACTION_SIBLING_GRANT_APPROVED,
    ACTION_SIBLING_GRANT_REJECTED,
    ACTION_SIBLING_GRANT_REVOKED,
    # Arc 12 WU2b -- per-instance tool authorization admin API.
    ACTION_TOOL_AUTHORIZED,
    ACTION_TOOL_REVOKED,
    # Arc 12 WU5 -- sibling-Luciel composition runtime dispatch.
    ACTION_SIBLING_ACCESS,
    # Arc 12 EX4 -- one-time audit-chain reseal.
    ACTION_AUDIT_CHAIN_RESEALED,
    # Arc 12b -- Enterprise custom-role authoring lifecycle.
    ACTION_CUSTOM_ROLE_AUTHORED,
    ACTION_CUSTOM_ROLE_UPDATED,
    ACTION_CUSTOM_ROLE_REVOKED,
    ACTION_USER_ROLE_ASSIGNED,
    ACTION_USER_ROLE_REVOKED,
    # Arc 13 — channel adapters (email + SMS).
    ACTION_CHANNEL_ENABLED,
    ACTION_CHANNEL_DISABLED,
    ACTION_CHANNEL_NUMBER_PROVISIONED,
    ACTION_CHANNEL_NUMBER_DEPROVISIONED,
    ACTION_CHANNEL_INBOUND_RECEIVED,
    ACTION_CHANNEL_INBOUND_DROPPED,
    ACTION_CHANNEL_OUTBOUND_DELIVERED,
    # Arc 14 U2 — §3.4.5 escalation judgment.
    ACTION_ESCALATION_FIRED,
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
# Arc 5 B5 -- V2 resource types. RESOURCE_TENANT remains valid for
# legacy audit rows referencing the (about-to-be-dropped) tenant_configs
# table; RESOURCE_ADMIN is the V2 equivalent. RESOURCE_DOMAIN is
# retained for chain-walkability of pre-B1 audit rows but no NEW rows
# emit it (Domain layer eliminated).
RESOURCE_ADMIN = "admin"
RESOURCE_INSTANCE = "instance"
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

# Step 30a.4 -- user_invites row is the auditable resource for the
# four-event invite arc (issue, redeem, resend, revoke). Distinct from
# RESOURCE_USER because the invite predates the User row (User is
# provisioned only at redemption time) and from RESOURCE_SCOPE_ASSIGNMENT
# because the invite is the intent to provision, not the provisioning
# itself. resource_natural_id = invited_email so an auditor can answer
# "every event involving jane@brokerage.com" with a single filter.
RESOURCE_USER_INVITE = "user_invite"

# Arc 8 WU-6 -- SES feedback / suppression cohort.
# email_suppression rows track addresses the application layer refuses
# to send to. email_send_event rows track received SES feedback events.
# resource_natural_id = address (lowercased) on suppression actions; the
# SNS MessageId on event-received actions. resource_pk = the surrogate
# id on the respective table.
RESOURCE_EMAIL_SUPPRESSION = "email_suppression"
RESOURCE_EMAIL_SEND_EVENT = "email_send_event"

# Arc 11 Step 7 -- knowledge_sources row. Distinct from RESOURCE_KNOWLEDGE
# ("knowledge_embedding"), which the legacy /admin/knowledge/ingest path
# still uses; RESOURCE_KNOWLEDGE_SOURCE identifies the parent row in the
# new knowledge_sources table introduced in Arc 11 Step 1.
# resource_pk = knowledge_sources.id.
# resource_natural_id = knowledge_sources.source_uuid (string form).
RESOURCE_KNOWLEDGE_SOURCE = "knowledge_source"

# Arc 12 WU4 -- sibling_call_grants row (Architecture §3.3.4). The
# auditable resource for the four sibling-grant lifecycle verbs
# (ACTION_SIBLING_GRANT_AUTHORED / _APPROVED / _REJECTED / _REVOKED).
# resource_pk = sibling_call_grants.id; resource_natural_id =
# "{caller_instance_id}->{callee_instance_id}" so an auditor can
# answer "every event involving the A->B edge" with a single filter.
RESOURCE_SIBLING_CALL_GRANT = "sibling_call_grant"

# Arc 12 WU2b -- instance_tool_authorizations row. The auditable
# resource for the per-instance tool authorize/revoke admin API.
# resource_pk = instance_tool_authorizations.id;
# resource_natural_id = "{instance_id}:{tool_id}" so an auditor can
# answer "every authorize/revoke event for tool X on Instance Y"
# with a single filter.
RESOURCE_INSTANCE_TOOL_AUTHORIZATION = "instance_tool_authorization"

# Arc 12b — custom role + user role assignment.
RESOURCE_CUSTOM_ROLE = "custom_role"
RESOURCE_USER_ROLE_ASSIGNMENT = "user_role_assignment"

# Arc 13 — channel routing + config. RESOURCE_CHANNEL_ROUTE is the
# channel_routes row (inbound addressing → Instance); resource_pk =
# channel_routes.id, resource_natural_id = the route_value (email
# address or E.164). RESOURCE_INSTANCE_CHANNEL is the Instance's
# channel-config surface (enabled_channels / sms_provisioned_number);
# resource_pk = instances.id, resource_natural_id = the channel id.
RESOURCE_CHANNEL_ROUTE = "channel_route"
RESOURCE_INSTANCE_CHANNEL = "instance_channel"

# Arc 14 U2 — escalation_events row (§3.4.5). The auditable resource for
# ACTION_ESCALATION_FIRED. resource_pk = escalation_events.id;
# resource_natural_id = the session_id so an auditor can answer "every
# escalation for this conversation" with a single filter.
RESOURCE_ESCALATION_EVENT = "escalation_event"

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
    # Step 30a.4 -- first-class invite lifecycle
    RESOURCE_USER_INVITE,
    # Arc 8 WU-6 -- SES feedback / suppression cohort.
    RESOURCE_EMAIL_SUPPRESSION,
    RESOURCE_EMAIL_SEND_EVENT,
    # Arc 5 B5 -- V2 Admin / Instance.
    RESOURCE_ADMIN,
    RESOURCE_INSTANCE,
    # Arc 11 Step 7 -- knowledge_sources row.
    RESOURCE_KNOWLEDGE_SOURCE,
    # Arc 12 WU4 -- sibling_call_grants row (§3.3.4).
    RESOURCE_SIBLING_CALL_GRANT,
    # Arc 12 WU2b -- instance_tool_authorizations row.
    RESOURCE_INSTANCE_TOOL_AUTHORIZATION,
    # Arc 12b — custom role + user role assignment.
    RESOURCE_CUSTOM_ROLE,
    RESOURCE_USER_ROLE_ASSIGNMENT,
    # Arc 13 — channel routing + config.
    RESOURCE_CHANNEL_ROUTE,
    RESOURCE_INSTANCE_CHANNEL,
    # Arc 14 U2 — escalation judgment event store.
    RESOURCE_ESCALATION_EVENT,
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


    admin_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("admins.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Arc 12 EX4 (founder-directed, LOCKED 2026-05-28): the legacy
    # ``domain_id`` and ``agent_id`` Mapped columns were removed from
    # this model. Migration arc12_ex4_reseal_audit_chain_drop_agent_domain
    # RESEALS the entire hash chain under the new canonical field set
    # (sans agent_id/domain_id) before physically dropping both columns
    # plus their indexes (ix_admin_audit_logs_domain_id /
    # ix_admin_audit_logs_agent_id). See arc12_specs/02_EXCISION_PLAN.md
    # "EX4 FOUNDER DECISION (RESEAL — LOCKED)" for the integrity
    # rationale. v2 customer-data scoping is admin_id +
    # luciel_instance_id (Architecture §3.7.3 Wall 3).
    # Arc 10 Gap 7 (2026-05-27): loosened back to nullable. The Arc
    # 9.1 Phase A tenant-isolation seal applied NOT NULL too
    # broadly. Per Architecture v1 §3.7.3 (Wall 3), the non-null
    # instance_id rule applies to customer-data tables; per §5.3
    # the admin audit log is a distinct concept (append-only audit
    # chain, separate DB role). §3.6.2 also requires admin-scoped
    # audit emissions (team-member invalidation, embed-key revocation)
    # which by their nature have no single instance scope. NULL here
    # means "this audit row records an admin-scoped op spanning all
    # instances"; non-NULL still means "scoped to this instance".
    luciel_instance_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True,
        comment=(
            "PK of the instances row this audit row is scoped to. "
            "NULL = admin-scoped op (cascade, team-member ops, etc.) "
            "spanning all of the admin's instances."
        ),
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
    # (admin_id slug, instance_id slug, key_prefix, etc.).
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

    # -----------------------------------------------------------------
    # Arc 10 (Alembic arc10_lifecycle_subsystem) — audit-tier retention.
    # -----------------------------------------------------------------
    # Reconciles Arc 9 C6.1's "forward-only forever" stance with Vision
    # §6.5 ("audit log archived to cold storage for legal retention
    # window") and §7 (tier-conditional retention: 30d Free / 1y Pro /
    # 7y Enterprise). The Vision is canonical per Vision §10 doctrine-
    # anchor.
    #
    # tier_at_write: STICKY — the admin's tier at the moment this row
    # was written. A Pro→Free downgrade does NOT retroactively shorten
    # the retention of Pro-era audit rows. Backfilled best-effort in
    # the Arc 10 migration from the admin's current tier. New rows
    # written by AdminAuditRepository.record() must populate this from
    # the writing context (paired code change in a follow-up commit
    # if the repo doesn't already do it).
    #
    # cold_archived_at: set by AuditRetentionService when this row has
    # been written to S3 cold storage with the hash chain extended
    # across the boundary. The hot row stays in place (chain stays
    # walkable from current rows back through history). A future arc
    # may add a hot-purge step that DELETEs rows whose cold_archived_at
    # is well-past retention; Arc 10 does NOT do that (luciel_audit_
    # archiver has SELECT + UPDATE only, no DELETE).
    tier_at_write: Mapped[str | None] = mapped_column(
        String(16), nullable=True,
        comment=(
            "Arc 10 — admin's tier AT THE MOMENT this row was written. "
            "Sticky across downgrades."
        ),
    )
    cold_archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment=(
            "Arc 10 — set when this row has been archived to S3 cold "
            "storage with hash-chain extension. Hot row retained."
        ),
    )

    __table_args__ = (
        # The query we care about most: "show me all admin actions for
        # tenant X in time range Y" for a tenant audit dashboard.
        Index(
            "ix_admin_audit_logs_tenant_time",
            "admin_id",
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
            f"tenant={self.admin_id} {self.action}:{self.resource_type} "
            f"pk={self.resource_pk}>"
        )