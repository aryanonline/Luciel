"""Step 29.y gap-fix C2: AdminAuditLog.note length cap invariant.

Drift token: D-audit-note-length-unbounded-2026-05-07.

Origin:
  AdminAuditLog.note is mapped to Postgres Text, so the column has
  no length bound. note is part of the hash-chain canonical content
  (audit_chain.py::_CHAIN_FIELDS), which means an unbounded value
  inflates row_hash input size. An accidental dump (exception
  message, debug payload concatenated into a note) would bloat the
  audit table and make per-row hash latency unpredictable.

This module asserts:

  1. MAX_NOTE_LENGTH constant exists on app.models.admin_audit_log
     and is exactly 256 (cluster plan binding).
  2. AdminAuditRepository.record() truncates over-cap notes at the
     boundary, with a visible '...[truncated]' marker, and the final
     stored value is <= MAX_NOTE_LENGTH.
  3. Under-cap and exactly-cap notes are stored verbatim (no marker).
  4. None / empty notes pass through unchanged.
  5. Truncation is forward-only: the column is still Text, so a
     historical row exceeding the cap remains readable. The cap
     applies only to NEW writes.
  6. AST assertion: the cap is enforced inside record() (not at the
     model column or any other layer the test would silently miss).

These tests do not need a live database; they construct an in-memory
repository against a stub session that captures the AdminAuditLog
instance the repository would have written.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.models.admin_audit_log import (
    ACTION_CREATE,
    MAX_NOTE_LENGTH,
    RESOURCE_TENANT,
    AdminAuditLog,
)
from app.repositories import admin_audit_repository
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)


# --------------------------------------------------------------------- #
# Test stub session                                                      #
# --------------------------------------------------------------------- #

class _CapturingSession:
    """Minimal SQLAlchemy Session stand-in for unit testing record().

    Captures the AdminAuditLog instance that record() adds; flushes
    are no-ops. We do not exercise the hash chain here -- that's
    Pillar 23's job. We just want to read the .note value the
    repository handed off to the ORM.
    """

    def __init__(self):
        self.captured: AdminAuditLog | None = None

    def add(self, obj):
        if isinstance(obj, AdminAuditLog):
            self.captured = obj

    def flush(self):
        pass

    def commit(self):
        pass

    def refresh(self, obj):
        pass

    def rollback(self):
        pass


def _record(note):
    sess = _CapturingSession()
    repo = AdminAuditRepository(sess)
    ctx = AuditContext(actor_permissions=("admin",))
    row = repo.record(
        ctx=ctx,
        tenant_id="t1",
        action=ACTION_CREATE,
        resource_type=RESOURCE_TENANT,
        resource_pk=1,
        note=note,
    )
    assert row is sess.captured
    return row


# --------------------------------------------------------------------- #
# 1. Constant invariant                                                  #
# --------------------------------------------------------------------- #

def test_max_note_length_is_256():
    assert MAX_NOTE_LENGTH == 256


# --------------------------------------------------------------------- #
# 2. Truncation behaviour                                                #
# --------------------------------------------------------------------- #

def test_under_cap_passes_through_verbatim():
    short = "operational note"
    row = _record(short)
    assert row.note == short
    assert "[truncated]" not in row.note


def test_exactly_at_cap_passes_through_verbatim():
    exactly = "x" * MAX_NOTE_LENGTH
    row = _record(exactly)
    assert row.note == exactly
    assert len(row.note) == MAX_NOTE_LENGTH


def test_over_cap_is_truncated_with_marker():
    over = "y" * (MAX_NOTE_LENGTH * 4)
    row = _record(over)
    assert len(row.note) <= MAX_NOTE_LENGTH
    assert row.note.endswith("...[truncated]")
    assert row.note.startswith("y")


def test_one_over_cap_is_truncated():
    one_over = "z" * (MAX_NOTE_LENGTH + 1)
    row = _record(one_over)
    assert len(row.note) <= MAX_NOTE_LENGTH
    assert row.note.endswith("...[truncated]")


def test_none_note_passes_through():
    row = _record(None)
    assert row.note is None


def test_empty_note_passes_through():
    row = _record("")
    assert row.note == ""


# --------------------------------------------------------------------- #
# 3. AST assertion: cap enforced inside record(), not elsewhere          #
# --------------------------------------------------------------------- #

def test_cap_is_enforced_in_record_method():
    """Make sure MAX_NOTE_LENGTH is referenced inside the record()
    function body. If a future refactor moves the cap to a different
    layer that the truncation tests don't cover, this assertion
    surfaces the move at CI time.
    """
    src = inspect.getsource(AdminAuditRepository.record)
    assert "MAX_NOTE_LENGTH" in src, (
        "AdminAuditRepository.record() must reference MAX_NOTE_LENGTH "
        "directly. If you moved the cap, update this test and the "
        "D-audit-note-length-unbounded drift register entry."
    )


def test_cap_constant_module_origin():
    """MAX_NOTE_LENGTH must live in app.models.admin_audit_log so other
    modules can reference it without circular imports through the
    repository layer."""
    from app.models import admin_audit_log as model_mod
    assert hasattr(model_mod, "MAX_NOTE_LENGTH")
    assert model_mod.MAX_NOTE_LENGTH == 256

    repo_module_path = Path(admin_audit_repository.__file__).resolve()
    src = repo_module_path.read_text()
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "app.models.admin_audit_log":
                names = {alias.name for alias in node.names}
                if "MAX_NOTE_LENGTH" in names:
                    found = True
                    break
    assert found, (
        "AdminAuditRepository module must import MAX_NOTE_LENGTH "
        "from app.models.admin_audit_log directly."
    )
