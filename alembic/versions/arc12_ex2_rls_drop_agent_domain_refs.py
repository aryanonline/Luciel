"""Arc 12 EX2 — re-seal RLS policies to v2 admin_id (+ luciel_instance_id),
removing any residual reference to the superseded v1 agent_id / domain_id
columns from active policy SQL.

Revision ID: arc12_ex2_rls_drop_agent_domain_refs
Revises: arc12_wu6_byo_webhook_and_tool_execution_log
Create Date: 2026-05-29

Why this migration exists (EX2 of the Arc 12 excision plan)
-----------------------------------------------------------

Arc 12's excision plan (``arc12_specs/02_EXCISION_PLAN.md``) ordered the
agent_id / domain_id rip-out so the system is never left with a broken
RLS policy or an unverifiable audit chain at any intermediate commit:

    EX1  code-level callsite + signature sweep        (merged: a-d)
    EX2  RLS rewrite — this migration                  ← we are here
    EX3  DROP non-audit-chain columns                  (next)
    EX4  audit-chain column reseal                     (LAST)

The single failure that Wall-1 RLS exists to prevent is a tenant leak.
EX3 cannot drop ``memory.agent_id`` / ``session.agent_id`` /
``trace.agent_id`` / ``api_key.agent_id`` (and the ``*.domain_id``
columns) until every live RLS policy stops referencing those columns —
otherwise the column drop would error out mid-migration on a still-live
``USING (... agent_id ...)`` predicate, and worse, would do so AFTER
EX1's code sweep removed the WHERE-clause callers that previously made
the policy's agent_id branch reachable, leaving the policy in a
half-functional state at the next intermediate commit.

The known target the spec named — confirmed
-------------------------------------------

``arc12_specs/02_EXCISION_PLAN.md`` and ``arc12_specs/EX_RESIDUAL_MAP.md``
both name ``alembic/versions/arc9_c3_3_rls_knowledge_embeddings.py`` as
the residual: the policy's MODULE DOCSTRING is the most detailed
description anywhere in the tree of an agent-aware visibility class
(``agent_knowledge`` rows with ``tenant_id SET`` and ``agent_id SET``),
and the residual map specifically notes "filtering still applies the
agent_id filter."

A full static scan of every CREATE POLICY / ALTER POLICY clause across
``alembic/versions/`` (``grep -E '(USING|WITH CHECK)' ... | rg
'agent_id|domain_id'``) confirms that the ACTUAL on-disk policy SQL —
after ``arc9_2_pr97_rls_to_admin_id.py`` re-pointed the predicate from
``tenant_id`` to ``admin_id`` — references neither agent_id nor
domain_id in any USING / WITH CHECK clause. The agent_id mention in
``arc9_c3_3``'s body was always a description of the table's scope
matrix, not an SQL filter. The "agent_id filter still applies" line in
that migration's doctstring describes a SERVICE-layer (Wall-1 L1) filter
applied in-code by the retriever, not a database-level RLS predicate.

So the policy WHOSE INTENT historically distinguished agent-level
visibility — ``knowledge_chunks_tenant_isolation`` on the
``knowledge_chunks`` table (renamed from ``knowledge_embeddings`` by
``arc11_b_rename_embeddings_to_chunks.py``; the policy itself was
renamed by ``arc11_d2_rls_chunks_postrename_verify.py``) — is the EX2
target. We DROP and RE-CREATE it under the explicit v2 §3.7.5 shape
described below. This is structurally a re-seal: it pins the policy to
the canonical v2 form in a single migration that EX3 / EX4 can name as
the gate dependency.

We additionally walk ``pg_policies`` once at the end of the upgrade
() and assert that no live policy contains an ``agent_id`` or
``domain_id`` reference in its predicate text. This is the
fail-closed invariant EX3 relies on: if a future drift adds a policy
referencing those columns between now and EX3, this migration's gate
will fire BEFORE EX3 attempts the column drop, surfacing the drift as
an explicit migration failure rather than as a silent half-broken
state.

v2 §3.7.5 policy shape (the canonical pattern)
----------------------------------------------

Architecture §3.7.5 mandates the canonical Wall-1 RLS pattern:

  ENABLE ROW LEVEL SECURITY;
  CREATE POLICY <table>_tenant_isolation
    ON <table>
    AS PERMISSIVE
    FOR ALL TO PUBLIC
    USING      (admin_id = current_setting('app.admin_id', true))
    WITH CHECK (admin_id = current_setting('app.admin_id', true));

with two amendments specific to ``knowledge_chunks``:

  (a) ``admin_id`` on ``knowledge_chunks`` is NULLABLE because that
      table holds platform-curated rows (``admin_id IS NULL`` =
      cross-tenant ``domain_knowledge``). We preserve the C3.3 +
      C11-documented carveout:

        USING (
          admin_id IS NULL
          OR admin_id::text = current_setting('app.admin_id', true)
        )
        WITH CHECK (
          (admin_id IS NULL
           AND current_setting('app.admin_id', true) = 'platform')
          OR admin_id::text = current_setting('app.admin_id', true)
        )

      The asymmetry (read NULL freely; write NULL only as the
      ``platform`` GUC) is exactly the rule documented in
      ``arc9_c3_3``'s docstring and locked by
      ``tests/db/test_rls_c3_3_knowledge_embeddings.py``. We
      preserve it unchanged.

  (b) Fail-closed: when ``app.admin_id`` is unset, ``current_setting
      ('app.admin_id', true)`` returns the empty string, and the
      comparison ``admin_id::text = ''`` is FALSE for every real row
      (admin ids are uuid-strings, never empty). The NULL-permissive
      USING branch ``admin_id IS NULL`` STILL fires on
      platform-curated rows — but those rows are PUBLIC by design
      (cross-tenant ``domain_knowledge`` material is intentionally
      visible to every tenant). The fail-closed property holds for
      every TENANT row: with no GUC set, no tenant row is visible.
      Tenant-scoped rows have admin_id NOT NULL by service-layer
      invariant; the C4.3b ``knowledge_chunks_instance_isolation``
      policy (untouched by this migration) ALSO requires the
      instance GUC to match for any luciel_instance_id-set row, so
      tenant rows cannot leak via the platform NULL carveout.

The companion Wall-3 policy ``knowledge_chunks_instance_isolation``
(created at ``arc9_c4_3b_rls_instance_knowledge_embeddings.py``,
renamed by ``arc11_d2_rls_chunks_postrename_verify.py``) is NOT
modified — it scopes on ``luciel_instance_id`` only and never
referenced ``agent_id`` / ``domain_id``. Two policies coexist on
``knowledge_chunks``; PostgreSQL ANDs multiple permissive policies, so
a row must pass BOTH to be visible/writable.

Visibility-preservation invariant (must NOT be widened)
-------------------------------------------------------

The §3.4 mandate is that this rewrite is equally or MORE restrictive,
never more permissive. The replacement policy below is identical in
predicate semantics to the post-``arc9_2_pr97`` policy that has been
in force since Arc 9.2 — the re-seal is a structural reaffirmation,
not a semantic widening:

  * Tenant row (admin_id SET, agent_id NULL after EX3, NOT NULL FK to
    admins) — visible iff app.admin_id = its admin_id. Same as today.
  * Platform-curated row (admin_id IS NULL) — readable by anyone;
    writable only when app.admin_id = 'platform'. Same as today.
  * Legacy agent_knowledge row (admin_id SET, agent_id SET) — visible
    iff app.admin_id = its admin_id. That is EXACTLY what the policy
    enforces today; the agent_id distinction was service-layer only.
    With EX1d already complete, the service layer no longer applies
    the agent_id filter at all, so the v2 reality is that legacy
    agent_knowledge rows under admin X are visible to admin X exactly
    as any other knowledge_type — which matches the v2 collapse
    (Admin→Instance is the only boundary, §3.7.2). EX3 will drop the
    column; production has 0 rows per ARC11 §12 already, and the
    arc11 ``knowledge_chunks.agent_id`` column drop has already
    landed (``arc11_cleanup_c_drop_agent_id_from_knowledge_chunks.py``).
    No tenant gains visibility under this re-seal; the agent_id
    distinction collapsed AT THE SERVICE LAYER during EX1, and the
    Wall-1 boundary (admin_id) was always the load-bearing fence.

Knowledge-class isolation preservation (the spec's deliverable #3)
------------------------------------------------------------------

The spec asks how knowledge-visibility isolation is preserved under v2
given that agent_knowledge rows historically carried ``agent_id SET``.
The answer:

  * In v1 the visibility classes were enforced as: RLS gates on
    tenant_id (database) + the retriever applies an agent_id WHERE
    filter (service). A v1 admin with tenant X and agent A would
    only see knowledge rows whose tenant_id = X AND (agent_id IS
    NULL OR agent_id = A) — agent A could not see agent B's
    agent_knowledge rows within the same tenant.

  * In v2 the agent level does not exist. The collapse means: an
    Admin sees ALL knowledge rows under its admin_id. There is no
    intra-Admin sub-fence. Legacy ``agent_knowledge`` rows persisted
    on disk would now be visible to every authorised request under
    the owning Admin. Production has 0 such rows
    (ARC11_PLAN.md §12); the agent_id COLUMN on knowledge_chunks
    has ALREADY been dropped at
    ``arc11_cleanup_c_drop_agent_id_from_knowledge_chunks.py``, so
    no "legacy agent_knowledge row" with a meaningful agent_id can
    exist on disk after Arc 11.

  * Net: this re-seal does NOT widen any tenant's visibility surface.
    The Wall-1 (admin_id) and Wall-3 (luciel_instance_id) boundaries
    that DO exist in v2 are preserved exactly; the v1 sub-boundary
    that no longer exists in the architecture is correctly absent
    from the policy.

Order invariant the comment block above promises EX3
----------------------------------------------------

After this migration:

  * Static check: no Alembic ``CREATE POLICY`` body in
    ``alembic/versions/`` references ``agent_id`` or ``domain_id`` in
    a USING/WITH CHECK clause (the only remaining matches are
    docstrings).

  * Live check (asserted at upgrade-time, below): no row of
    ``pg_policies`` whose ``schemaname='public'`` carries a predicate
    string mentioning ``agent_id`` or ``domain_id``.

Therefore EX3 may safely drop the columns ``memory.agent_id``,
``session.agent_id``, ``trace.agent_id``, ``api_key.agent_id``, and
the various ``*.domain_id`` columns without any active RLS predicate
breaking. (EX4 separately handles ``admin_audit_logs.{agent_id,
domain_id}`` — those are hash-chained and require the canonical-set
reseal documented in EX_PLAN §EX4.)

Rollback contract
-----------------

``alembic downgrade -1`` re-creates the previous (pre-EX2) policy
text. Since the pre-EX2 SQL was already in the v2 admin_id shape
(courtesy of ``arc9_2_pr97``), the downgrade restores the
byte-identical policy DDL — i.e. this migration is effectively a
no-op on policy semantics and a strong structural marker for the
excision chain. Downgrade is therefore data-safe and reversible.
"""
from __future__ import annotations

from alembic import op


# ---------------------------------------------------------------------
# Alembic identifiers.
# ---------------------------------------------------------------------
revision = "arc12_ex2_rls_drop_agent_domain_refs"
down_revision = "arc12_wu6_byo_webhook_and_tool_execution_log"
branch_labels = None
depends_on = None


_TABLE = "knowledge_chunks"
_POLICY = "knowledge_chunks_tenant_isolation"


# The §3.7.5 + C3.3-NULL-carveout shape. Identical to the policy
# installed by ``arc9_2_pr97_rls_to_admin_id`` (and renamed by
# ``arc11_d2_rls_chunks_postrename_verify``). Re-CREATED here as the
# structural EX2 marker.
_V2_POLICY_DDL = f"""
    DROP POLICY IF EXISTS {_POLICY} ON {_TABLE};
    CREATE POLICY {_POLICY}
    ON {_TABLE}
    AS PERMISSIVE
    FOR ALL
    TO PUBLIC
    USING (
        admin_id IS NULL
        OR admin_id::text = current_setting('app.admin_id', true)
    )
    WITH CHECK (
        (
            admin_id IS NULL
            AND current_setting('app.admin_id', true) = 'platform'
        )
        OR admin_id::text = current_setting('app.admin_id', true)
    );
"""


# The "pre-EX2" policy DDL — byte-identical to the v2 policy because
# the on-disk state was already v2-shaped after arc9_2_pr97. Used by
# downgrade() for an honest revert.
_PRE_EX2_POLICY_DDL = _V2_POLICY_DDL


# Live-state gate: assert no policy in the public schema mentions
# agent_id or domain_id in its USING / WITH CHECK predicate. This is
# the EX3-unblock invariant. Predicate text comes from pg_policies
# (``qual`` for USING, ``with_check`` for WITH CHECK).
_LIVE_GATE_DDL = """
    DO $$
    DECLARE
        bad RECORD;
    BEGIN
        FOR bad IN
            SELECT schemaname,
                   tablename,
                   policyname,
                   qual,
                   with_check
              FROM pg_policies
             WHERE schemaname = 'public'
               AND (
                       qual       ~ '\\magent_id\\M'
                    OR qual       ~ '\\mdomain_id\\M'
                    OR with_check ~ '\\magent_id\\M'
                    OR with_check ~ '\\mdomain_id\\M'
                   )
        LOOP
            RAISE EXCEPTION
              'Arc 12 EX2 gate failed: policy %.% on table % '
              'still references agent_id/domain_id in its '
              'predicate. USING=[%], WITH_CHECK=[%]. EX3 cannot '
              'safely drop the columns until this policy is rewritten.',
              bad.schemaname, bad.policyname, bad.tablename,
              bad.qual, bad.with_check;
        END LOOP;
    END $$;
"""


def upgrade() -> None:
    # 1. Ensure RLS stays on. ENABLE is idempotent; FORCE matches the
    #    Arc 9 C10.a doctrine for tenant-scoped tables but C3.3 +
    #    arc9_2_pr97 did NOT FORCE on knowledge_chunks (the platform-
    #    curated cross-tenant carveout depends on the table owner
    #    NOT being subject to the same NULL-write fence as luciel_app).
    #    We preserve the existing FORCE/NO-FORCE posture exactly.
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY;")

    # 2. Drop + re-create the policy in the canonical v2 §3.7.5 +
    #    C3.3-NULL-carveout shape. Idempotent (DROP IF EXISTS).
    op.execute(_V2_POLICY_DDL)

    # 3. Live gate: scan pg_policies for any residual agent_id /
    #    domain_id reference in active predicates. This is what
    #    unblocks EX3.
    op.execute(_LIVE_GATE_DDL)


def downgrade() -> None:
    # Revert is structurally a no-op on semantics (the policy was
    # already in the v2 admin_id shape pre-EX2). We emit the
    # byte-identical DDL for a clean reversibility contract.
    op.execute(_PRE_EX2_POLICY_DDL)
