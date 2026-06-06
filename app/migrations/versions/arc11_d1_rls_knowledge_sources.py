"""Arc 11 Step 4 (d1) — RLS policies on knowledge_sources.

Revision ID: arc11_d1_rls_knowledge_sources
Revises: arc11_b_rename_embeddings_to_chunks
Create Date: 2026-05-28

Why this migration exists
-------------------------

Step 1 created ``knowledge_sources`` but did not install RLS — the
plan deliberately sequenced RLS into Step 4 so the Step-3 code-side
refactor (repository + retriever) could land independently and be
audited under non-fenced reads first.

This migration installs the standard Arc 9 RLS posture on the new
table. ``knowledge_sources`` has ``admin_id NOT NULL`` (unlike
``knowledge_chunks`` which carries platform-curated NULL-admin rows
— see arc9_c3_3 / arc9_c11 for that historical asymmetry), so its
policy is **RESTRICTIVE** with a strict equality check, matching
the Arc 9 C11 "strict-tenant tables" shape (instances, traces,
admin_audit_logs, etc.).

The policy posture
------------------

1. ``ENABLE ROW LEVEL SECURITY`` — turns RLS on.
2. ``FORCE ROW LEVEL SECURITY`` — makes RLS apply to the table
   owner too, per Arc 9 C10.a. Without FORCE, ``luciel_admin`` (the
   migration role) would bypass; the prod app role is
   ``luciel_app`` which is NOBYPASSRLS, but FORCE seals the
   ownership escape.
3. ``knowledge_sources_admin_isolation`` (RESTRICTIVE, FOR ALL) —
   the fail-closed tenant boundary. ``admin_id::text =
   current_setting('app.admin_id', true)`` for both USING and
   WITH CHECK. Restrictive policies AND together with permissive
   ones, so this fence holds regardless of any future permissive
   layer.

   When ``app.admin_id`` is unset (boot / no-GUC), the
   ``current_setting(..., true)`` returns ``NULL``; ``admin_id =
   NULL`` is NULL in SQL three-valued logic; the RESTRICTIVE
   USING clause then evaluates to NULL, which RLS treats as
   "deny." Fail-closed by construction. Matches the Arc 9 WS4b
   doctrine ("Empty-tier / no-GUC = deny, not silent-empty
   default", arc9 C22 corrigendum).

Why we deliberately do NOT add an ``instance_id`` policy here
-------------------------------------------------------------

``knowledge_chunks`` carries both a tenant-isolation policy
(``knowledge_embeddings_tenant_isolation``, post-rename) and an
instance-isolation policy (``knowledge_embeddings_instance_isolation``)
— the Wall-1 + Wall-3 doctrine from Arc 9 C3/C4. For
``knowledge_sources``, Vision §5.1's three-layer defence locates
**instance scoping at the service layer (L1)**, not at RLS.
Architecture v1 §3.7.1 confirms: the admin can read across their
own instances (Ownership Model C), so an instance-level fence at
the database layer would prevent legitimate cross-instance admin
views. We rely on the L1 service-layer filter
(``KnowledgeSourceRepository.list_sources_for_instance`` already
takes ``luciel_instance_id`` as a mandatory kw-arg, per Step 3).

Grants
------

Arc 9 C10.b set ``ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT
SELECT, INSERT, UPDATE, DELETE ON TABLES TO luciel_app``. Tables
created after that migration — including ``knowledge_sources`` —
inherit the grant automatically. We do NOT issue an explicit grant
here to avoid two sources of truth; the default-privilege ALTER is
the canonical mechanism.

Rollback
--------

``downgrade()`` reverses everything: drop both policies, DISABLE
RLS. Safe because the policy is fail-closed: dropping it widens
visibility, never narrows it, so there is no chance of a downgrade
locking the operator out of legitimate rows.
"""
from __future__ import annotations

from alembic import op


revision = "arc11_d1_rls_knowledge_sources"
down_revision = "arc11_b_rename_embeddings_to_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Turn RLS on.
    op.execute(
        "ALTER TABLE knowledge_sources ENABLE ROW LEVEL SECURITY;"
    )
    op.execute(
        "ALTER TABLE knowledge_sources FORCE ROW LEVEL SECURITY;"
    )

    # 2. RESTRICTIVE tenant fence — fail-closed on unset GUC, strict
    #    equality on the NOT NULL admin_id column. This is the Arc 9
    #    C11 "strict-tenant" shape. FOR ALL covers SELECT / INSERT /
    #    UPDATE / DELETE in one policy; PostgreSQL applies USING to
    #    read paths and WITH CHECK to write paths.
    op.execute(
        """
        CREATE POLICY knowledge_sources_admin_isolation
        ON knowledge_sources
        AS RESTRICTIVE
        FOR ALL
        TO PUBLIC
        USING (admin_id::text = current_setting('app.admin_id', true))
        WITH CHECK (admin_id::text = current_setting('app.admin_id', true));
        """
    )

    # 3. A second, INSERT-only WITH CHECK policy mirroring the
    #    pattern in arc10_lifecycle_subsystem.py's data_export_jobs
    #    install (defence-in-depth: an explicit FOR INSERT policy
    #    documents the write-side fence even though the FOR ALL
    #    policy above also enforces it). Named with the
    #    ``_write`` suffix per the brief.
    op.execute(
        """
        CREATE POLICY knowledge_sources_admin_isolation_write
        ON knowledge_sources
        AS RESTRICTIVE
        FOR INSERT
        TO PUBLIC
        WITH CHECK (admin_id::text = current_setting('app.admin_id', true));
        """
    )


def downgrade() -> None:
    # Reverse order: drop the policies, then disable RLS.
    op.execute(
        "DROP POLICY IF EXISTS knowledge_sources_admin_isolation_write "
        "ON knowledge_sources;"
    )
    op.execute(
        "DROP POLICY IF EXISTS knowledge_sources_admin_isolation "
        "ON knowledge_sources;"
    )
    op.execute(
        "ALTER TABLE knowledge_sources NO FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        "ALTER TABLE knowledge_sources DISABLE ROW LEVEL SECURITY;"
    )
