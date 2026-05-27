"""Arc 10 regression tests -- audit-tier retention doctrine.

Protects two founder locks and one doctrinal reconciliation:

  L4  Tier-conditional audit retention is in scope. Vision 6.5 / 7
      windows: 30d Free / 1y Pro / 7y Enterprise.

  L5  tier_at_write is STICKY across downgrades. A Pro -> Free
      downgrade does NOT retroactively shorten the retention of
      Pro-era audit rows.

  Doctrine reconciliation: Arc 9 C6.1 declared the audit log
  "forward-only forever, even the ops role cannot mutate audit
  rows." That stance contradicts Vision 6.5 ("audit log archived
  to cold storage"). Per Vision 10 doctrine-anchor: Vision wins.
  Arc 10 reconciles by giving the archival work its own narrowly-
  granted role (luciel_audit_archiver, SELECT + UPDATE on
  admin_audit_log only, no DELETE). luciel_ops still cannot
  mutate audit rows -- the C6.1 blast-radius discipline holds.

Test strategy: AST / text assertions against shipped source. We
pin the doctrine surface in code; live-DB integration tests live
in a follow-up coverage PR.
"""
from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_SERVICE_PATH = (
    REPO_ROOT / "app" / "services" / "audit_retention_service.py"
)
AUDIT_MODEL_PATH = REPO_ROOT / "app" / "models" / "admin_audit_log.py"
MIGRATION_PATH = (
    REPO_ROOT / "alembic" / "versions" / "arc10_lifecycle_subsystem.py"
)


# ---------------------------------------------------------------------
# L4: tier windows match Vision 7 verbatim.
# ---------------------------------------------------------------------

def test_tier_window_days_match_vision_section_7():
    """30d Free / 1y Pro / 7y Enterprise -- the canonical numbers."""
    src = AUDIT_SERVICE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    target = None
    for node in ast.walk(tree):
        # The constant has a type annotation so it's an AnnAssign,
        # not an Assign. Check both shapes.
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_TIER_WINDOW_DAYS"
        ):
            target = node
            break
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "_TIER_WINDOW_DAYS"
        ):
            target = node
            break
    assert target is not None, "_TIER_WINDOW_DAYS not found"
    assert isinstance(target.value, ast.Dict)

    # Build {tier_name: days} from the AST.
    actual: dict[str, int] = {}
    for k, v in zip(target.value.keys, target.value.values):
        assert isinstance(k, ast.Constant)
        # The value may be a BinOp (365 * 7) so we eval to int.
        actual[k.value] = _eval_int(v)

    expected = {
        "free":       30,
        "pro":        365,
        "enterprise": 365 * 7,
    }
    assert actual == expected, (
        f"Audit tier windows must match Vision 7 exactly. "
        f"Expected {expected}, found {actual}. Any change to these "
        f"values needs a documented founder approval in the same "
        f"commit."
    )


def _eval_int(node: ast.AST) -> int:
    """Evaluate a simple int-or-BinOp(int*int) AST node."""
    if isinstance(node, ast.Constant):
        return int(node.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        return _eval_int(node.left) * _eval_int(node.right)
    raise AssertionError(f"Cannot eval AST node: {ast.dump(node)}")


# ---------------------------------------------------------------------
# L5: tier_at_write column is sticky -- the model declares it and the
# migration backfills it.
# ---------------------------------------------------------------------

def test_admin_audit_log_model_declares_tier_at_write_column():
    """Model must declare tier_at_write column for ORM coverage."""
    src = AUDIT_MODEL_PATH.read_text(encoding="utf-8")
    assert "tier_at_write" in src, (
        "AdminAuditLog model must declare tier_at_write column."
    )
    # The docstring near the column must reference stickiness.
    assert "Sticky" in src or "sticky" in src, (
        "tier_at_write column docstring must explain that the value "
        "is sticky across downgrades (Arc 10 L5)."
    )


def test_admin_audit_log_model_declares_cold_archived_at_column():
    """Model must declare cold_archived_at column."""
    src = AUDIT_MODEL_PATH.read_text(encoding="utf-8")
    assert "cold_archived_at" in src, (
        "AdminAuditLog model must declare cold_archived_at column."
    )


def test_migration_backfills_tier_at_write_from_admins_current_tier():
    """Migration backfill keys tier_at_write off the admin's current tier."""
    src = MIGRATION_PATH.read_text(encoding="utf-8")
    # The backfill SQL must reference both admin_audit_log.tier_at_write
    # and admins.tier in an UPDATE-FROM shape.
    assert "UPDATE admin_audit_log" in src, (
        "Migration must backfill admin_audit_log."
    )
    assert "SET tier_at_write" in src, (
        "Migration backfill must set tier_at_write."
    )
    assert "FROM admins" in src, (
        "Migration backfill must JOIN admins."
    )


# ---------------------------------------------------------------------
# Doctrine reconciliation: chain stays append-only in hot+cold combined.
# ---------------------------------------------------------------------

def test_audit_archiver_role_has_no_delete_grant():
    """luciel_audit_archiver gets SELECT + UPDATE only, never DELETE.

    The Arc 9 C6.1 blast-radius discipline says no role mutates the
    audit log. We are softening that to "no role DELETEs the audit
    log; only the audit-tier archiver UPDATEs cold_archived_at."
    DELETE would break the append-only-in-combined invariant.
    """
    src = MIGRATION_PATH.read_text(encoding="utf-8")
    # The constants list naming the granted privileges must contain
    # SELECT and UPDATE and must NOT name DELETE for the archiver
    # role's table set.
    assert "GRANT SELECT, UPDATE ON" in src, (
        "Audit archiver must be granted SELECT + UPDATE."
    )
    # No GRANT ... DELETE ... TO luciel_audit_archiver should appear.
    # We check by ensuring no line grants DELETE specifically to the
    # archiver role.
    bad_pattern = "DELETE ON admin_audit_log TO luciel_audit_archiver"
    assert bad_pattern not in src, (
        "luciel_audit_archiver MUST NOT receive DELETE on "
        "admin_audit_log. Vision 6.5 archival is move-to-cold, "
        "not delete-from-hot."
    )


def test_audit_archiver_role_is_distinct_from_luciel_ops():
    """The archiver role is NEW, NOT a grant extension on luciel_ops.

    Arc 9 C6.1 declared luciel_ops forward-only on the audit log.
    Arc 10 honors that by creating a SEPARATE role for the archival
    work. Adding UPDATE to luciel_ops would directly violate C6.1's
    blast-radius rule.
    """
    src = MIGRATION_PATH.read_text(encoding="utf-8")
    # The migration must name luciel_audit_archiver as its archiver
    # role constant. luciel_ops must not appear with a new UPDATE
    # grant.
    assert "luciel_audit_archiver" in src, (
        "Migration must create the luciel_audit_archiver role."
    )
    # Make sure we did NOT accidentally add UPDATE on admin_audit_log
    # to luciel_ops.
    assert "UPDATE ON admin_audit_log TO luciel_ops" not in src, (
        "Arc 10 must NOT grant UPDATE on admin_audit_log to "
        "luciel_ops -- C6.1 forward-only discipline applies to ops."
    )


# ---------------------------------------------------------------------
# Service shape: per-tier loop + chain-extension at archive time.
# ---------------------------------------------------------------------

def test_audit_service_iterates_all_three_tiers():
    """The retention service must iterate over all three tier names."""
    src = AUDIT_SERVICE_PATH.read_text(encoding="utf-8")
    for tier in ("free", "pro", "enterprise"):
        assert f'"{tier}"' in src, (
            f"Audit retention service must reference tier '{tier}' "
            f"by name."
        )


def test_cold_archive_writer_extends_hash_chain():
    """The cold-archive writer must compute cold_hash via sha256.

    Vision 6.5 archival preserves the chain across the hot/cold
    boundary. The cold-archive writer computes
    sha256(canonical_content + row_hash) so a forensic walk can
    verify the cold row was once a legitimate hot-chain member.
    """
    src = AUDIT_SERVICE_PATH.read_text(encoding="utf-8")
    assert "_cold_archive_hash" in src, (
        "Service must define _cold_archive_hash for chain extension."
    )
    assert "hashlib.sha256" in src, (
        "Cold-archive hash must use sha256 to match the hot chain."
    )
    assert "row_hash" in src, (
        "Cold hash input must include the row's own row_hash so the "
        "cold record references a specific hot chain position."
    )


# ---------------------------------------------------------------------
# Arc 10 Gap 6 close (D-arc10-audit-archiver-action-not-in-allowed-actions-
# 2026-05-27).
#
# Bug: the audit retention service emits ACTION_AUDIT_LOG_TIER_ARCHIVED
# once per archived batch, but that constant -- though declared in
# app/models/admin_audit_log.py -- was never wired into ALLOWED_ACTIONS.
# AdminAuditRepository.record() validates action against ALLOWED_ACTIONS
# and raised ValueError, rolling back the archive transaction AFTER the
# S3 object had already been written. Net effect: partial state where
# cold_archived_at stayed NULL but the cold-storage S3 object existed.
#
# These tests pin (a) the constant exists, (b) it is wired into
# ALLOWED_ACTIONS, and (c) the retention service still references it,
# so the three pieces cannot drift apart again without a test failure.
# ---------------------------------------------------------------------

def test_audit_log_tier_archived_action_constant_exists():
    """The string constant must exist on the model module."""
    from app.models import admin_audit_log as m
    assert hasattr(m, "ACTION_AUDIT_LOG_TIER_ARCHIVED"), (
        "ACTION_AUDIT_LOG_TIER_ARCHIVED constant missing from "
        "app/models/admin_audit_log.py. The audit retention worker "
        "imports it; removing the symbol breaks the worker."
    )
    assert m.ACTION_AUDIT_LOG_TIER_ARCHIVED == "audit_log_tier_archived", (
        "Constant value must not drift -- existing audit rows on disk "
        "carry this exact string and forensic queries filter on it."
    )


def test_audit_log_tier_archived_action_is_in_allowed_actions():
    """The constant must be registered in ALLOWED_ACTIONS.

    AdminAuditRepository.record() rejects any action not present in
    ALLOWED_ACTIONS with ValueError, which rolls back the surrounding
    transaction. The retention worker calls record() AFTER writing the
    S3 cold-archive object, so a missing entry here causes a partial-
    state bug: S3 object exists, cold_archived_at stays NULL, and the
    same rows get re-archived on the next worker tick.
    """
    from app.models import admin_audit_log as m
    assert m.ACTION_AUDIT_LOG_TIER_ARCHIVED in m.ALLOWED_ACTIONS, (
        "ACTION_AUDIT_LOG_TIER_ARCHIVED must be in ALLOWED_ACTIONS. "
        "See D-arc10-audit-archiver-action-not-in-allowed-actions-"
        "2026-05-27 for the production partial-state bug this prevents."
    )


def test_audit_retention_service_uses_canonical_action_constant():
    """The service must import + use the constant, not a string literal.

    A string literal would silently desync from the constant if either
    side were renamed. The import + reference forces a NameError on
    any drift.
    """
    src = AUDIT_SERVICE_PATH.read_text(encoding="utf-8")
    assert "ACTION_AUDIT_LOG_TIER_ARCHIVED" in src, (
        "audit_retention_service.py must reference the canonical "
        "ACTION_AUDIT_LOG_TIER_ARCHIVED constant by name."
    )
    assert "action=ACTION_AUDIT_LOG_TIER_ARCHIVED" in src, (
        "The batch-audit emission must pass the constant as the "
        "action= kwarg to AuditRepository.record(); a string literal "
        "would bypass the ALLOWED_ACTIONS membership guarantee."
    )


# ---------------------------------------------------------------------
# Arc 10 Gap 6 close part 2 (D-arc10-audit-archiver-cannot-insert-
# batch-audit-row-2026-05-27).
#
# The original arc10_lifecycle_subsystem migration granted SELECT +
# UPDATE only to luciel_audit_archiver. The audit retention service
# also INSERTs a per-batch audit row (action='audit_log_tier_archived')
# in the same transaction that stamps cold_archived_at. Without INSERT
# the worker hit psycopg.errors.InsufficientPrivilege and rolled back
# the entire archive transaction AFTER the S3 object was written.
#
# Fix: arc10_gap6_archiver_insert_grant.py grants INSERT on
# admin_audit_logs to luciel_audit_archiver. These tests pin the
# follow-up migration's existence + content so the grant cannot get
# silently dropped or downgraded.
# ---------------------------------------------------------------------

GAP6_MIGRATION_PATH = (
    REPO_ROOT
    / "alembic"
    / "versions"
    / "arc10_gap6_archiver_insert_grant.py"
)


def test_gap6_archiver_insert_grant_migration_exists():
    """The follow-up migration file must exist on disk."""
    assert GAP6_MIGRATION_PATH.exists(), (
        f"Missing migration file at {GAP6_MIGRATION_PATH}. "
        "Without this grant the audit_retention worker rolls back "
        "every archive transaction with InsufficientPrivilege."
    )


def test_gap6_migration_chains_off_arc10_lifecycle():
    """The new migration must point at arc10_lifecycle_subsystem as
    down_revision -- it depends on the role existing.
    """
    src = GAP6_MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'down_revision = "arc10_lifecycle_subsystem"' in src, (
        "Gap 6 grant migration must chain off arc10_lifecycle_subsystem "
        "since it depends on the luciel_audit_archiver role created there."
    )


def test_gap6_migration_grants_insert_on_admin_audit_logs():
    """The upgrade body must GRANT INSERT to the archiver role.

    Pinned as a string so a refactor that accidentally drops the
    grant statement fails this test instead of silently re-introducing
    the production bug.
    """
    src = GAP6_MIGRATION_PATH.read_text(encoding="utf-8")
    assert "GRANT INSERT ON" in src, (
        "Migration must contain a GRANT INSERT statement."
    )
    assert "admin_audit_logs" in src, (
        "GRANT must target admin_audit_logs specifically."
    )
    assert "luciel_audit_archiver" in src, (
        "GRANT must be issued TO luciel_audit_archiver, not any other role."
    )


def test_gap6_migration_does_not_widen_to_delete():
    """Doctrine guardrail: the move-to-cold path must never DELETE
    audit rows. Arc 9 C6.1 "forward-only forever, no DELETE" is
    preserved through Arc 10 + Gap 6. INSERT is forward-only;
    DELETE is destruction. If a future patch ever appends a DELETE
    grant here, this test must fail.
    """
    src = GAP6_MIGRATION_PATH.read_text(encoding="utf-8")
    assert "GRANT DELETE" not in src, (
        "DOCTRINE VIOLATION: the move-to-cold path must remain "
        "DELETE-free (Arc 9 C6.1 + Arc 10 reconciliation). The hash "
        "chain stays append-only across hot+cold combined."
    )


def test_gap6_migration_downgrade_only_revokes_insert():
    """Downgrade must NOT revoke SELECT or UPDATE -- those were
    granted by arc10_lifecycle_subsystem and a downgrade of THIS
    migration should leave the prior arc10 state intact, not
    half-revert it.
    """
    src = GAP6_MIGRATION_PATH.read_text(encoding="utf-8")
    # Find the downgrade body.
    down_idx = src.find("def downgrade(")
    assert down_idx >= 0, "downgrade() must exist for migration symmetry"
    down_body = src[down_idx:]
    assert "REVOKE INSERT" in down_body, (
        "downgrade must REVOKE INSERT to mirror the upgrade."
    )
    assert "REVOKE SELECT" not in down_body, (
        "downgrade must NOT revoke SELECT -- that grant belongs to "
        "arc10_lifecycle_subsystem and surviving its own downgrade."
    )
    assert "REVOKE UPDATE" not in down_body, (
        "downgrade must NOT revoke UPDATE -- same reasoning as SELECT."
    )
