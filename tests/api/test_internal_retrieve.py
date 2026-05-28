"""Arc 11 Step 7 — /internal/v1/retrieve contract tests.

Static-shape + a small in-process unit test for the 403-on-non-
platform-admin gate. The live-DB equivalents (2-tenant scope
isolation + EXPLAIN-ANALYZE includes ``Index Scan``) live in
``tests/db/test_arc11_internal_retrieve_live.py`` opt-in via
``LUCIEL_LIVE_POSTGRES_URL``, matching the Step-4 / Step-5 / Step-6
precedent.

Contracts:

  I1   Endpoint registered at POST /internal/v1/retrieve.
  I2   Request schema has the four locked fields.
  I3   Response schema includes ``explain`` and ``chunks`` with the
       per-chunk source_identifier surface.
  I4   Non-platform-admin → 403 (verified by directly calling the
       handler with a synthetic Request whose state has no
       platform_admin permission).
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import HTTPException
from starlette.requests import Request

from app.api.v1 import admin_knowledge as ak


def _make_request(*, permissions: list[str]) -> Request:
    """Build a minimal Starlette Request whose .state carries the
    given permissions. The slowapi @limiter.limit decorator on the
    handler requires a real Starlette Request instance — a
    SimpleNamespace is rejected."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/internal/v1/retrieve",
        "headers": [(b"host", b"test")],
        "query_string": b"",
        "client": ("127.0.0.1", 0),
    }
    req = Request(scope)
    req.state.permissions = permissions
    req.state.admin_id = None
    return req


class TestInternalRetrieveContract(unittest.TestCase):

    # ----- I1 -----
    def test_i1_route_path_and_method(self):
        actual = {
            (route.path, method)
            for route in ak.internal_router.routes
            for method in (route.methods or ())
            if method != "HEAD"
        }
        self.assertEqual(actual, {("/internal/v1/retrieve", "POST")})

    # ----- I2 -----
    def test_i2_request_schema_locked_fields(self):
        fields = set(ak.InternalRetrieveRequest.model_fields.keys())
        self.assertEqual(fields, {"admin_id", "instance_id", "query", "top_k"})

    def test_i2_top_k_default_is_five(self):
        self.assertEqual(
            ak.InternalRetrieveRequest.model_fields["top_k"].default, 5,
        )

    # ----- I3 -----
    def test_i3_response_schema_has_chunks_and_explain(self):
        fields = set(ak.InternalRetrieveResponse.model_fields.keys())
        self.assertEqual(fields, {"chunks", "explain"})

    def test_i3_chunk_schema_exposes_source_identifier(self):
        fields = set(ak.InternalRetrieveChunk.model_fields.keys())
        self.assertEqual(
            fields, {"chunk_id", "content", "distance", "source_identifier"},
        )
        # source_identifier must be int | str | None — same shape the
        # retriever publishes in RetrievedChunk.
        annot = str(
            ak.InternalRetrieveChunk.model_fields["source_identifier"].annotation
        )
        self.assertIn("int", annot)
        self.assertIn("str", annot)


class TestInternalRetrievePlatformAdminGate(unittest.TestCase):
    """The platform_admin gate is the only auth check on this route.
    Call the handler directly with a synthetic Request whose state
    has no platform_admin permission; expect HTTPException(403)."""

    def test_i4_non_platform_admin_gets_403(self):
        req = _make_request(permissions=["admin"])  # NOT platform_admin
        payload = ak.InternalRetrieveRequest(
            admin_id="some-tenant",
            instance_id=1,
            query="anything",
            top_k=5,
        )
        with self.assertRaises(HTTPException) as ctx:
            ak.internal_retrieve(
                request=req,
                payload=payload,
                db=MagicMock(),
            )
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("platform_admin", str(ctx.exception.detail))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
