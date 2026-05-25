"""Arc 9 C3.4 — RLS on api_keys (read-permissive, write-strict).

Revision ID: arc9_c3_4_rls_api_keys
Revises: arc9_c3_3_rls_knowledge_embeddings
Create Date: 2026-05-24

WHAT
----
ENABLE ROW LEVEL SECURITY on api_keys and CREATE POLICY tenant_isolation
with **asymmetric** USING vs WITH CHECK semantics:

    USING       (read):  TRUE
    WITH CHECK  (write): (tenant_id IS NULL
                            AND current_setting('app.admin_id', true) = 'platform')
                         OR tenant_id = current_setting('app.admin_id', true)

WHY ASYMMETRIC — and why USING is intentionally permissive
----------------------------------------------------------
api_keys is the **authentication-perimeter table**. The
ApiKeyAuthMiddleware (`app/middleware/auth.py`) MUST be able to look up
a row by `key_hash` BEFORE any tenant context exists — that lookup IS
what produces the `tenant_id` that the rest of the request then runs
under. A strict-equal RLS policy on reads creates a chicken-and-egg:

    middleware opens session
      -> after_begin listener fires
        -> get_current_admin_id() returns None (no auth yet)
        -> SET LOCAL app.admin_id = ''
      -> middleware queries api_keys WHERE key_hash = :hash
        -> RLS policy: tenant_id = '' is never true
        -> 0 rows returned
      -> middleware returns 401 — every request fails

We considered three remediations:

  (A) Bootstrap sentinel GUC: middleware SETs app.admin_id =
      '__auth_bootstrap__' before validate_key() and re-SETs to the
      real tenant_id after. RLS USING clause permits the sentinel.
      Adds two SET LOCAL round-trips per request and a fragile
      middleware contract that future refactors can break silently.

  (B) Middleware refactor: validate_key() uses a raw connection that
      bypasses the after_begin listener. Couples auth to engine
      internals and breaks if anyone adds another listener.

  (C) **Permissive USING, strict WITH CHECK** (this commit). Treat
      api_keys as a system/auth table, not user data:

        - The structural defence on api_keys READS is the cryptographic
          unguessability of `key_hash` (SHA-256 of a 32-byte random raw
          key). The query is `WHERE key_hash = :hash` — RLS is the
          wrong layer to defend that; the right layer is the hash
          comparison itself.

        - The real RLS-shaped risk on api_keys is **writes**: an
          authenticated admin A using the admin-create-key endpoint
          (or compromised insider with DB access via the app role)
          INSERTing a row tagged with admin B's tenant_id, then
          presenting that key to impersonate admin B. WITH CHECK
          strict on writes blocks this at the database, independent
          of whether the service-layer L1 filter happens to forget
          the tenant_id check.

        - NULL tenant_id (platform-admin cross-tenant keys, like the
          one used by Stripe webhooks and the SES sink) reads work
          unchanged. Writes to NULL are gated to the 'platform'
          sentinel only — regular admins cannot mint platform-admin
          keys even if a service-layer bug forgets to validate.

Option C is the minimum surgery that achieves the actual security
goal (block cross-tenant key minting) without breaking the
authentication path. It is consistent with the C3.3 NULL-permissive
asymmetric pattern already established for knowledge_embeddings.

DEPLOY COUPLING
---------------
Migration + `rls_tenant_context_enabled=true` ECS env MUST ship in the
same deploy bundle (same constraint as C3.1, C3.2, C3.3). Migration-
first with flag off is a no-op (RLS is enabled but the GUC is never
set so USING=TRUE still admits all rows). Flag-first is a no-op (RLS
not yet enabled on this table).

CHAIN
-----
arc9_c3_3_rls_knowledge_embeddings -> arc9_c3_4_rls_api_keys

DEFERRED
--------
- Future C8-or-later: consider a column-level RLS or trigger-based
  audit on api_keys.tenant_id mutations (UPDATE that flips tenant_id
  is currently allowed under WITH CHECK as long as new tenant_id
  matches caller — an admin cannot flip THEIR OWN key to point at
  someone else, but they can flip a key they own back to themselves
  if it was somehow detached. This is acceptable for v1).
"""
from __future__ import annotations

from alembic import op


# Alembic identifiers.
revision = "arc9_c3_4_rls_api_keys"
down_revision = "arc9_c3_3_rls_knowledge_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Enable RLS + asymmetric policy on api_keys.

    See module docstring for the full doctrine. Summary:
      USING       = TRUE                  (auth-perimeter reads pass)
      WITH CHECK  = strict tenant equality OR platform-sentinel NULL
    """
    # 1) Enable RLS on the table. Until a policy matches, RLS denies
    #    everything for non-superusers. Step 2 immediately attaches a
    #    permissive USING so auth flows continue to work.
    op.execute("ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;")

    # 2) The tenant_isolation policy. USING is unconditionally true
    #    (auth perimeter requires this — see docstring). WITH CHECK
    #    enforces:
    #      - NULL tenant_id writes only when GUC = 'platform' sentinel
    #      - non-NULL tenant_id writes only when GUC matches exactly
    #    `current_setting('app.admin_id', true)` returns '' rather
    #    than raising when the GUC is unset, matching the C2 listener
    #    behaviour for background / unauthenticated paths. An unset
    #    GUC therefore cannot write NULL (would need GUC='platform')
    #    nor any specific tenant_id (would need GUC=that tenant_id).
    op.execute(
        """
        CREATE POLICY tenant_isolation ON api_keys
            FOR ALL
            USING (TRUE)
            WITH CHECK (
                (tenant_id IS NULL
                 AND current_setting('app.admin_id', true) = 'platform')
                OR tenant_id = current_setting('app.admin_id', true)
            );
        """
    )


def downgrade() -> None:
    """Drop the policy and disable RLS on api_keys.

    Reversible. The auth middleware does not depend on the policy
    existing — it depends only on api_keys being queryable, which
    is the pre-Arc-9 baseline.
    """
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON api_keys;")
    op.execute("ALTER TABLE api_keys DISABLE ROW LEVEL SECURITY;")
