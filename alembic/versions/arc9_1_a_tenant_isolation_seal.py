"""Arc 9.1 Phase A -- Tenant isolation seal.

This is the single atomic schema change that closes the three live
prod gaps surfaced by the 2026-05-25 prod-alignment verification:

  G2 + P3 (CRITICAL leak surface):
    707 api_keys + 204 knowledge_embeddings rows in prod had
    luciel_instance_id = NULL, and the RLS policy permitted
    "instance_id IS NULL" as a match clause -- making every one of
    those rows visible to every Admin. Combined with the orm-level
    nullability of luciel_instance_id this was a structural bypass.

  P2 (the policy bypass itself):
    Every Instance-scoped RLS policy in prod contained the clause
        luciel_instance_id IS NULL
        OR (luciel_instance_id::text = current_setting('app.instance_id', true))
    The "IS NULL" disjunct is the bypass. Once luciel_instance_id is
    NOT NULL at the schema level, the clause is dead -- but to make
    the doctrine explicit (defense-in-depth) we also REMOVE the
    "IS NULL" disjunct from every Instance-scoped policy.

  G8 (audit immutability):
    luciel_app retained UPDATE + DELETE on admin_audit_logs in prod,
    contradicting the Vision §4 "append-only" guarantee. We REVOKE
    those two privileges.

Approach (clean-slate, founder-approved 2026-05-25):
  1. WIPE all rows in messages, sessions, traces, memory_items,
     admin_audit_logs, knowledge_embeddings, api_keys, identity_claims,
     scope_assignments, user_invites, user_consents, conversations,
     instance_composition_grants, knowledge_share_grants, subscriptions,
     instances, admins, users.
     Justification: probe of 2026-05-25 confirmed every row in the
     database is CI/synthetic test residue or pre-Vision dev signups.
     No real customer data exists. The free-56c7f4e5 "Vantage Mnd" admin
     is a dev demo signup that will be recreated post-deploy.
  2. ALTER COLUMN luciel_instance_id SET NOT NULL on every Instance-scoped
     table (messages, sessions, traces, memory_items, knowledge_embeddings,
     api_keys, admin_audit_logs).
  3. DROP each *_instance_isolation policy and recreate without the
     IS NULL disjunct.
  4. REVOKE UPDATE, DELETE on admin_audit_logs from luciel_app.

Roll-forward only -- downgrade() is intentionally a no-op because the
WIPE step is irreversible by design. Recovery path is restore from
RDS automated backup (point-in-time-recovery is available).

Revision ID: arc9_1_a_tenant_isolation_seal
Revises: arc9_c22_identity_bootstrap
Create Date: 2026-05-25
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "arc9_1_a_tenant_isolation_seal"
down_revision = "arc9_c22_identity_bootstrap"
branch_labels = None
depends_on = None


# Tables that have luciel_instance_id and need it tightened to NOT NULL.
# (admin_audit_logs is special-cased below because its policy uses
#  app.instance_id but it's still keyed by Instance.)
INSTANCE_SCOPED_TABLES = [
    "messages",
    "sessions",
    "traces",
    "memory_items",
    "knowledge_embeddings",
    "api_keys",
    "admin_audit_logs",
]


# Mapping: table -> (policy_name, column).
# Every one of these policies must be rewritten without the IS NULL clause.
# Pulled verbatim from prod pg_policies on 2026-05-25.
INSTANCE_POLICIES = {
    "messages":             ("messages_instance_isolation",             "luciel_instance_id"),
    "sessions":             ("sessions_instance_isolation",             "luciel_instance_id"),
    "traces":               ("traces_instance_isolation",               "luciel_instance_id"),
    "memory_items":         ("memory_items_instance_isolation",         "luciel_instance_id"),
    "knowledge_embeddings": ("knowledge_embeddings_instance_isolation", "luciel_instance_id"),
    "api_keys":             ("api_keys_instance_isolation",             "luciel_instance_id"),
    "admin_audit_logs":     ("admin_audit_logs_instance_isolation",     "luciel_instance_id"),
}


def upgrade() -> None:
    bind = op.get_bind()

    # ----------------------------------------------------------------
    # STEP 1: WIPE (clean slate, founder-approved)
    # ----------------------------------------------------------------
    # Order matters: children before parents on FK references.
    # All wipes are unconditional DELETE -- TRUNCATE would skip RLS audit.
    wipe_order = [
        # leaf tables first
        "messages",
        "memory_items",
        "traces",
        "sessions",
        "knowledge_embeddings",
        "admin_audit_logs",
        "api_keys",
        "identity_claims",
        "scope_assignments",
        "user_invites",
        "user_consents",
        "conversations",
        "instance_composition_grants",
        "knowledge_share_grants",
        "deletion_logs",
        "retention_policies",
        "admin_widget_domains",
        "admin_tier_overrides",
        "metering_emissions",
        "email_send_event",
        "email_suppression",
        "subscriptions",
        # parent tables last
        "instances",
        "admins",
        "users",
    ]
    for tbl in wipe_order:
        # DELETE not TRUNCATE: keeps sequences, runs through any
        # triggers, and is RLS-bypassed only because alembic runs as
        # luciel_admin (superuser equivalent for our DB).
        op.execute(sa.text(f"DELETE FROM {tbl}"))

    # ----------------------------------------------------------------
    # STEP 2: NOT NULL on luciel_instance_id
    # ----------------------------------------------------------------
    for tbl in INSTANCE_SCOPED_TABLES:
        op.alter_column(
            tbl,
            "luciel_instance_id",
            existing_type=sa.Integer(),
            nullable=False,
        )

    # ----------------------------------------------------------------
    # STEP 3: rewrite every Instance-scoped policy without IS NULL
    # ----------------------------------------------------------------
    for tbl, (policy, col) in INSTANCE_POLICIES.items():
        op.execute(sa.text(f"DROP POLICY IF EXISTS {policy} ON {tbl}"))
        op.execute(sa.text(f"""
            CREATE POLICY {policy} ON {tbl}
            FOR ALL
            USING (
                {col}::text = current_setting('app.instance_id', true)
            )
            WITH CHECK (
                {col}::text = current_setting('app.instance_id', true)
            )
        """))

    # ----------------------------------------------------------------
    # STEP 4: G8 -- revoke audit-log mutation from app role
    # ----------------------------------------------------------------
    op.execute(sa.text(
        "REVOKE UPDATE, DELETE ON admin_audit_logs FROM luciel_app"
    ))


def downgrade() -> None:
    # Intentional no-op: STEP 1 (wipe) is irreversible.
    # Recovery: RDS point-in-time-recovery to the timestamp BEFORE this
    # migration ran. The migration is gated by Arc 9 5-gate deploy
    # (lint -> unit -> integration -> RLS-leak -> smoke) so a bad
    # rollout is caught before this DDL touches prod.
    raise RuntimeError(
        "arc9_1_a_tenant_isolation_seal: downgrade is intentionally "
        "unsupported. Use RDS point-in-time-recovery to restore."
    )
