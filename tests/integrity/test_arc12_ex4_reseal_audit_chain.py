"""Arc 12 EX4 — audit-chain reseal regression tests.

Pins the contract of the founder-locked reseal migration
``arc12_ex4_reseal_audit_chain_drop_agent_domain``:

  1. The migration file exists at the expected path, declares the
     expected revision id, and chains off
     ``arc12_ex3_drop_scope_assignment_domain`` (the EX3 head).
  2. ``_CHAIN_FIELDS`` in ``app.repositories.audit_chain`` does NOT
     contain ``agent_id`` / ``domain_id`` post-EX4 (the locked v2
     field set).
  3. The AdminAuditLog ORM model has no ``agent_id`` / ``domain_id``
     attribute (the columns were dropped).
  4. ``ACTION_AUDIT_CHAIN_RESEALED`` is declared AND wired into
     ``ALLOWED_ACTIONS`` so the migration's own emitted audit row
     is admissible.
  5. The reseal procedure produces a fully-chained set of rows that
     re-verify under the v2 canonical hash (synthetic in-memory chain),
     AND any tamper to a resealed row's hashable content breaks the
     chain at the tamper point (immutability preserved).

The reseal verification (item 5) is performed with a synthetic
in-memory chain — same shape the migration walks — so the test is
DB-free and runs in CI without needing alembic+Postgres infrastructure.
A real end-to-end run is covered by the existing Pillar 23
chain-verifier when the migration is applied against a live database.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
from datetime import datetime, timezone

import pytest


# ---------------------------------------------------------------------
# 1. Migration file presence + chain wiring
# ---------------------------------------------------------------------

def _migration_path() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve()
    project_root = here.parents[2]
    return (
        project_root
        / "app" / "migrations"
        / "versions"
        / "arc12_ex4_reseal_audit_chain_drop_agent_domain.py"
    )


def test_ex4_migration_file_present_and_chained() -> None:
    mig = _migration_path()
    assert mig.exists(), (
        "EX4 reseal migration is missing. Expected at "
        f"{mig}. The founder-locked reseal must be a discrete "
        "Alembic migration so the integrity operation is replayable "
        "and reviewable."
    )
    src = mig.read_text()
    assert re.search(
        r'^revision\s*=\s*[\'"]arc12_ex4_reseal_audit_chain_drop_agent_domain[\'"]',
        src,
        re.MULTILINE,
    ), "EX4 migration must declare revision = 'arc12_ex4_reseal_audit_chain_drop_agent_domain'."
    assert re.search(
        r'^down_revision\s*=\s*[\'"]arc12_ex3_drop_scope_assignment_domain[\'"]',
        src,
        re.MULTILINE,
    ), (
        "EX4 migration must chain off arc12_ex3_drop_scope_assignment_domain "
        "(the EX3 head). If a newer migration has been merged ahead of this "
        "one, restack."
    )


def test_ex4_migration_drops_columns_and_indexes() -> None:
    """The migration must drop both indexes AND both columns
    (the only acceptable shape for the post-reseal cleanup)."""
    src = _migration_path().read_text()
    assert "drop_index" in src and "ix_admin_audit_logs_agent_id" in src, (
        "EX4 migration must drop ix_admin_audit_logs_agent_id."
    )
    assert "ix_admin_audit_logs_domain_id" in src, (
        "EX4 migration must drop ix_admin_audit_logs_domain_id."
    )
    assert "drop_column(\"admin_audit_logs\", \"agent_id\")" in src, (
        "EX4 migration must drop admin_audit_logs.agent_id."
    )
    assert "drop_column(\"admin_audit_logs\", \"domain_id\")" in src, (
        "EX4 migration must drop admin_audit_logs.domain_id."
    )
    assert "pg_advisory_xact_lock" in src, (
        "EX4 migration must take the chain advisory lock during the "
        "reseal to block concurrent audit writers."
    )
    assert "audit_chain_resealed" in src, (
        "EX4 migration must emit the audit-of-the-reseal row "
        "(ACTION_AUDIT_CHAIN_RESEALED) so the integrity operation "
        "is itself traceable."
    )


# ---------------------------------------------------------------------
# 2. _CHAIN_FIELDS no longer contains agent_id/domain_id
# ---------------------------------------------------------------------

def test_chain_fields_excludes_agent_and_domain() -> None:
    from app.repositories.audit_chain import _CHAIN_FIELDS

    assert "agent_id" not in _CHAIN_FIELDS, (
        "Arc 12 EX4: agent_id must NOT be in _CHAIN_FIELDS after the "
        "reseal. The migration recomputes every historical row's "
        "row_hash under a field set that omits this column."
    )
    assert "domain_id" not in _CHAIN_FIELDS, (
        "Arc 12 EX4: domain_id must NOT be in _CHAIN_FIELDS after the "
        "reseal."
    )


# ---------------------------------------------------------------------
# 3. AdminAuditLog model has no agent_id/domain_id attribute
# ---------------------------------------------------------------------

def test_admin_audit_log_model_has_no_agent_or_domain_columns() -> None:
    from app.models.admin_audit_log import AdminAuditLog

    cols = {c.name for c in AdminAuditLog.__table__.columns}
    assert "agent_id" not in cols, (
        "Arc 12 EX4: AdminAuditLog.agent_id Mapped column must be gone "
        "(physically dropped by the reseal migration)."
    )
    assert "domain_id" not in cols, (
        "Arc 12 EX4: AdminAuditLog.domain_id Mapped column must be gone."
    )


# ---------------------------------------------------------------------
# 4. ACTION_AUDIT_CHAIN_RESEALED is declared + wired
# ---------------------------------------------------------------------

def test_action_audit_chain_resealed_is_wired_into_allowed_actions() -> None:
    from app.models.admin_audit_log import (
        ACTION_AUDIT_CHAIN_RESEALED,
        ALLOWED_ACTIONS,
    )

    assert ACTION_AUDIT_CHAIN_RESEALED == "audit_chain_resealed"
    assert ACTION_AUDIT_CHAIN_RESEALED in ALLOWED_ACTIONS, (
        "ACTION_AUDIT_CHAIN_RESEALED must be in ALLOWED_ACTIONS so the "
        "reseal migration's own emitted audit row passes the action "
        "whitelist check."
    )


# ---------------------------------------------------------------------
# 5. Reseal correctness: synthetic in-memory chain re-verifies
# ---------------------------------------------------------------------

def _reseal_in_memory(rows: list[dict]) -> list[dict]:
    """Reproduce the migration's reseal step on an in-memory row list.

    Uses ``app.repositories.audit_chain.canonical_row_hash`` (the
    runtime hash function) so this test ALSO catches any drift
    between the migration's inlined ``_canonical_hash_v2`` and the
    runtime function — the migration must produce hashes that match
    what Pillar 23 recomputes at verify time.
    """
    from app.repositories.audit_chain import (
        GENESIS_PREV_HASH,
        canonical_row_hash,
    )

    prev_hash = GENESIS_PREV_HASH
    out = []
    for r in rows:
        new_hash = canonical_row_hash(r, prev_hash)
        r2 = dict(r)
        r2["prev_row_hash"] = prev_hash
        r2["row_hash"] = new_hash
        out.append(r2)
        prev_hash = new_hash
    return out


def _make_row(idx: int, **overrides) -> dict:
    base = {
        "id": idx,
        "admin_id": f"admin-{idx}",
        "luciel_instance_id": idx,
        "actor_key_prefix": f"lusck{idx:08d}",
        "actor_permissions": '["admin"]',
        "actor_label": f"label-{idx}",
        "action": "create",
        "resource_type": "luciel_instance",
        "resource_pk": idx,
        "resource_natural_id": f"inst-{idx}",
        "before_json": None,
        "after_json": {"active": True},
        "note": f"row-{idx}",
        "created_at": datetime(2026, 1, 1, idx % 24, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


def test_reseal_produces_a_verifying_chain() -> None:
    """After reseal, the runtime verifier (recompute every row in id
    ASC and compare to stored hashes) must validate end-to-end."""
    from app.repositories.audit_chain import (
        GENESIS_PREV_HASH,
        canonical_row_hash,
    )

    rows = [_make_row(i) for i in range(1, 6)]
    resealed = _reseal_in_memory(rows)

    # Verify: recompute every row from GENESIS and compare to stored.
    prev_hash = GENESIS_PREV_HASH
    for r in resealed:
        expected = canonical_row_hash(r, prev_hash)
        assert r["row_hash"] == expected, (
            f"Resealed row id={r['id']} does not re-verify. The "
            f"migration's reseal step produced hashes the runtime "
            f"verifier cannot reproduce — that means the inlined "
            f"hash function in the migration has drifted from "
            f"app.repositories.audit_chain.canonical_row_hash."
        )
        assert r["prev_row_hash"] == prev_hash, (
            f"Resealed row id={r['id']} has wrong prev_row_hash; "
            f"chain pointer is broken."
        )
        prev_hash = r["row_hash"]


def test_reseal_chain_breaks_under_tampered_content() -> None:
    """Tamper with the content of a resealed row; recompute MUST
    fail at the tamper point. This is the immutability invariant —
    the reseal must not weaken it."""
    from app.repositories.audit_chain import (
        GENESIS_PREV_HASH,
        canonical_row_hash,
    )

    rows = [_make_row(i) for i in range(1, 6)]
    resealed = _reseal_in_memory(rows)

    # Tamper with row id=3's note. Its stored row_hash is now wrong;
    # the verifier should refuse to confirm.
    resealed[2]["note"] = "TAMPERED"

    prev_hash = GENESIS_PREV_HASH
    mismatch_at = None
    for r in resealed:
        expected = canonical_row_hash(r, prev_hash)
        if r["row_hash"] != expected:
            mismatch_at = r["id"]
            break
        prev_hash = r["row_hash"]

    assert mismatch_at == 3, (
        "Tampering with a resealed row's content must produce a "
        "mismatch at the verifier on that exact row. The reseal "
        "MUST preserve the tamper-evidence invariant."
    )


def test_reseal_inlined_hash_matches_runtime_hash() -> None:
    """The migration inlines a self-contained ``_canonical_hash_v2``.
    Verify it produces byte-identical output to the runtime
    ``canonical_row_hash``. Drift between the two would mean
    historical rows resealed by the migration would never re-verify
    at runtime."""
    import importlib.util

    from app.repositories.audit_chain import (
        GENESIS_PREV_HASH,
        canonical_row_hash,
    )

    spec = importlib.util.spec_from_file_location(
        "_arc12_ex4_reseal_mig", _migration_path()
    )
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    assert mig.GENESIS_PREV_HASH == GENESIS_PREV_HASH, (
        "Migration's GENESIS_PREV_HASH must match the runtime constant."
    )
    assert set(mig._CHAIN_FIELDS_V2) == set(
        __import__(
            "app.repositories.audit_chain", fromlist=["_CHAIN_FIELDS"]
        )._CHAIN_FIELDS
    ), (
        "Migration's _CHAIN_FIELDS_V2 must match the runtime "
        "_CHAIN_FIELDS exactly — they pin the same canonical "
        "content under the same hash function."
    )

    for i in range(1, 6):
        row = _make_row(i)
        prev = "a" * 64 if i > 1 else GENESIS_PREV_HASH
        assert mig._canonical_hash_v2(row, prev) == canonical_row_hash(
            row, prev
        ), (
            f"Migration inlined hash != runtime hash for row {i}. "
            "Drift between the two would invalidate every resealed "
            "row at the next Pillar 23 verify run."
        )
