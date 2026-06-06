"""Arc 9 C3.3 -- RLS policy on knowledge_embeddings (Wall 1 Layer 2).

Continues the Wall 1 Layer 2 rollout. knowledge_embeddings is the
first NULL-permissive table in the sequence -- its tenant_id column
is nullable=True because the table holds FIVE knowledge types with
different scope shapes (documented in app/models/knowledge.py:
``Scoping rules`` block):

    knowledge_type          tenant_id    domain_id    agent_id    visibility
    --------------------    ---------    ---------    --------    ------------
    domain_knowledge        NULL         SET          NULL        shared in domain
    tenant_document         SET          NULL/SET     NULL        per-tenant
    role_instruction        SET          SET          NULL        per-tenant per-role
    agent_knowledge         SET          NULL/SET     SET         LEGACY (pre-Step-24.5)
    luciel_knowledge        SET          NULL/SET     NULL        per-tenant per-Luciel

The domain_knowledge rows (tenant_id IS NULL) are deliberately
cross-tenant: they represent shared educational/reference material
visible to every tenant in the domain. A naive ``tenant_id =
current_setting()`` policy would deny ALL domain_knowledge for
every admin (NULL never equals anything in SQL), breaking the
knowledge retriever for the most common query path.

RLS policy design:

  USING clause (read-side):
    ``tenant_id IS NULL OR tenant_id = current_setting(...)``

    Reads ALL domain_knowledge rows (cross-tenant by design) PLUS
    rows scoped to the current admin. This is the intended
    visibility surface for the KnowledgeRetriever.

  WITH CHECK clause (write-side):
    ``(tenant_id IS NULL
        AND current_setting('app.admin_id', true) = 'platform')
       OR tenant_id = current_setting('app.admin_id', true)``

    A regular admin can ONLY insert/update rows with their own
    tenant_id. Inserting a domain_knowledge row (tenant_id NULL)
    requires the GUC to be explicitly set to 'platform', which
    only the platform-admin tooling does. This closes the most
    dangerous write-leak: an ordinary admin uploading content as
    if it were platform-curated.

  The asymmetry between USING and WITH CHECK here is intentional
  and matches the documented Scoping Rules. Most RLS policies are
  symmetric (USING == WITH CHECK); this one is not, and the
  asymmetry is the WHOLE POINT of the table's scope semantics.

What this does NOT cover:

  * The legacy ``agent_knowledge`` rows (tenant_id SET, agent_id SET)
    are gated only by tenant_id at this layer. Service-layer L1
    filtering still applies the agent_id filter. RLS at the
    agent-level would be Wall 2 territory, deferred indefinitely
    until/unless we adopt agent-level isolation (currently
    out-of-scope per ARC9_RUNBOOK).

  * The luciel_instance_id scope (Wall 3) is gated separately by
    C4 service-layer audit fixes; an admin with multiple instances
    can read across them (correct behaviour per Ownership Model C).

Reversibility: zero-data-impact downgrade (drop policy, disable RLS).

Refs ARC9_RUNBOOK §C3.
"""

from __future__ import annotations

from alembic import op


revision = "arc9_c3_3_rls_knowledge_embeddings"
down_revision = "arc9_c3_2g_rls_scope_assignments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE knowledge_embeddings ENABLE ROW LEVEL SECURITY;"
    )
    op.execute(
        """
        CREATE POLICY knowledge_embeddings_tenant_isolation
        ON knowledge_embeddings
        AS PERMISSIVE
        FOR ALL
        TO PUBLIC
        USING (
            tenant_id IS NULL
            OR tenant_id = current_setting('app.admin_id', true)
        )
        WITH CHECK (
            (
                tenant_id IS NULL
                AND current_setting('app.admin_id', true) = 'platform'
            )
            OR tenant_id = current_setting('app.admin_id', true)
        );
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS knowledge_embeddings_tenant_isolation "
        "ON knowledge_embeddings;"
    )
    op.execute(
        "ALTER TABLE knowledge_embeddings DISABLE ROW LEVEL SECURITY;"
    )
