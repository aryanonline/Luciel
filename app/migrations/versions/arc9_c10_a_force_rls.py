"""Arc 9 C10.a -- flip every RLS-enabled table to FORCE ROW LEVEL SECURITY.

Background (the C10 reality-check, 2026-05-25):

  Arc 9 C3-C6 installed 26 RLS policies on 17 tables, all using
  ENABLE ROW LEVEL SECURITY. Under PostgreSQL semantics, ENABLE
  applies policies only to non-owner roles. The Luciel backend
  currently connects as `luciel_admin` -- the role that OWNS those
  tables -- so RLS was silently bypassed in production. The Arc 9
  C9 envelope claimed RLS was active in prod; that claim was wrong.

  The original Arc 9 design (see arc9_c3_1_rls_admin_audit_logs.py
  line 66) noted: ``flips to FORCE in the envelope-close commit as
  the final hardening step``. That step never landed -- C9 shipped
  as a cosmetic feature-flag flip instead. This migration closes
  that gap.

What FORCE does:

  FORCE ROW LEVEL SECURITY makes the policy apply to the table
  owner as well as ordinary users. Combined with the existing
  per-tenant USING/WITH CHECK predicates, this means EVERY query
  -- including queries from the backend connecting as luciel_admin
  -- is fenced by `app.admin_id` / `app.instance_id` GUCs.

  The BYPASSRLS role (`luciel_ops`, created in arc9_c6_1) is the
  ONLY escape hatch and is reserved for retention/deletion sweeps.

Companion migration arc9_c10_b_luciel_app_role.py creates a new
non-owner `luciel_app` role that the backend will use; switching
DATABASE_URL is part of the same deploy.

Refs:
  ARC9_RUNBOOK §C3 (line 66 "flips to FORCE in envelope-close")
  ARC9_ENVELOPE corrigendum (Drive, 2026-05-25)
"""
from __future__ import annotations

from alembic import op


revision = "arc9_c10_a_force_rls"
down_revision = "arc9_c6_2_admin_audit_immutability"
branch_labels = None
depends_on = None


# The 17 RLS-enabled tables, ordered by Wall layer (matches the
# C3-C5 install order). Verified 2026-05-25 against pg_class.relrowsecurity
# on the prod cluster after arc9 upgrade head.
FORCE_TABLES = (
    # C3 Wall-1 tenant isolation
    "admin_audit_logs",
    "traces",
    "memory_items",
    "conversations",
    "sessions",
    "subscriptions",
    "scope_assignments",
    "knowledge_embeddings",
    "api_keys",
    "user_invites",
    "user_consents",
    "identity_claims",
    "instances",
    "admin_widget_domains",
    "retention_policies",
    "deletion_logs",
    # C5 messages (added in arc9_c5)
    "messages",
)


def upgrade() -> None:
    for table in FORCE_TABLES:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")


def downgrade() -> None:
    # Symmetric: revert to ENABLE-only (policies stay installed,
    # but owner-role traffic bypasses them again). This is the
    # pre-C10 state, NOT a full RLS teardown.
    for table in FORCE_TABLES:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
