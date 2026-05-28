"""Arc 11 Step 4 (d2) — rename stale policy names on knowledge_chunks.

Revision ID: arc11_d2_rls_chunks_postrename_verify
Revises: arc11_d1_rls_knowledge_sources
Create Date: 2026-05-28

Why this migration exists
-------------------------

Step 2 renamed the table ``knowledge_embeddings`` → ``knowledge_chunks``.
Postgres preserves RLS state across ``ALTER TABLE ... RENAME``:
policies stay attached to the table by OID, and the table's
``pg_class.relrowsecurity`` / ``relforcerowsecurity`` flags carry
through. But the **policy names** in ``pg_policies`` do not
auto-rename; a policy created as
``knowledge_embeddings_tenant_isolation`` is still called that even
though its host table is now ``knowledge_chunks``.

Two policies sit on the (renamed) chunks table at this point in the
migration graph:

  * ``knowledge_embeddings_tenant_isolation`` — Arc 9 C3.3, then
    re-pointed to admin_id by Arc 9.2 PR #97.
  * ``knowledge_embeddings_instance_isolation`` — Arc 9 C4.3b.

Both still function correctly: PostgreSQL identifies them by
``(polname, polrelid)``, not by name alone, and the predicates
remain valid. The stale names are a hygiene issue, not a
correctness issue — but they are also a *future-trip-hazard*: the
next person who tries to ``DROP POLICY knowledge_chunks_tenant_isolation
ON knowledge_chunks`` (the obvious-from-table-name DDL) will be
silently no-op'd.

This migration renames each policy whose name starts with
``knowledge_embeddings_`` and which sits on the ``knowledge_chunks``
table to start with ``knowledge_chunks_`` instead. It is written as
a discovery-loop against ``pg_policies`` so any policy variant we
missed in inventory is still caught.

Out of scope
------------

Re-shaping the policy *predicates* themselves. The NULL-permissive
read-side carveout for platform-curated chunks (Arc 9 C3.3's
``admin_id IS NULL OR admin_id = …`` clause) is intentional and
documented in Arc 9 C11 as one of the four tables deliberately
excluded from the strict-tenant flip. We rename names only.

Rollback
--------

``downgrade()`` performs the inverse rename: any policy on
``knowledge_chunks`` whose name starts with ``knowledge_chunks_``
goes back to ``knowledge_embeddings_``. Symmetric, so the migration
graph remains reversible. (Inverse is technically slightly
over-broad — it would also rename a future Step-4-or-later policy
named ``knowledge_chunks_foo`` to ``knowledge_embeddings_foo`` —
but at downgrade time we have not yet shipped any such policy.)
"""
from __future__ import annotations

from alembic import op


revision = "arc11_d2_rls_chunks_postrename_verify"
down_revision = "arc11_d1_rls_knowledge_sources"
branch_labels = None
depends_on = None


def _rename_policies(old_prefix: str, new_prefix: str) -> None:
    """Walk ``pg_policies`` for the chunks table, rename anything
    starting with ``old_prefix`` to start with ``new_prefix``.

    ``ALTER POLICY`` does not have an ``IF EXISTS`` form, but we
    iterate only over rows we know exist, so guarding is unnecessary.
    """
    op.execute(
        f"""
        DO $$
        DECLARE
            r RECORD;
            old_name TEXT;
            new_name TEXT;
        BEGIN
            FOR r IN
                SELECT polname
                  FROM pg_policies
                 WHERE schemaname = 'public'
                   AND tablename  = 'knowledge_chunks'
                   AND polname LIKE '{old_prefix}%'
            LOOP
                old_name := r.polname;
                new_name := '{new_prefix}'
                          || substring(old_name FROM length('{old_prefix}') + 1);
                EXECUTE format(
                    'ALTER POLICY %I ON knowledge_chunks RENAME TO %I',
                    old_name, new_name
                );
            END LOOP;
        END $$;
        """
    )


def upgrade() -> None:
    """Audit + rename stale policy names."""
    # Catch the legacy ``knowledge_embeddings_`` prefix; this is the
    # primary case the brief calls out.
    _rename_policies(
        old_prefix="knowledge_embeddings_",
        new_prefix="knowledge_chunks_",
    )


def downgrade() -> None:
    """Inverse rename. See the module docstring on the slight
    over-broadness of this loop — it is safe at the current head
    where no other ``knowledge_chunks_*`` policies exist on the
    chunks table."""
    _rename_policies(
        old_prefix="knowledge_chunks_",
        new_prefix="knowledge_embeddings_",
    )
