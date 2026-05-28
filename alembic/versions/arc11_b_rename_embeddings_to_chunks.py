"""Arc 11 Step 2 — rename knowledge_embeddings to knowledge_chunks.

Revision ID: arc11_b_rename_embeddings_to_chunks
Revises: arc11_a_knowledge_sources_schema
Create Date: 2026-05-28

Why this migration exists
-------------------------

Per Architecture v1 §3.2, the Arc 11 doctrinal shape is two tables —
``knowledge_sources`` (provenance) and ``knowledge_chunks`` (vectors).
Step 1 created the new sources table and added the additive FK
column ``source_fk``. This step is the table rename:

    knowledge_embeddings  ->  knowledge_chunks

It is **schema-only**, not behavioural. The model class
``KnowledgeEmbedding`` is renamed to ``KnowledgeChunk`` in
``app/models/knowledge.py`` and a backwards-compat alias
``KnowledgeEmbedding = KnowledgeChunk`` keeps all existing import
sites working unchanged. Step 3 (``arc11/c-repository``) then
migrates each caller off the legacy name file-by-file.

What gets renamed
-----------------

1. ``ALTER TABLE knowledge_embeddings RENAME TO knowledge_chunks``.
2. Every index whose name carries the old prefix
   ``ix_knowledge_embeddings_*`` is renamed to
   ``ix_knowledge_chunks_*``. The exact set of live indexes is
   discovered at runtime via ``pg_indexes`` because several have
   been added/dropped across Arc 5..10 and a hard-coded list would
   drift. Indexes without the prefix (e.g. ``ix_knowledge_scope``,
   ``ix_knowledge_embeddings_scope_source``) are renamed
   defensively under the same discovery query so any new arrival
   from a later migration on top of this one is caught too.
3. The FK constraints carrying the old name:
     * ``fk_knowledge_embeddings_luciel_instance_id``  (Arc 5)
     * ``fk_knowledge_embeddings_source_fk``           (Arc 11 Step 1)
   Renamed via ``ALTER TABLE ... RENAME CONSTRAINT``.

What this migration does NOT touch
----------------------------------

* **RLS policies.** Postgres ``ALTER TABLE ... RENAME`` automatically
  moves attached policies to the new table; their *names* are
  unchanged and still read ``knowledge_embeddings_*``. Step 4
  (``arc11/d-rls``) revisits the policies. Per the ARC11_PLAN.md
  §9 sequencing, Step 2 must not alter RLS.
* **HNSW or any new vector index.** Deferred to Step 4 where the
  table shape is finalised.
* **Legacy string ``source_id`` column / ``agent_id`` column.**
  Locked-deferred to Step 11.
* **The model attribute named ``source``** on the renamed
  ``KnowledgeChunk`` (free-text string reference) — Step 11.

Production safety
-----------------

ARC11_PLAN.md §12: ``knowledge_embeddings`` has zero rows. The
rename is metadata-only on an empty table and runs in milliseconds.
``ALTER TABLE ... RENAME`` takes an ACCESS EXCLUSIVE lock briefly;
on an empty table this is negligible.

Rollback
--------

``downgrade()`` reverses the rename of table, indexes, and FK
constraints in symmetric order. Safe because the table is empty
and no Step-3-or-later code has migrated off the alias yet at the
time this migration first ships.
"""
from __future__ import annotations

from alembic import op


# ---------------------------------------------------------------------
# Alembic identifiers.
# ---------------------------------------------------------------------
revision = "arc11_b_rename_embeddings_to_chunks"
down_revision = "arc11_a_knowledge_sources_schema"
branch_labels = None
depends_on = None


# Constraint renames are a known finite set (one per FK with the old
# prefix). Indexes are discovered dynamically because the live set has
# shifted across migrations.
_FK_RENAMES_UP = [
    # (old_constraint_name, new_constraint_name)
    (
        "fk_knowledge_embeddings_luciel_instance_id",
        "fk_knowledge_chunks_luciel_instance_id",
    ),
    (
        "fk_knowledge_embeddings_source_fk",
        "fk_knowledge_chunks_source_fk",
    ),
]


def _rename_indexes(old_prefix: str, new_prefix: str) -> None:
    """Rename every index on the (now renamed) table whose name starts
    with ``old_prefix`` to use ``new_prefix`` instead. Runs against the
    catalog after the table rename so we see all live indexes whatever
    their creation history.

    Also handles the special-case index ``ix_knowledge_scope`` (no
    ``_embeddings`` infix) by leaving it untouched — its name does
    not embed the old table name. The composite
    ``ix_knowledge_embeddings_scope_source`` does carry the prefix and
    is picked up by the prefix sweep.
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
                SELECT indexname
                  FROM pg_indexes
                 WHERE schemaname = 'public'
                   AND tablename = 'knowledge_chunks'
                   AND indexname LIKE '{old_prefix}%'
            LOOP
                old_name := r.indexname;
                new_name := '{new_prefix}' || substring(old_name from length('{old_prefix}') + 1);
                EXECUTE format('ALTER INDEX %I RENAME TO %I', old_name, new_name);
            END LOOP;
        END $$;
        """
    )


def upgrade() -> None:
    """Rename the table, its indexes, and its FK constraints."""

    # -----------------------------------------------------------------
    # 1. The table rename itself. Policies follow the table by OID.
    # -----------------------------------------------------------------
    op.execute(
        "ALTER TABLE knowledge_embeddings RENAME TO knowledge_chunks"
    )

    # -----------------------------------------------------------------
    # 2. Indexes — discovered dynamically against pg_indexes.
    # -----------------------------------------------------------------
    _rename_indexes(
        old_prefix="ix_knowledge_embeddings_",
        new_prefix="ix_knowledge_chunks_",
    )

    # -----------------------------------------------------------------
    # 3. FK constraints with the old name.
    # -----------------------------------------------------------------
    # ALTER TABLE ... RENAME CONSTRAINT does not have an "IF EXISTS"
    # form in PostgreSQL prior to 17. Guard each rename in a DO block
    # so a constraint that was dropped by some earlier migration we
    # missed in inventory does not break the upgrade.
    for old_name, new_name in _FK_RENAMES_UP:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                      FROM pg_constraint
                     WHERE conname = '{old_name}'
                )
                THEN
                    EXECUTE 'ALTER TABLE knowledge_chunks '
                            'RENAME CONSTRAINT {old_name} '
                            'TO {new_name}';
                END IF;
            END $$;
            """
        )


def downgrade() -> None:
    """Reverse the rename of FKs, indexes, and the table."""

    # -----------------------------------------------------------------
    # 3. FK constraints first (still on the new-name table).
    # -----------------------------------------------------------------
    for old_name, new_name in _FK_RENAMES_UP:
        # Reverse direction: new_name -> old_name.
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                      FROM pg_constraint
                     WHERE conname = '{new_name}'
                )
                THEN
                    EXECUTE 'ALTER TABLE knowledge_chunks '
                            'RENAME CONSTRAINT {new_name} '
                            'TO {old_name}';
                END IF;
            END $$;
            """
        )

    # -----------------------------------------------------------------
    # 2. Indexes — symmetric prefix swap.
    # -----------------------------------------------------------------
    _rename_indexes(
        old_prefix="ix_knowledge_chunks_",
        new_prefix="ix_knowledge_embeddings_",
    )

    # -----------------------------------------------------------------
    # 1. Table rename back.
    # -----------------------------------------------------------------
    op.execute(
        "ALTER TABLE knowledge_chunks RENAME TO knowledge_embeddings"
    )
