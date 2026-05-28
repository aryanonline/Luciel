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


# ---------------------------------------------------------------------
# Arc 10 Gap 6 close part 3 (D-arc10-audit-archiver-sequence-grant-
# missing-2026-05-27).
#
# After parts 1+2, the re-run E2E surfaced one more missing privilege:
# PostgreSQL requires USAGE on admin_audit_logs_id_seq to call
# nextval() during INSERT. Without it the transaction rolls back
# AFTER the S3 object is written -- same partial-state class as
# parts 1+2.
# ---------------------------------------------------------------------

GAP6_SEQ_MIGRATION_PATH = (
    REPO_ROOT
    / "alembic"
    / "versions"
    / "arc10_gap6_archiver_sequence_grant.py"
)


def test_gap6_sequence_grant_migration_exists():
    """The sequence-grant follow-up migration file must exist."""
    assert GAP6_SEQ_MIGRATION_PATH.exists(), (
        f"Missing migration file at {GAP6_SEQ_MIGRATION_PATH}. "
        "Without it the archiver INSERT fails on sequence USAGE."
    )


def test_gap6_sequence_migration_chains_off_insert_grant():
    """The sequence grant must chain off the INSERT grant migration."""
    src = GAP6_SEQ_MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'down_revision = "arc10_gap6_archiver_insert_grant"' in src, (
        "Sequence-grant migration must chain off the INSERT-grant "
        "migration so the migration history reflects the order in "
        "which the bugs surfaced and were fixed in production."
    )


def test_gap6_sequence_migration_grants_usage_on_sequence():
    """The migration body must GRANT USAGE on the sequence to the role."""
    src = GAP6_SEQ_MIGRATION_PATH.read_text(encoding="utf-8")
    assert "GRANT USAGE ON SEQUENCE" in src, (
        "Migration must contain GRANT USAGE ON SEQUENCE -- USAGE is "
        "the minimum sequence privilege for nextval() in INSERT."
    )
    assert "admin_audit_logs_id_seq" in src, (
        "GRANT must target admin_audit_logs_id_seq specifically."
    )
    assert "luciel_audit_archiver" in src, (
        "GRANT must be issued TO luciel_audit_archiver."
    )


# ---------------------------------------------------------------------
# Arc 10 Gap 6 close part 4 (D-arc10-audit-batch-spans-multiple-
# instances-2026-05-27).
#
# After parts 1+2+3 (ALLOWED_ACTIONS + INSERT grant + sequence grant),
# the re-run E2E surfaced the final-layer drift:
#
#   psycopg.errors.NotNullViolation: null value in column
#   "luciel_instance_id" of relation "admin_audit_logs"
#
# Arc 9.1 Phase A tenant-isolation seal made admin_audit_logs.
# luciel_instance_id NOT NULL on every instance-scoped table. The Arc
# 10 audit-retention worker grouped batches by admin_id only and
# passed luciel_instance_id=None to the batch-audit emission, which
# made it impossible for the INSERT to satisfy the NOT NULL constraint.
#
# Fix: sub-group batches by (admin_id, luciel_instance_id) so each
# emitted batch-audit row carries a real instance_id. An admin with
# N instances produces N batch-audit rows per worker run instead of
# 1; trades a small row-count increase for doctrine compliance and
# more precise forensic scoping.
# ---------------------------------------------------------------------

def test_audit_retention_select_includes_luciel_instance_id():
    """The _fetch_batch SELECT must include luciel_instance_id.

    Without it the worker cannot sub-group by (admin, instance) and
    cannot pass a non-NULL instance_id to the batch-audit emission.
    """
    src = AUDIT_SERVICE_PATH.read_text(encoding="utf-8")
    # The SELECT statement must list luciel_instance_id alongside
    # admin_id. Pinning as a substring rather than parsing SQL to
    # keep the test resilient to whitespace + comment changes.
    assert (
        "id, admin_id, luciel_instance_id" in src
        or "id, luciel_instance_id, admin_id" in src
        or "admin_id, luciel_instance_id" in src
    ), (
        "_fetch_batch SELECT must include luciel_instance_id so "
        "the worker can carry instance scope into the batch-audit "
        "emission. Arc 9.1 Phase A made the column NOT NULL."
    )


def test_audit_retention_groups_batches_by_admin_and_instance():
    """The archive loop must sub-group batches by (admin_id, instance_id).

    Pinned by checking for the dict type annotation tuple[str, int],
    which is the in-loop key. If a future refactor flattens the
    grouping back to admin-only, the type annotation will change and
    this test fails -- catching the regression before the NOT NULL
    constraint catches it in prod.
    """
    src = AUDIT_SERVICE_PATH.read_text(encoding="utf-8")
    assert "tuple[str, int]" in src, (
        "Archive loop must group by (admin_id, instance_id) tuple. "
        "Single-axis grouping by admin_id alone caused the Arc 9.1 "
        "NOT NULL violation on luciel_instance_id."
    )


def test_emit_batch_audit_passes_luciel_instance_id():
    """The _emit_batch_audit call must pass luciel_instance_id.

    Pinned as a kwarg name to prevent a future refactor from
    silently dropping it (kwarg keeps it self-documenting at the
    call site).
    """
    src = AUDIT_SERVICE_PATH.read_text(encoding="utf-8")
    # The call site inside _archive_one_tier
    assert "luciel_instance_id=instance_id" in src, (
        "_emit_batch_audit must be invoked with luciel_instance_id "
        "matching the sub-grouping key."
    )
    # The function signature itself
    assert "def _emit_batch_audit(" in src, (
        "_emit_batch_audit method must still exist."
    )
    # And the record() invocation inside _emit_batch_audit must
    # forward luciel_instance_id
    emit_idx = src.find("def _emit_batch_audit(")
    assert emit_idx >= 0
    emit_body = src[emit_idx:emit_idx + 3000]
    assert "luciel_instance_id=luciel_instance_id" in emit_body, (
        "_emit_batch_audit body must forward luciel_instance_id "
        "to AuditRepository.record()."
    )


def test_s3_key_encodes_instance_id():
    """The S3 key shape must include the instance id so a forensic
    walk can scope by (admin, instance) without listing all admin
    objects. The dir boundary stays at admin_id so an admin's full
    archive is still locatable by a single prefix scan.
    """
    src = AUDIT_SERVICE_PATH.read_text(encoding="utf-8")
    assert "inst-{instance_id}" in src, (
        "S3 key shape must encode instance_id in the filename "
        "(format: inst-{instance_id}-{first_id}-{last_id}.jsonl)."
    )


def test_cold_archive_hash_unchanged_by_instance_fix():
    """_cold_archive_hash must NOT include luciel_instance_id.

    The cold-archive hash function is part of the verification
    contract for previously-archived cold rows. Adding a new field
    to its canonical content would break verification of every cold
    row written before the change. luciel_instance_id surfaces in
    the JSONL output for forensic readers (separate concern) but
    must stay OUT of the hash input.
    """
    src = AUDIT_SERVICE_PATH.read_text(encoding="utf-8")
    # Find the _cold_archive_hash function body and pin its payload
    # keys list.
    hash_idx = src.find("def _cold_archive_hash(")
    assert hash_idx >= 0, "_cold_archive_hash must still exist"
    # Take just the payload dict literal (next ~1500 chars covers it).
    hash_body = src[hash_idx:hash_idx + 1500]
    assert '"luciel_instance_id"' not in hash_body, (
        "DOCTRINE VIOLATION: _cold_archive_hash must NOT include "
        "luciel_instance_id in its canonical payload. Doing so "
        "would break verification of every cold row written before "
        "this change. The field belongs in the JSONL output, not "
        "in the hash input."
    )


# ---------------------------------------------------------------------
# Arc 10 Gap 7 close: admin_audit_logs.luciel_instance_id nullability.
#
# Anchored to Architecture v1 \u00a73.7.3 (Wall 3 applies to customer-data
# tables, NOT to the admin audit log) and \u00a75.3 (admin audit log is a
# distinct concept -- append-only chain with its own DB role). The
# Arc 9.1 Phase A tenant-isolation seal swept admin_audit_logs into
# the customer-data NOT NULL bucket too broadly; Arc 10 Gap 7 walks
# that back. Architecture \u00a73.6.2 requires admin-scoped audit
# emissions (cascade, team-member ops, embed-key revoke) which
# cannot pick a single instance_id.
# ---------------------------------------------------------------------

GAP7_LOOSEN_MIGRATION_PATH = (
    REPO_ROOT
    / "alembic"
    / "versions"
    / "arc10_gap7_audit_loosen_instance_for_admin_scope.py"
)


def test_gap7_loosen_migration_exists():
    assert GAP7_LOOSEN_MIGRATION_PATH.exists(), (
        f"Missing migration at {GAP7_LOOSEN_MIGRATION_PATH}. "
        "Without the loosening, ClosureService.initiate_closure "
        "fails on the very first cascade audit emission because the "
        "row's luciel_instance_id is NULL by design (admin-scoped op "
        "spanning all instances per Architecture v1 \u00a73.6.2)."
    )


def test_gap7_loosen_migration_chains_off_gap6_sequence_grant():
    src = GAP7_LOOSEN_MIGRATION_PATH.read_text(encoding="utf-8")
    assert (
        'down_revision = "arc10_gap6_archiver_sequence_grant"' in src
    ), (
        "Gap 7 loosen migration must chain off the last Gap 6 "
        "migration so the audit-archiver fixes remain in the linear "
        "history above this change."
    )


def test_gap7_loosen_migration_makes_column_nullable():
    src = GAP7_LOOSEN_MIGRATION_PATH.read_text(encoding="utf-8")
    assert "alter_column" in src and "nullable=True" in src, (
        "Migration must ALTER COLUMN luciel_instance_id to nullable=True."
    )
    assert '"admin_audit_logs"' in src or "'admin_audit_logs'" in src, (
        "Migration must target the admin_audit_logs table specifically."
    )


def test_gap7_loosen_migration_restores_null_disjunct_in_policy():
    """The RLS policy must accept NULL luciel_instance_id rows (admin-
    scoped audit emissions) AND still enforce equality for non-NULL
    rows (instance-scoped audit emissions).
    """
    src = GAP7_LOOSEN_MIGRATION_PATH.read_text(encoding="utf-8")
    assert "luciel_instance_id IS NULL" in src, (
        "RLS policy must accept NULL luciel_instance_id rows -- "
        "admin-scoped audit emissions have no instance binding."
    )
    assert "current_setting('app.instance_id', true)" in src, (
        "RLS policy must still enforce equality for non-NULL rows."
    )


def test_audit_log_model_reflects_nullable_instance_id():
    """The SQLAlchemy model must declare the column as nullable. A
    drift between the model and the schema would silently re-introduce
    the bug at the ORM layer even though the DB accepts NULL.
    """
    src = (
        REPO_ROOT / "app" / "models" / "admin_audit_log.py"
    ).read_text(encoding="utf-8")
    # Walk to the luciel_instance_id column declaration.
    idx = src.find("luciel_instance_id: Mapped")
    assert idx >= 0, "luciel_instance_id column declaration not found"
    # Take the next ~400 chars covering the mapped_column(...) call.
    decl = src[idx:idx + 600]
    assert "Mapped[int | None]" in decl or "Mapped[Optional[int]]" in decl, (
        "luciel_instance_id must be typed as Mapped[int | None]."
    )
    assert "nullable=True" in decl, (
        "luciel_instance_id mapped_column must declare nullable=True."
    )


def test_account_closure_initiated_is_in_allowed_actions():
    """ACTION_ACCOUNT_CLOSURE_INITIATED must be wired into
    ALLOWED_ACTIONS. ClosureService.initiate_closure emits this row
    at the end of the close flow; without the membership wiring,
    AdminAuditRepository.record() rejects it with ValueError and the
    entire close flow crashes. Anchored to Architecture v1 \u00a73.6.2
    step 6 (Record closure-initiation timestamp).
    """
    from app.models import admin_audit_log as m
    assert hasattr(m, "ACTION_ACCOUNT_CLOSURE_INITIATED")
    assert m.ACTION_ACCOUNT_CLOSURE_INITIATED in m.ALLOWED_ACTIONS, (
        "ACTION_ACCOUNT_CLOSURE_INITIATED must be in ALLOWED_ACTIONS. "
        "Without this, /account/close crashes ValueError on the final "
        "audit emission \u2014 see ClosureService.initiate_closure."
    )


def test_account_reactivated_is_in_allowed_actions():
    """ACTION_ACCOUNT_REACTIVATED must be wired into ALLOWED_ACTIONS.
    ReactivationService.complete_reactivation emits this row when the
    admin returns within the 30-day grace window. Anchored to
    Architecture v1 \u00a73.6.2 (30-day grace reactivation).
    """
    from app.models import admin_audit_log as m
    assert hasattr(m, "ACTION_ACCOUNT_REACTIVATED")
    assert m.ACTION_ACCOUNT_REACTIVATED in m.ALLOWED_ACTIONS, (
        "ACTION_ACCOUNT_REACTIVATED must be in ALLOWED_ACTIONS. "
        "Without this, /account/reactivate/complete crashes ValueError "
        "\u2014 see ReactivationService.complete_reactivation."
    )
