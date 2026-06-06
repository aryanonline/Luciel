"""Arc 5 Revision A — additive schema for the Admin → Instance → Lead collapse.

Revision ID: arc5_a_admin_instance_additive
Revises: b2e5f17a3d9c
Create Date: 2026-05-23

Why this migration exists
-------------------------

Arc 5 collapses the legacy ``Tenant → Domain → Agent → LucielInstance``
four-level hierarchy onto a uniform ``Admin → Instance → Lead`` shape
across every tier (Free / Pro / Enterprise). This is the founder-locked
doctrine recorded at:

* CANONICAL_RECAP §11 Q1 (Admin scope as billing boundary)
* CANONICAL_RECAP §14 (Option A entitlement matrix, locked 2026-05-23)
* ARCHITECTURE §3.2.14 (billing_model enum + admin_tier_overrides)
* ARCHITECTURE §4.1 / §4.7 (three-layer scope enforcement)
* arc4-out/A-tier-matrix-detail.md §17 (WU-8 Phase A schema)
* arc5-out/A-arc5-preflight.md §2 (Revision A specification)
* arc5-out/A-arc5-arc4-plan-defects.md §6 (Q1/Q2/Q3 + Gap 1 partner locks)

Revision A is **pure additive** — it creates new tables and one nullable
column on an existing table. **No existing data is mutated.** Rollback
(``alembic downgrade -1``) drops the new tables and column with zero
prod-data risk.

The high-risk work (~3,900-callsite code rename + tenant→admin
backfill) lands at Revision B. The destructive work (drop legacy
``tenant_configs`` / ``domains`` / ``luciel_instances`` / ``agents`` +
40 scope-FK columns + tighten the tier CHECK constraint) lands at
Revision C.

Schema shape decisions
----------------------

**admins.id is VARCHAR(100), not INTEGER.** Per partner Q1 lock
2026-05-23 (arc5-out/A-arc5-arc4-plan-defects.md §6.1):
``admins.id`` is the semantic slug key mirrored from
``tenant_configs.tenant_id``. This makes Revision B's backfill JOIN
trivial (``WHERE admins.id = tenant_configs.tenant_id``) and aligns
with the rest of the platform's existing scope-by-slug convention
(``subscriptions.tenant_id`` String(100) FK, ``luciel_instances.scope_owner_tenant_id``
String(100) FK, ``users.tenant_id`` String(100) FK).

**instances.id is INTEGER autoincrement.** Mirrors
``luciel_instances.id`` exactly so Revision B's backfill is a one-line
INSERT-SELECT preserving the original integer surrogate key. Cross-row
references inside this revision use ``instances.id`` (the integer);
references from existing legacy tables to instances during the
Revision B dual-write window resolve via the back-pointer columns
``legacy_luciel_instance_id`` and ``legacy_agent_id``.

**admin_tier_overrides is WIDE-ROW, not EAV.** Per partner doctrine
judgment 2026-05-23 (resolving the preflight §2.1 vs ARCHITECTURE
§3.2.14 conflict in favor of the latter): one row per Enterprise
Admin, ~24 explicit nullable columns mirroring the
``TierEntitlement`` dataclass field names from
``app/policy/entitlements.py``. This gives type-safe DB-layer
constraints, queryable compliance audits, and a 1:1 mapping with
``resolve_entitlement(overrides=dict)`` ergonomics. Wide-row also
matches the existing ``subscriptions`` table shape — one Stripe entity
per Admin maps to one override entity per Admin.

The drift opened by this judgment is recorded as
``D-admin-tier-overrides-shape-preflight-stale-2026-05-23`` in
the Arc 5 preflight (resolved-on-arrival — the preflight prose is
truthified in the same commit window).

**Tier CHECK constraint is PERMISSIVE during the migration window.**
``CHECK (tier IN ('free', 'pro', 'enterprise', 'individual', 'solo',
'team', 'company'))`` so Revision B's UPDATE statements can rewrite
``individual``/``solo``→``pro`` and ``team``/``company``→``enterprise``
without violating the constraint mid-flight. Revision C tightens it to
``tier IN ('free', 'pro', 'enterprise')``.

**subscriptions.billing_model is NULLABLE with backfill to 'flat'.** Per
ARCHITECTURE §3.2.14: nullable absorbs both the Free case (Free Admins
have no ``subscriptions`` row at all) and the existing Pro rows that
backfill to ``flat`` in this same migration. Enum values authored at
v1: ``flat`` / ``hybrid`` / ``consumption`` (consumption reserved for
future use; no SKU ships with it at Arc 6).

What this revision does NOT do
------------------------------

* Does NOT rename any existing table or column.
* Does NOT touch any existing data outside the
  ``UPDATE subscriptions SET billing_model='flat'`` backfill.
* Does NOT modify ``app/models/`` — the new models (``app/models/admin.py``,
  ``app/models/instance.py``, ``app/models/aliases.py``) land at
  Revision B Batch 1.
* Does NOT enforce v2 tier values — the permissive CHECK accepts both
  legacy and v2 strings to permit the Revision B rename UPDATEs.
* Does NOT mint any ``admins`` or ``instances`` rows. The backfill runs
  in Revision B (idempotent INSERT-SELECT).

Rollback contract
-----------------

``alembic downgrade -1`` drops the 6 new tables (in FK-reverse order:
metering_emissions, admin_tier_overrides, knowledge_share_grants,
instance_composition_grants, instances, admins) and drops
``subscriptions.billing_model``. Because every drop targets new state
created in this revision, the downgrade is data-safe.
"""

import sqlalchemy as sa
from alembic import op


# Alembic identifiers
revision = "arc5_a_admin_instance_additive"
down_revision = "b2e5f17a3d9c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the additive schema for Arc 5 Admin/Instance collapse.

    Order matters because of foreign keys:
      1. ``admins`` (no FK dependencies on new tables)
      2. ``instances`` (FK admin_id → admins.id)
      3. ``instance_composition_grants`` (FK admin_id, caller_instance_id, callee_instance_id)
      4. ``knowledge_share_grants`` (FK admin_id, source_instance_id, target_instance_id)
      5. ``admin_tier_overrides`` (FK admin_id; PK admin_id)
      6. ``metering_emissions`` (FK admin_id)
      7. Add ``subscriptions.billing_model`` column + backfill
    """

    # ------------------------------------------------------------------
    # 1. admins — the billing entity and permissions root.
    # ------------------------------------------------------------------
    # Replaces ``tenant_configs`` (which is dropped at Revision C).
    # ``id`` is String(100) semantic key per Q1 lock (mirrors
    # tenant_configs.tenant_id). ``legacy_tenant_id`` is the back-pointer
    # so Revision B's alias helper (``admin_or_tenant_id_for(row)``) can
    # resolve callsite reads during the dual-write cutover window.
    op.create_table(
        "admins",
        sa.Column(
            "id",
            sa.String(100),
            primary_key=True,
            comment=(
                "Semantic slug key mirrored from tenant_configs.tenant_id "
                "at Revision B backfill (Q1 lock 2026-05-23 — "
                "arc5-out/A-arc5-arc4-plan-defects.md §6.1)."
            ),
        ),
        sa.Column(
            "name",
            sa.String(200),
            nullable=False,
            comment="Human-readable display name (mirrors tenant_configs.display_name).",
        ),
        sa.Column(
            "tier",
            sa.String(16),
            nullable=False,
            server_default="free",
            comment=(
                "Tier vocabulary: 'free' / 'pro' / 'enterprise' at Revision C; "
                "during Revision A+B window the CHECK accepts legacy values too "
                "('individual' / 'solo' / 'team' / 'company') for the rename "
                "UPDATEs. Orphan-customer default is 'free' per Q2 lock."
            ),
        ),
        sa.Column(
            "tier_source",
            sa.String(32),
            nullable=False,
            server_default="manual",
            comment=(
                "Audit-source field per Q2 lock — records HOW the tier was "
                "established: 'stripe_webhook' (paid Pro/Enterprise via webhook), "
                "'sales_ops_provisioned' (Enterprise sales-mediated), "
                "'free_signup' (self-serve Free), 'revision_b_backfill' "
                "(Arc 5 backfill from legacy tier), 'manual' (operator-applied)."
            ),
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="Soft-delete flag (Pattern E — deactivate, never delete).",
        ),
        sa.Column(
            "stripe_customer_id",
            sa.String(64),
            nullable=True,
            comment=(
                "Stripe customer ID. NULL while the Admin is on Free tier per "
                "Gap 1 lock (arc5-out/A-arc5-arc4-plan-defects.md §6.4) — "
                "lazy-created on upgrade to Pro/Enterprise. Pro/Enterprise "
                "Admins MUST carry a non-NULL value (enforced at the "
                "application layer in BillingService; not a DB CHECK because "
                "the transient mint window cannot atomically populate both "
                "the admins row and the Stripe customer record)."
            ),
        ),
        sa.Column(
            "legacy_tenant_id",
            sa.String(100),
            nullable=True,
            comment=(
                "Back-pointer to tenant_configs.tenant_id for the Revision B "
                "alias helper. NULL only for Admins minted post-Revision-B "
                "(no legacy ancestor). Dropped at Revision C."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "tier IN ('free', 'pro', 'enterprise', 'individual', 'solo', 'team', 'company')",
            name="ck_admins_tier_valid_during_migration",
        ),
        sa.CheckConstraint(
            "tier_source IN ('stripe_webhook', 'sales_ops_provisioned', "
            "'free_signup', 'revision_b_backfill', 'manual')",
            name="ck_admins_tier_source_valid",
        ),
    )
    op.create_index("ix_admins_tier", "admins", ["tier"])
    op.create_index("ix_admins_active", "admins", ["active"])
    op.create_index(
        "ix_admins_legacy_tenant_id",
        "admins",
        ["legacy_tenant_id"],
        unique=True,
        postgresql_where=sa.text("legacy_tenant_id IS NOT NULL"),
    )
    op.create_index(
        "ix_admins_stripe_customer_id",
        "admins",
        ["stripe_customer_id"],
        unique=True,
        postgresql_where=sa.text("stripe_customer_id IS NOT NULL"),
    )

    # ------------------------------------------------------------------
    # 2. instances — replaces luciel_instances + collapses agents into
    #    the same table (the four-level hierarchy's Domain + Agent layers
    #    are eliminated; an Instance is the new combined unit).
    # ------------------------------------------------------------------
    op.create_table(
        "instances",
        sa.Column(
            "id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
            comment=(
                "Integer surrogate PK mirroring luciel_instances.id exactly so "
                "Revision B's INSERT-SELECT preserves the original key space "
                "(idempotent backfill: WHERE instances.id = luciel_instances.id)."
            ),
        ),
        sa.Column(
            "admin_id",
            sa.String(100),
            sa.ForeignKey("admins.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
            comment="Owning Admin scope. RESTRICT — soft-delete Admins, never hard-delete.",
        ),
        sa.Column(
            "instance_slug",
            sa.String(100),
            nullable=False,
            comment=(
                "URL-safe slug, unique within Admin. Mirrors "
                "luciel_instances.instance_id at Revision B backfill."
            ),
        ),
        sa.Column(
            "display_name",
            sa.String(200),
            nullable=False,
            comment="Mirrors luciel_instances.display_name.",
        ),
        sa.Column(
            "description",
            sa.String(1000),
            nullable=True,
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "legacy_luciel_instance_id",
            sa.Integer(),
            nullable=True,
            comment=(
                "Back-pointer to luciel_instances.id for the Revision B "
                "alias helper. NULL only for Instances minted post-Revision-B. "
                "Dropped at Revision C alongside the luciel_instances table."
            ),
        ),
        sa.Column(
            "legacy_agent_id",
            sa.Integer(),
            nullable=True,
            comment=(
                "Back-pointer to agents.id for any Instance that was a "
                "pre-collapse Agent. NULL for Instances that were "
                "LucielInstances or are post-Revision-B mints. Dropped at "
                "Revision C alongside the agents table."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "admin_id", "instance_slug", name="uq_instances_admin_id_slug"
        ),
    )
    op.create_index("ix_instances_active", "instances", ["active"])
    op.create_index(
        "ix_instances_legacy_luciel_instance_id",
        "instances",
        ["legacy_luciel_instance_id"],
        unique=True,
        postgresql_where=sa.text("legacy_luciel_instance_id IS NOT NULL"),
    )
    op.create_index(
        "ix_instances_legacy_agent_id",
        "instances",
        ["legacy_agent_id"],
        unique=True,
        postgresql_where=sa.text("legacy_agent_id IS NOT NULL"),
    )

    # ------------------------------------------------------------------
    # 3. instance_composition_grants — Pro/Enterprise inter-Instance
    #    composition (depth-bounded per tier; CANONICAL §11 Q4).
    # ------------------------------------------------------------------
    # Pro caps depth ≤2 via app/policy/entitlements.py max_composition_depth=2;
    # Enterprise caps via admin_tier_overrides.max_composition_depth_override.
    # Free has composition_enabled=False so no rows can be authored.
    op.create_table(
        "instance_composition_grants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "admin_id",
            sa.String(100),
            sa.ForeignKey("admins.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
            comment=(
                "Denormalized for cross-Admin abuse-prevention: a grant row "
                "is valid only if the caller AND callee Instances both belong "
                "to this admin_id. Enforced at the service layer in "
                "InstanceCompositionService."
            ),
        ),
        sa.Column(
            "caller_instance_id",
            sa.Integer(),
            sa.ForeignKey("instances.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
            comment="The Instance making the composition call.",
        ),
        sa.Column(
            "callee_instance_id",
            sa.Integer(),
            sa.ForeignKey("instances.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
            comment="The Instance being composed into.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_by_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
            comment="The admin-team member who minted the grant (audit trail).",
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Soft-revoke timestamp (Pattern E). NULL means active.",
        ),
        sa.Column(
            "notes",
            sa.Text(),
            nullable=True,
            comment="Optional operator notes (e.g. 'enabled for Q3 marketing pilot').",
        ),
        sa.CheckConstraint(
            "caller_instance_id != callee_instance_id",
            name="ck_composition_grants_no_self_composition",
        ),
        sa.UniqueConstraint(
            "admin_id",
            "caller_instance_id",
            "callee_instance_id",
            name="uq_composition_grants_admin_caller_callee",
        ),
    )
    op.create_index(
        "ix_composition_grants_active",
        "instance_composition_grants",
        ["admin_id", "caller_instance_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    # ------------------------------------------------------------------
    # 4. knowledge_share_grants — Enterprise cross-Instance knowledge
    #    sharing (CANONICAL §11 Q4; ARCHITECTURE §4.7).
    # ------------------------------------------------------------------
    # Free + Pro cannot author rows here (knowledge_share_grants_enabled=False
    # in app/policy/entitlements.py). Enterprise authors at-will subject to
    # admin_tier_overrides.knowledge_share_grants_enabled.
    op.create_table(
        "knowledge_share_grants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "admin_id",
            sa.String(100),
            sa.ForeignKey("admins.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "source_instance_id",
            sa.Integer(),
            sa.ForeignKey("instances.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
            comment="The Instance whose knowledge namespace is being shared.",
        ),
        sa.Column(
            "target_instance_id",
            sa.Integer(),
            sa.ForeignKey("instances.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
            comment="The Instance receiving read access.",
        ),
        sa.Column(
            "scope",
            sa.String(32),
            nullable=False,
            server_default="read_only",
            comment=(
                "Grant scope. v1 values: 'read_only' (target can read source's "
                "knowledge embeddings). Reserved for future: 'read_write', "
                "'namespace_scoped'."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_by_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "source_instance_id != target_instance_id",
            name="ck_knowledge_share_grants_no_self_share",
        ),
        sa.CheckConstraint(
            "scope IN ('read_only')",
            name="ck_knowledge_share_grants_scope_valid",
        ),
        sa.UniqueConstraint(
            "admin_id",
            "source_instance_id",
            "target_instance_id",
            name="uq_knowledge_share_grants_admin_source_target",
        ),
    )
    op.create_index(
        "ix_knowledge_share_grants_active",
        "knowledge_share_grants",
        ["admin_id", "source_instance_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    # ------------------------------------------------------------------
    # 5. admin_tier_overrides — WIDE-ROW per-Admin entitlement overrides.
    # ------------------------------------------------------------------
    # Doctrine: ARCHITECTURE §3.2.14 + CANONICAL §14 + Commit 3
    # resolve_entitlement(overrides=dict) ergonomics. One row per
    # Enterprise Admin; Free and Pro Admins NEVER carry a row (the
    # absence of a row is the canonical "static map applies" signal).
    #
    # Column naming convention: column name == TierEntitlement dataclass
    # field name (no "_override" suffix) so resolve_entitlement() can
    # destructure the row into kwargs without remapping. The semantics
    # are still "override" — the values here override the static map per
    # the §18.3 algorithm.
    #
    # All entitlement-axis columns are NULLABLE — NULL means "no override,
    # fall through to static map." This is the same semantic the
    # resolve_entitlement() Enterprise-only override gate already
    # implements.
    op.create_table(
        "admin_tier_overrides",
        sa.Column(
            "admin_id",
            sa.String(100),
            sa.ForeignKey("admins.id", ondelete="RESTRICT"),
            primary_key=True,
            comment=(
                "1:1 with admins. Enterprise-only — Free and Pro Admins do "
                "NOT carry a row here."
            ),
        ),
        # --- Axis 1: Instance count ---
        sa.Column("instance_count_cap", sa.Integer(), nullable=True),
        # --- Axis 2: Leads per month ---
        sa.Column("leads_per_month_cap", sa.Integer(), nullable=True),
        # --- Axis 3: Model tier ---
        sa.Column("model_tier_default", sa.String(32), nullable=True),
        # --- Axis 4: Composition ---
        sa.Column("composition_enabled", sa.Boolean(), nullable=True),
        sa.Column("max_composition_depth", sa.Integer(), nullable=True),
        sa.Column("knowledge_share_grants_enabled", sa.Boolean(), nullable=True),
        # --- Axis 5: API access ---
        sa.Column("api_enabled", sa.Boolean(), nullable=True),
        sa.Column("api_rate_limit_rpm", sa.Integer(), nullable=True),
        sa.Column("embed_key_count_cap", sa.Integer(), nullable=True),
        # --- Axis 6: Seats + delegated admin ---
        sa.Column("seat_cap", sa.Integer(), nullable=True),
        sa.Column("delegated_admin_enabled", sa.Boolean(), nullable=True),
        # --- Axis 8: Audit retention ---
        sa.Column(
            "audit_retention_days",
            sa.Integer(),
            nullable=True,
            comment=(
                "Per-contract audit retention. Most Enterprise contracts "
                "negotiate 2555 (7 years) for FINTRAC compliance; NULL means "
                "unlimited (no automatic purge)."
            ),
        ),
        # --- Axis 9: SSO ---
        sa.Column("sso_enabled", sa.Boolean(), nullable=True),
        # --- Axis 10: Widget branding + CNAME ---
        sa.Column("widget_branding_custom", sa.Boolean(), nullable=True),
        sa.Column("widget_custom_domain_cname_cap", sa.Integer(), nullable=True),
        # --- Axis 11: Webhook outbound ---
        sa.Column("webhook_outbound_enabled", sa.Boolean(), nullable=True),
        # --- Axis 12: Cross-Instance memory federation ---
        sa.Column("cross_instance_memory_federation", sa.Boolean(), nullable=True),
        # --- Axis 13: SLA ---
        sa.Column("uptime_sla_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("support_sla", sa.String(32), nullable=True),
        # --- Axis 14: Data residency ---
        sa.Column("data_residency_region", sa.String(32), nullable=True),
        # --- Axis 15: Export ---
        sa.Column("export_csv_enabled", sa.Boolean(), nullable=True),
        sa.Column("export_audit_chain_enabled", sa.Boolean(), nullable=True),
        # --- Axis 16: Billing model ---
        sa.Column(
            "billing_model",
            sa.String(16),
            nullable=True,
            comment=(
                "Mirrors subscriptions.billing_model so a regulator reading "
                "the override table alone can see the negotiated shape. "
                "Values: 'flat' / 'hybrid' / 'consumption'."
            ),
        ),
        # --- WU-8 Phase A — hybrid-billing metering columns ---
        sa.Column(
            "included_usage_per_period",
            sa.Integer(),
            nullable=True,
            comment=(
                "WU-8 Phase A. The floor of included usage per billing period "
                "(units depend on metered_unit). Above the floor, overage "
                "billing kicks in via the metering hook (§3.2.14)."
            ),
        ),
        sa.Column(
            "overage_rate_cents",
            sa.Integer(),
            nullable=True,
            comment=(
                "WU-8 Phase A. Per-unit overage rate in cents (Stripe wire "
                "format). Multiplied by (usage - included_usage_per_period) "
                "for the metered emission."
            ),
        ),
        sa.Column(
            "committed_use_discount_bps",
            sa.Integer(),
            nullable=True,
            comment=(
                "WU-8 Phase A. Basis points off the platform-fee floor for "
                "committed-use Enterprise customers (e.g. 1500 = 15%). "
                "Bounded [0, 10000] via CHECK below."
            ),
        ),
        sa.Column(
            "period_start",
            sa.Date(),
            nullable=True,
            comment=(
                "WU-8 Phase A. Contract window start. Expiry triggers "
                "re-negotiation rather than silent renewal."
            ),
        ),
        sa.Column(
            "period_end",
            sa.Date(),
            nullable=True,
            comment="WU-8 Phase A. Contract window end.",
        ),
        sa.Column(
            "metered_unit",
            sa.String(32),
            nullable=True,
            comment=(
                "WU-8 Phase A. What gets metered for hybrid billing: 'leads' "
                "(count of leads.created_at in window) / 'tokens' (sum of "
                "traces token counts) / 'api_calls' (count of API requests)."
            ),
        ),
        # --- Audit + lifecycle ---
        sa.Column(
            "notes",
            sa.Text(),
            nullable=True,
            comment="Operator notes (e.g. contract ID, negotiation summary).",
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="Soft-deactivate without dropping the row (audit trail).",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_by_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
            comment="Operator who authored the override row (audit trail).",
        ),
        sa.CheckConstraint(
            "billing_model IS NULL OR billing_model IN ('flat', 'hybrid', 'consumption')",
            name="ck_admin_tier_overrides_billing_model_valid",
        ),
        sa.CheckConstraint(
            "metered_unit IS NULL OR metered_unit IN ('leads', 'tokens', 'api_calls')",
            name="ck_admin_tier_overrides_metered_unit_valid",
        ),
        sa.CheckConstraint(
            "committed_use_discount_bps IS NULL OR "
            "(committed_use_discount_bps >= 0 AND committed_use_discount_bps <= 10000)",
            name="ck_admin_tier_overrides_discount_bps_range",
        ),
        sa.CheckConstraint(
            "support_sla IS NULL OR support_sla IN "
            "('community', 'email_48h', 'email_24h_plus_csm')",
            name="ck_admin_tier_overrides_support_sla_valid",
        ),
        sa.CheckConstraint(
            "period_start IS NULL OR period_end IS NULL OR period_start <= period_end",
            name="ck_admin_tier_overrides_period_order",
        ),
        sa.CheckConstraint(
            "uptime_sla_pct IS NULL OR "
            "(uptime_sla_pct >= 0 AND uptime_sla_pct <= 100)",
            name="ck_admin_tier_overrides_uptime_range",
        ),
    )
    op.create_index(
        "ix_admin_tier_overrides_active",
        "admin_tier_overrides",
        ["admin_id"],
        postgresql_where=sa.text("active = TRUE"),
    )

    # ------------------------------------------------------------------
    # 6. metering_emissions — append-only cursor for Enterprise hybrid
    #    metering hook (ARCHITECTURE §3.2.14, WU-8 Phase A).
    # ------------------------------------------------------------------
    # The metering worker (app/workers/metering_worker.py, lands at WU-8
    # Phase B) reads admin_tier_overrides for each Enterprise Admin with
    # subscriptions.billing_model='hybrid', computes the delta since the
    # last successful emission cursor, and writes a row here in the same
    # SQLAlchemy transaction as the Stripe usage_record + audit row.
    op.create_table(
        "metering_emissions",
        sa.Column(
            "admin_id",
            sa.String(100),
            sa.ForeignKey("admins.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "period",
            sa.String(7),
            primary_key=True,
            comment=(
                "Billing period in YYYY-MM format (Stripe's billing-period "
                "convention). E.g. '2026-06'."
            ),
        ),
        sa.Column(
            "emission_ts",
            sa.DateTime(timezone=True),
            primary_key=True,
            server_default=sa.func.now(),
            comment=(
                "Wall-clock timestamp of this emission. PK so the same "
                "(admin, period) can have multiple emissions per period "
                "(hourly cadence + period-close)."
            ),
        ),
        sa.Column(
            "stripe_idempotency_key",
            sa.String(128),
            nullable=False,
            unique=True,
            comment=(
                "Stripe Idempotency-Key header sent on the usage_record "
                "create. Format: 'metering-{admin_id}-{period}-{emission_ts_iso}'. "
                "UNIQUE so a retry that lost its DB write but succeeded on "
                "Stripe's side cannot double-bill."
            ),
        ),
        sa.Column(
            "quantity_emitted",
            sa.Integer(),
            nullable=False,
            comment=(
                "Number of metered units (per admin_tier_overrides.metered_unit) "
                "emitted to Stripe in this emission. CHECK >= 0 — emissions "
                "are always non-negative deltas."
            ),
        ),
        sa.Column(
            "stripe_subscription_item_id",
            sa.String(64),
            nullable=False,
            comment=(
                "The Stripe SubscriptionItem.id this usage_record was attached "
                "to. Denormalized for audit replay — a regulator reading this "
                "table alone can reconstruct what was billed without joining "
                "to live Stripe state."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "quantity_emitted >= 0",
            name="ck_metering_emissions_quantity_nonneg",
        ),
        sa.CheckConstraint(
            r"period ~ '^[0-9]{4}-[0-9]{2}$'",
            name="ck_metering_emissions_period_format",
        ),
    )
    op.create_index(
        "ix_metering_emissions_period",
        "metering_emissions",
        ["period", "admin_id"],
    )

    # ------------------------------------------------------------------
    # 7. subscriptions.billing_model — new nullable column + backfill.
    # ------------------------------------------------------------------
    # Per ARCHITECTURE §3.2.14: nullable because Free Admins never have a
    # subscriptions row (so the column doesn't apply to them) and the
    # backfill UPDATE writes 'flat' for every existing Pro row.
    op.add_column(
        "subscriptions",
        sa.Column(
            "billing_model",
            sa.String(16),
            nullable=True,
            comment=(
                "Billing model: 'flat' (Pro — single recurring Price) / "
                "'hybrid' (Enterprise — platform-fee + metered) / 'consumption' "
                "(future, no SKU yet). NULL is invalid for live Pro/Enterprise "
                "rows but allowed by schema during the Revision A backfill "
                "window. Application-layer enforces non-NULL at write time "
                "post-Revision-A."
            ),
        ),
    )
    op.create_index(
        "ix_subscriptions_billing_model",
        "subscriptions",
        ["billing_model"],
    )
    # In-migration backfill: every existing subscription row is Pro under
    # the legacy 4-tier shape (no Enterprise subscriptions exist yet —
    # Enterprise is sales-provisioned starting at Arc 6), so 'flat' is
    # the correct backfill value for 100% of existing rows.
    op.execute(
        "UPDATE subscriptions SET billing_model = 'flat' WHERE billing_model IS NULL"
    )
    op.create_check_constraint(
        "ck_subscriptions_billing_model_valid",
        "subscriptions",
        "billing_model IS NULL OR billing_model IN ('flat', 'hybrid', 'consumption')",
    )


def downgrade() -> None:
    """Reverse upgrade() in strict FK-reverse order.

    Order matters: child tables drop before parents because of the
    RESTRICT foreign-key constraints.
    """
    # 7. subscriptions.billing_model (reverse of step 7)
    op.drop_constraint(
        "ck_subscriptions_billing_model_valid", "subscriptions", type_="check"
    )
    op.drop_index("ix_subscriptions_billing_model", table_name="subscriptions")
    op.drop_column("subscriptions", "billing_model")

    # 6. metering_emissions
    op.drop_index("ix_metering_emissions_period", table_name="metering_emissions")
    op.drop_table("metering_emissions")

    # 5. admin_tier_overrides
    op.drop_index(
        "ix_admin_tier_overrides_active", table_name="admin_tier_overrides"
    )
    op.drop_table("admin_tier_overrides")

    # 4. knowledge_share_grants
    op.drop_index(
        "ix_knowledge_share_grants_active", table_name="knowledge_share_grants"
    )
    op.drop_table("knowledge_share_grants")

    # 3. instance_composition_grants
    op.drop_index(
        "ix_composition_grants_active", table_name="instance_composition_grants"
    )
    op.drop_table("instance_composition_grants")

    # 2. instances
    op.drop_index(
        "ix_instances_legacy_agent_id", table_name="instances"
    )
    op.drop_index(
        "ix_instances_legacy_luciel_instance_id", table_name="instances"
    )
    op.drop_index("ix_instances_active", table_name="instances")
    op.drop_table("instances")

    # 1. admins
    op.drop_index(
        "ix_admins_stripe_customer_id", table_name="admins"
    )
    op.drop_index(
        "ix_admins_legacy_tenant_id", table_name="admins"
    )
    op.drop_index("ix_admins_active", table_name="admins")
    op.drop_index("ix_admins_tier", table_name="admins")
    op.drop_table("admins")
