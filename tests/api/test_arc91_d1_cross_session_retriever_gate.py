"""Arc 9.1 Phase D1 — CrossSessionRetriever quarantine gate (G1).

This module proves the runtime gate that closes G1:
    1. With the feature flag unset, retrieve() refuses with RuntimeError.
    2. With the flag set but no admin_id GUC bound, retrieve() refuses.
    3. With both flag and GUC set, retrieve() reaches the SQL path.

We do NOT prove the SQL itself here — the original shape suite
(tests/api/test_step24_5c_cross_session_retriever_shape.py) already
asserts the query shape. Here we only prove the gate FIRES correctly.
"""

from __future__ import annotations

import os
import unittest
import uuid
from unittest import mock


CONV_ID = uuid.uuid4()


class _GucStubDb:
    """Stub DB that returns a configurable admin_id GUC value."""

    def __init__(self, admin_guc):
        self._admin_guc = admin_guc
        self.executed = []

    def execute(self, stmt):
        # Capture and return either GUC probe result or empty .all()
        sql = str(stmt)
        if "app.admin_id" in sql:
            return _Result(scalar=self._admin_guc)
        # Beyond the GUC probe — record we got here, return empty all().
        self.executed.append(sql)
        return _Result(scalar=None, rows=[])


class _Result:
    def __init__(self, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar(self):
        return self._scalar

    def all(self):
        return self._rows


class TestQuarantineGate(unittest.TestCase):
    """When the feature flag is unset, the gate must refuse."""

    def test_refuses_without_feature_flag(self):
        from app.memory.cross_session_retriever import CrossSessionRetriever

        with mock.patch.dict(
            os.environ,
            {"LUCIEL_CROSS_SESSION_RETRIEVER_ENABLED": ""},
            clear=False,
        ):
            os.environ.pop(
                "LUCIEL_CROSS_SESSION_RETRIEVER_ENABLED", None
            )
            r = CrossSessionRetriever(db=_GucStubDb(admin_guc="admin-A"))
            with self.assertRaises(RuntimeError) as cm:
                r.retrieve(
                    conversation_id=CONV_ID,
                    admin_id="t1",
                )
            self.assertIn("quarantined", str(cm.exception).lower())

    def test_refuses_with_flag_set_but_no_admin_guc(self):
        from app.memory.cross_session_retriever import CrossSessionRetriever

        with mock.patch.dict(
            os.environ,
            {"LUCIEL_CROSS_SESSION_RETRIEVER_ENABLED": "1"},
        ):
            r = CrossSessionRetriever(db=_GucStubDb(admin_guc=None))
            with self.assertRaises(RuntimeError) as cm:
                r.retrieve(
                    conversation_id=CONV_ID,
                    admin_id="t1",
                )
            self.assertIn("app.admin_id", str(cm.exception).lower())

    def test_refuses_with_flag_set_but_empty_admin_guc(self):
        from app.memory.cross_session_retriever import CrossSessionRetriever

        with mock.patch.dict(
            os.environ,
            {"LUCIEL_CROSS_SESSION_RETRIEVER_ENABLED": "1"},
        ):
            r = CrossSessionRetriever(db=_GucStubDb(admin_guc=""))
            with self.assertRaises(RuntimeError) as cm:
                r.retrieve(
                    conversation_id=CONV_ID,
                    admin_id="t1",
                )
            self.assertIn("guc", str(cm.exception).lower())

    def test_passes_gate_with_flag_set_and_guc_bound(self):
        """With both prerequisites satisfied, the gate lets the call
        proceed (the call still completes via the empty stub)."""
        from app.memory.cross_session_retriever import CrossSessionRetriever

        with mock.patch.dict(
            os.environ,
            {"LUCIEL_CROSS_SESSION_RETRIEVER_ENABLED": "1"},
        ):
            r = CrossSessionRetriever(db=_GucStubDb(admin_guc="admin-A"))
            out = r.retrieve(
                conversation_id=CONV_ID,
                admin_id="t1",
            )
            self.assertEqual(out, [])


class TestGateDoesNotBlockInputValidation(unittest.TestCase):
    """Input-validation errors must still fire BEFORE the gate."""

    def test_blank_tenant_id_still_raises_value_error(self):
        from app.memory.cross_session_retriever import CrossSessionRetriever

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(
                "LUCIEL_CROSS_SESSION_RETRIEVER_ENABLED", None
            )
            r = CrossSessionRetriever(db=_GucStubDb(admin_guc="x"))
            with self.assertRaises(ValueError):
                r.retrieve(
                    conversation_id=CONV_ID,
                    admin_id="   ",
                )

    def test_non_uuid_still_raises_type_error(self):
        from app.memory.cross_session_retriever import CrossSessionRetriever

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(
                "LUCIEL_CROSS_SESSION_RETRIEVER_ENABLED", None
            )
            r = CrossSessionRetriever(db=_GucStubDb(admin_guc="x"))
            with self.assertRaises(TypeError):
                r.retrieve(
                    conversation_id="not-a-uuid",  # type: ignore[arg-type]
                    admin_id="t1",
                )


if __name__ == "__main__":
    unittest.main()
