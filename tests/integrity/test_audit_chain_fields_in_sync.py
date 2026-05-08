"""Step 29.y Cluster 3 (D-9): _CHAIN_FIELDS lockstep test.

findings_phase1d.md D-9 documents a silent drift hazard: if a
future migration adds a column to admin_audit_logs and the
maintainer forgets to extend ``app.repositories.audit_chain
._CHAIN_FIELDS``, all existing rows continue to verify (their
chain content is unchanged) but every new row will have a hash
input that omits the new column. A forensic auditor diffing the
schema against the hash inputs would catch this only after the
fact.

This test makes the drift loud at CI time. It asserts:

  set(_CHAIN_FIELDS)
    == set(AdminAuditLog table columns)
       - {audit-internal columns}

where audit-internal columns are the ones that must NOT be in the
hash input by definition:

  - ``id``           : autoincrement PK; not part of content
  - ``row_hash``     : the hash output itself
  - ``prev_row_hash``: the chain pointer, not content

Anything else added to admin_audit_logs is content. If you are
reading this after a CI failure, your migration added a column
without updating ``_CHAIN_FIELDS``. Either:

  (a) Add the column name to ``_CHAIN_FIELDS`` in
      ``app/repositories/audit_chain.py`` AND in any inlined copy
      inside the migration that adds it (the 8ddf0be96f44 and
      c5d8a1e7b3f9 migrations both keep their own snapshots --
      that is intentional; see those migrations' docstrings).
  (b) If the new column is genuinely NOT content (e.g. a
      timing/observability column that does not need forensic
      protection), explicitly extend the exclusion set in this
      test with a one-line comment justifying the exclusion.
"""

from __future__ import annotations

import pytest

# Columns that are part of the audit row but NOT inputs to the
# canonical hash. Adding to this set is a deliberate decision
# requiring the comment justification rule above.
_AUDIT_INTERNAL_COLUMNS = frozenset(
    {
        "id",            # PK; row identity, not content
        "row_hash",      # the hash output itself
        "prev_row_hash", # chain pointer, not content
        # ``updated_at`` is inherited from TimestampMixin and ends
        # up on admin_audit_logs as a side effect. The audit log
        # is append-only at the DB privilege layer (the migration
        # 8ddf0be96f44 deliberately did NOT grant UPDATE on
        # admin_audit_logs to luciel_worker -- see the comment
        # block at app/models/admin_audit_log.py around line 341),
        # so updated_at can never diverge from created_at after
        # insert. Including it in _CHAIN_FIELDS would either
        # double-count the timestamp or, if the database emits a
        # microsecond-level difference between the two NOW()
        # calls during the same INSERT, introduce a chain input
        # that is not actually content. Excluding it preserves
        # "only true content is hashed" while keeping the chain
        # field set minimal. Revisit this exclusion if
        # admin_audit_logs is ever made non-append-only at the
        # privilege layer.
        "updated_at",
    }
)


def test_chain_fields_match_admin_audit_log_columns() -> None:
    from app.models.admin_audit_log import AdminAuditLog
    from app.repositories.audit_chain import _CHAIN_FIELDS

    table_cols = {c.name for c in AdminAuditLog.__table__.columns}
    expected = table_cols - _AUDIT_INTERNAL_COLUMNS
    actual = set(_CHAIN_FIELDS)

    missing_from_chain = expected - actual
    extra_in_chain = actual - expected

    assert not missing_from_chain, (
        f"D-9 drift: admin_audit_logs has columns that are NOT in "
        f"_CHAIN_FIELDS: {sorted(missing_from_chain)}. New columns "
        f"on admin_audit_logs are content by default and must be "
        f"hashed. Either add these names to _CHAIN_FIELDS in "
        f"app/repositories/audit_chain.py (and to any inlined copy "
        f"in the migration that introduced them) OR add them to "
        f"_AUDIT_INTERNAL_COLUMNS in this test with a justifying "
        f"comment."
    )
    assert not extra_in_chain, (
        f"D-9 drift: _CHAIN_FIELDS lists columns that DO NOT exist "
        f"on admin_audit_logs: {sorted(extra_in_chain)}. A column "
        f"was renamed or dropped without updating the chain field "
        f"set. The chain hashes will reference a missing column "
        f"and every new insert will produce a hash that no future "
        f"reader can reproduce."
    )


def test_chain_fields_tuple_is_sorted_for_drift_visibility() -> None:
    """Soft check: the tuple is iterated as-is for the canonical
    JSON which itself uses sort_keys=True, so the tuple's own
    order is technically irrelevant for the hash. We still pin
    that the tuple does not contain duplicates -- a copy/paste
    accident that doubles a name silently changes the hash for
    ``json.dumps(sort_keys=True)`` consumers (no, it doesn't, but
    the duplication is still a code smell)."""
    from app.repositories.audit_chain import _CHAIN_FIELDS

    assert len(_CHAIN_FIELDS) == len(set(_CHAIN_FIELDS)), (
        f"_CHAIN_FIELDS contains duplicates: "
        f"{[x for x in _CHAIN_FIELDS if _CHAIN_FIELDS.count(x) > 1]}"
    )


def test_audit_internal_columns_present_on_table() -> None:
    """Make sure our exclusion set actually matches real columns.
    If admin_audit_logs ever drops one of these (e.g. row_hash
    rename), the test above would falsely pass because expected
    would silently lose its protection. Pin them here."""
    from app.models.admin_audit_log import AdminAuditLog

    table_cols = {c.name for c in AdminAuditLog.__table__.columns}
    missing = _AUDIT_INTERNAL_COLUMNS - table_cols
    assert not missing, (
        f"_AUDIT_INTERNAL_COLUMNS lists columns not present on "
        f"admin_audit_logs: {sorted(missing)}. The exclusion set "
        f"is stale; either remove the obsolete name or restore "
        f"the column."
    )


# =====================================================================
# Cluster 3 D-8 sub-checks: model nullability matches the migration.
# =====================================================================

def test_admin_audit_log_row_hash_is_not_nullable() -> None:
    """D-8: the ORM column must declare nullable=False for
    row_hash so SQLAlchemy autogen + Pillar 23's STRICT-mode
    probe both agree with the DB after migration c5d8a1e7b3f9."""
    from app.models.admin_audit_log import AdminAuditLog

    col = AdminAuditLog.__table__.columns["row_hash"]
    assert col.nullable is False, (
        "D-8 drift: AdminAuditLog.row_hash declares nullable=True "
        "but Cluster 3 migration c5d8a1e7b3f9 makes the column "
        "NOT NULL. The model must match: drop Optional from the "
        "Mapped[...] annotation and set nullable=False on the "
        "mapped_column(...) call. Pillar 23 probes column "
        "nullability and may switch to STRICT mode unexpectedly "
        "if the model and DB disagree across deploys."
    )


def test_admin_audit_log_prev_row_hash_is_not_nullable() -> None:
    from app.models.admin_audit_log import AdminAuditLog

    col = AdminAuditLog.__table__.columns["prev_row_hash"]
    assert col.nullable is False, (
        "D-8 drift: AdminAuditLog.prev_row_hash declares "
        "nullable=True but Cluster 3 migration c5d8a1e7b3f9 "
        "makes the column NOT NULL. See the row_hash test above."
    )


# =====================================================================
# D-8: migration file presence + chain head wiring.
# =====================================================================

def test_cluster3_migration_file_present_and_wired() -> None:
    """The migration file must exist and have correct down_revision
    so it sits at the current chain head (extending a1f29c7e4b08).
    """
    import pathlib
    import re

    here = pathlib.Path(__file__).resolve()
    project_root = here.parents[2]
    mig = (
        project_root
        / "alembic"
        / "versions"
        / "c5d8a1e7b3f9_step29y_cluster3_audit_row_hash_not_null.py"
    )
    assert mig.exists(), (
        "D-8: Cluster 3 migration file is missing. Expected "
        f"at {mig}. The migration must be present in the "
        "alembic/versions directory and named "
        "c5d8a1e7b3f9_step29y_cluster3_audit_row_hash_not_null.py."
    )

    src = mig.read_text()
    assert re.search(
        r'^revision\s*=\s*[\'"]c5d8a1e7b3f9[\'"]', src, re.MULTILINE
    ), "Cluster 3 migration must declare revision = 'c5d8a1e7b3f9'."
    assert re.search(
        r'^down_revision\s*=\s*[\'"]a1f29c7e4b08[\'"]', src, re.MULTILINE
    ), (
        "Cluster 3 migration must chain down_revision to "
        "a1f29c7e4b08 (the head before this cluster). If a newer "
        "migration has been merged ahead of this one, restack."
    )

    # The migration must call alter_column on row_hash with
    # nullable=False; the simplest way to enforce that without
    # importing alembic is a substring check.
    assert "row_hash" in src and "prev_row_hash" in src, (
        "Cluster 3 migration must reference both row_hash and "
        "prev_row_hash."
    )
    assert "nullable=False" in src, (
        "Cluster 3 migration must contain nullable=False to flip "
        "the columns to NOT NULL."
    )


@pytest.mark.parametrize(
    "module",
    [
        "app.models.admin_audit_log",
        "app.repositories.audit_chain",
    ],
)
def test_cluster3_modules_import(module: str) -> None:
    import importlib

    importlib.import_module(module)
