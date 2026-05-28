"""Arc 10 Gap 7 closure: ReactivationService._inverse_restore_table
must be column-aware about the soft-delete timestamp column.

Anchored to Architecture v1 \u00a73.6 (lifecycle / closure / 30-day grace)
and \u00a73.6.2 step 3 (closure cascades per 3.6.1 across all instances;
reactivation is the inverse). Different per-admin tables use different
soft-delete timestamp columns:

  * conversations / identity_claims -> deactivated_at
  * instances -> soft_deleted_at
  * api_keys / memory_items / sessions / user_invites /
    scope_assignments -> no timestamp column

The original implementation hard-coded ``deactivated_at = NULL``,
which made the reactivate-complete leg crash with UndefinedColumn
on every table that does not have it -- breaking Customer Journey
\u00a78 (Marcus reactivates their account within the grace window).
"""
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_PATH = (
    REPO_ROOT / "app" / "services" / "reactivation_service.py"
)


def _service_src() -> str:
    return SERVICE_PATH.read_text(encoding="utf-8")


def test_inverse_restore_discovers_timestamp_column_at_runtime():
    """The method must discover the soft-delete timestamp column at
    runtime, not hardcode a single name.
    """
    src = _service_src()
    idx = src.find("def _inverse_restore_table(")
    assert idx >= 0, "_inverse_restore_table method must exist"
    body = src[idx:idx + 4500]
    # Must enumerate candidate column names.
    assert '"deactivated_at"' in body and '"soft_deleted_at"' in body, (
        "_inverse_restore_table must enumerate both 'deactivated_at' "
        "and 'soft_deleted_at' as candidate timestamp columns. The "
        "previous hard-coded shape crashed on every table that does "
        "not have 'deactivated_at'."
    )
    # Must look up the column via information_schema, not assume.
    assert "information_schema.columns" in body, (
        "Column existence must be discovered via information_schema."
    )


def test_inverse_restore_does_not_unconditionally_set_deactivated_at():
    """Reject the old hardcoded ``deactivated_at = NULL`` shape that
    appeared in BOTH branches of the original if/else. The new shape
    builds the SET clause conditionally based on which column the
    table actually carries.
    """
    src = _service_src()
    idx = src.find("def _inverse_restore_table(")
    body = src[idx:idx + 4500]
    # The old code had two literal ``deactivated_at = NULL`` strings,
    # one in each branch. The fixed code has it conditionally appended
    # via an f-string. Count the literal occurrences:
    literal_count = body.count("deactivated_at = NULL")
    # At most one occurrence is allowed (could appear in a docstring
    # explaining what the column is, but not as runnable SQL).
    assert literal_count <= 1, (
        f"Found {literal_count} literal 'deactivated_at = NULL' "
        "occurrences in _inverse_restore_table. The column-aware fix "
        "should not contain more than one literal occurrence; the SET "
        "clause must be built from the discovered column name."
    )


def test_inverse_restore_uses_dynamic_set_clause():
    """The SET clause must be built dynamically from the discovered
    timestamp column name (or omit the timestamp reset for tables
    that don't have one).
    """
    src = _service_src()
    idx = src.find("def _inverse_restore_table(")
    body = src[idx:idx + 4500]
    # The dynamic SET clause uses an f-string with the discovered name.
    assert "set_clauses" in body or "set_sql" in body, (
        "Method must build the SET clause from a list/joined string "
        "(set_clauses / set_sql) rather than two hardcoded SQL bodies."
    )
