"""Arc 10 re-open Gap 5 -- DowngradeGraceService contract regression.

Locks the surface that backs the customer-visible downgrade-grace
experience: 30-day read-only window after POST /billing/downgrade,
then enforcement (archive overflow across 5 axes) at day 30.

  C1  Service class + read-only error class exist.

  C2  Three primary methods exist with the contract the route + the
      grace-middleware depend on:
        * is_in_grace(admin_id) -> bool
        * grace_expires_at(admin_id) -> datetime | None
        * assert_writable(admin_id) -> None      (raises on grace)
        * enforce_at_grace_expiry() -> list[EnforcementResult]

  C3  GRACE_WINDOW_DAYS is sourced from closure_service so the two
      30-day clocks (closure + downgrade) share a single source of
      truth. Founder lock L1 -- 30 days, not 90.

  C4  The 5 downgrade-archive axes (Customer Journey Phase 8 Pro) are
      declared as module constants on downgrade_archive_service and
      AXIS_KNOWLEDGE is in the set. The original arc added the 5th
      axis; the test pins that decision.

  C5  AXIS_KNOWLEDGE operates on knowledge_embeddings grouped by
      source_id (sum-of-bytes), per the design note in
      downgrade_archive_service. There is no knowledge_sources table
      to group by, and Arc 11 owns that gap.

  C6  Subscription row carries the two grace clock columns the
      worker scans:
        * pending_downgrade_initiated_at  (clock start)
        * pending_downgrade_enforced_at   (idempotency marker)

  C7  Partial index supporting the day-30 worker scan exists.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_PATH = REPO_ROOT / "app" / "services" / "downgrade_grace_service.py"
ARCHIVE_PATH = REPO_ROOT / "app" / "services" / "downgrade_archive_service.py"
MIGRATION_PATH = (
    REPO_ROOT / "alembic" / "versions" / "arc10_lifecycle_subsystem.py"
)


def _parse(p: Path) -> ast.Module:
    return ast.parse(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------
# C1: service + error classes.
# ---------------------------------------------------------------------

def test_downgrade_grace_service_class_exists():
    classes = {n.name for n in ast.walk(_parse(SERVICE_PATH)) if isinstance(n, ast.ClassDef)}
    assert "DowngradeGraceService" in classes


def test_downgrade_grace_read_only_error_exists():
    classes = {n.name for n in ast.walk(_parse(SERVICE_PATH)) if isinstance(n, ast.ClassDef)}
    assert "DowngradeGraceReadOnlyError" in classes, (
        "Routes that need to fail-closed on write attempts during the "
        "grace window catch this error class. Removing it would force "
        "every route to re-implement the gate."
    )


def test_read_only_error_inherits_grace_error():
    cls = next(
        n for n in ast.walk(_parse(SERVICE_PATH))
        if isinstance(n, ast.ClassDef) and n.name == "DowngradeGraceReadOnlyError"
    )
    bases = [b.id for b in cls.bases if isinstance(b, ast.Name)]
    assert "DowngradeGraceError" in bases


# ---------------------------------------------------------------------
# C2: public method surface.
# ---------------------------------------------------------------------

def _service_methods() -> set[str]:
    cls = next(
        n for n in ast.walk(_parse(SERVICE_PATH))
        if isinstance(n, ast.ClassDef) and n.name == "DowngradeGraceService"
    )
    return {
        n.name for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


@pytest.mark.parametrize(
    "method",
    [
        "is_in_grace",
        "grace_expires_at",
        "assert_writable",
        "enforce_at_grace_expiry",
    ],
)
def test_grace_service_method_exists(method: str):
    assert method in _service_methods(), (
        f"DowngradeGraceService.{method} is part of the route/middleware "
        "contract; removing it breaks the read-only enforcement path."
    )


def test_assert_writable_raises_read_only_error_in_grace():
    """assert_writable is the gate. When in grace, it must raise
    DowngradeGraceReadOnlyError (not some other exception, not silently
    return) so callers get a typed signal they can map to HTTP 423/409."""
    src = SERVICE_PATH.read_text(encoding="utf-8")
    # The method body must reference the read-only error class.
    method = re.search(
        r"def assert_writable\(.*?\)(.*?)(?=\n    def |\n\nclass |\Z)",
        src, re.DOTALL,
    )
    assert method, "assert_writable not found"
    body = method.group(1)
    assert "DowngradeGraceReadOnlyError" in body, (
        "assert_writable must raise DowngradeGraceReadOnlyError when "
        "the admin is in grace. Found body does not reference it."
    )


# ---------------------------------------------------------------------
# C3: shared GRACE_WINDOW_DAYS source of truth.
# ---------------------------------------------------------------------

def test_grace_window_days_imported_from_closure_service():
    src = SERVICE_PATH.read_text(encoding="utf-8")
    assert "from app.lifecycle.closure import GRACE_WINDOW_DAYS" in src, (
        "DowngradeGraceService must import GRACE_WINDOW_DAYS from "
        "closure_service so the closure and downgrade clocks share a "
        "single source of truth. Founder lock L1: 30 days, not 90."
    )


def test_grace_window_days_used_for_expiry_computation():
    src = SERVICE_PATH.read_text(encoding="utf-8")
    flat = re.sub(r"\s+", " ", src)
    # The expiry expression must use GRACE_WINDOW_DAYS (not a literal 30).
    assert "GRACE_WINDOW_DAYS" in flat
    assert re.search(
        r"timedelta\(days=GRACE_WINDOW_DAYS\)", flat,
    ), (
        "grace expiry must be computed as initiated_at + timedelta("
        "days=GRACE_WINDOW_DAYS). Hardcoded 30 would drift from the "
        "single source of truth at closure_service.GRACE_WINDOW_DAYS."
    )


# ---------------------------------------------------------------------
# C4: 5 axes, AXIS_KNOWLEDGE present.
# ---------------------------------------------------------------------

def test_all_five_downgrade_axes_declared():
    src = ARCHIVE_PATH.read_text(encoding="utf-8")
    axes = ["AXIS_INSTANCES", "AXIS_EMBED_KEYS", "AXIS_CNAMES", "AXIS_SEATS", "AXIS_KNOWLEDGE"]
    for axis in axes:
        assert axis in src, (
            f"DowngradeArchiveService must declare {axis} as a module "
            "constant. Customer Journey Phase 8 Pro enumerates the 5 "
            "axes; missing one is a doctrine drift."
        )


def test_all_axes_tuple_contains_knowledge():
    """The AXIS_KNOWLEDGE constant is only meaningful if it's in the
    'all axes' tuple the enforcement worker iterates over. The
    decision to add knowledge as the 5th axis was founder lock L2.
    Unit 1 excision: AXIS_SEATS removed (single-owner model); tuple is
    now AXIS_INSTANCES, AXIS_EMBED_KEYS, AXIS_CNAMES, AXIS_KNOWLEDGE."""
    src = ARCHIVE_PATH.read_text(encoding="utf-8")
    flat = re.sub(r"\s+", " ", src)
    # The tuple should include all four axis names in declaration order.
    # AXIS_SEATS was removed in Unit 1 (no multi-seat table).
    pat = re.search(
        r"\(\s*AXIS_INSTANCES\s*,\s*AXIS_EMBED_KEYS\s*,\s*AXIS_CNAMES\s*,\s*AXIS_KNOWLEDGE\s*[,)]",
        flat,
    )
    assert pat, (
        "Expected a tuple containing AXIS_INSTANCES, AXIS_EMBED_KEYS, "
        "AXIS_CNAMES, AXIS_KNOWLEDGE in that order (AXIS_SEATS removed "
        "in Unit 1 excision). The enforcement worker iterates over this "
        "tuple to archive overflow."
    )


# ---------------------------------------------------------------------
# C5: AXIS_KNOWLEDGE operates on knowledge_embeddings grouped by source_id.
# ---------------------------------------------------------------------

def test_axis_knowledge_groups_by_source_id():
    """Founder lock L14: no knowledge_sources table; the lifecycle
    columns live on knowledge_embeddings. AXIS_KNOWLEDGE aggregates
    via GROUP BY source_id, sum(bytes). The supporting partial
    index is ix_knowledge_embeddings_lru_source."""
    src = ARCHIVE_PATH.read_text(encoding="utf-8")
    assert "source_id" in src, (
        "AXIS_KNOWLEDGE must reference source_id since aggregation is "
        "per source. Arc 11 owns the originals; Arc 10 groups chunks."
    )
    # And the migration must declare the supporting index.
    mig = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "ix_knowledge_embeddings_lru_source" in mig, (
        "Migration must declare ix_knowledge_embeddings_lru_source -- "
        "the partial index that backs the LRU sort by per-source oldest "
        "updated_at."
    )


def test_axis_knowledge_archives_via_pending_downgrade_archived_at():
    """The archive operation sets pending_downgrade_archived_at on the
    chunk rows -- it does NOT delete them. Customer Journey Phase 8
    Pro: 'archived (not deleted) until he upgrades again'."""
    src = ARCHIVE_PATH.read_text(encoding="utf-8")
    assert "pending_downgrade_archived_at" in src, (
        "AXIS_KNOWLEDGE archive path must stamp "
        "pending_downgrade_archived_at on knowledge_embeddings rows. "
        "Hard-deleting chunks at downgrade would break the 'recoverable "
        "on re-upgrade' contract."
    )


# ---------------------------------------------------------------------
# C6: subscription row has the two grace clock columns.
# ---------------------------------------------------------------------

def test_subscriptions_has_pending_downgrade_initiated_at():
    src = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "pending_downgrade_initiated_at" in src


def test_subscriptions_has_pending_downgrade_enforced_at():
    src = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "pending_downgrade_enforced_at" in src, (
        "pending_downgrade_enforced_at is the idempotency marker the "
        "day-30 worker stamps after archive completion. Without it, "
        "re-runs would re-archive the same overflow on every beat."
    )


# ---------------------------------------------------------------------
# C7: partial index for the day-30 worker scan.
# ---------------------------------------------------------------------

def test_downgrade_grace_eligible_partial_index_exists():
    src = MIGRATION_PATH.read_text(encoding="utf-8")
    pat = re.search(
        r"CREATE INDEX ix_subscriptions_downgrade_grace_eligible"
        r".*?WHERE pending_downgrade_target IS NOT NULL"
        r".*?AND pending_downgrade_enforced_at IS NULL",
        src, re.DOTALL,
    )
    assert pat, (
        "Migration must declare ix_subscriptions_downgrade_grace_eligible "
        "as a partial index keying off pending_downgrade_target IS NOT "
        "NULL AND pending_downgrade_enforced_at IS NULL. This is what "
        "the day-30 enforcement worker's scan predicate hits."
    )
