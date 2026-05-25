"""Arc 9 WS4c -- structured request-logging middleware contract.

Pure unit tests, no DB. We build a minimal Starlette app with just
RequestLoggingMiddleware mounted, hit it with the TestClient, and
assert the log line and response header contract.
"""
from __future__ import annotations

import json
import logging
import os
import unittest

# DB stub required so any indirect app.* import does not blow up
# trying to build a live engine. RequestLoggingMiddleware itself
# does not touch the DB, but we import it via app.middleware which
# pulls a couple of sibling modules into the package namespace.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://stub:stub@localhost:5432/stub",
)

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.middleware.request_logging import (  # noqa: E402
    RequestLoggingMiddleware,
    _coerce_request_id,
    _LOG_SKIP_PATHS,
)


def _build_app(*, populate_state: bool = False) -> FastAPI:
    """Build a minimal app with RequestLoggingMiddleware mounted."""
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/api/v1/ok")
    def _ok(request: Request):
        if populate_state:
            request.state.user_id = "u-123"
            request.state.tenant_id = "t-abc"
            request.state.auth_method = "cookie"
        return {"ok": True}

    @app.get("/api/v1/forbidden")
    def _forbidden():
        raise HTTPException(status_code=403, detail="nope")

    @app.get("/api/v1/boom")
    def _boom():
        raise RuntimeError("synthetic")

    @app.get("/health")
    def _health():
        return {"status": "ok"}

    if populate_state:
        @app.get("/api/v1/ok_state")
        def _ok_state(request: Request):
            request.state.user_id = "u-123"
            request.state.tenant_id = "t-abc"
            request.state.auth_method = "cookie"
            return {"ok": True}

    return app


class _LogCapture(logging.Handler):
    """Capture luciel.request log records for assertion."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class TestArc9WS4cRequestIDCoercion(unittest.TestCase):
    """_coerce_request_id helper guards against log-injection."""

    def test_valid_inbound_id_is_propagated(self):
        out = _coerce_request_id("abc123-XYZ_456")
        self.assertEqual(out, "abc123-XYZ_456")

    def test_empty_inbound_id_generates_new(self):
        out = _coerce_request_id(None)
        self.assertTrue(out)
        self.assertEqual(len(out), 32)  # uuid4().hex

    def test_overlong_inbound_id_generates_new(self):
        out = _coerce_request_id("a" * 65)
        self.assertEqual(len(out), 32)
        self.assertNotEqual(out, "a" * 65)

    def test_injection_chars_in_inbound_id_generate_new(self):
        # Newline + quote would break a JSON log line if we trusted it.
        bad = 'evil"\n{"forged":true}'
        out = _coerce_request_id(bad)
        self.assertEqual(len(out), 32)
        self.assertNotIn('"', out)
        self.assertNotIn("\n", out)


class TestArc9WS4cMiddlewareContract(unittest.TestCase):
    """End-to-end contract via Starlette TestClient."""

    def setUp(self) -> None:
        self.capture = _LogCapture()
        self.logger = logging.getLogger("luciel.request")
        self.original_level = self.logger.level
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.capture)

    def tearDown(self) -> None:
        self.logger.removeHandler(self.capture)
        self.logger.setLevel(self.original_level)

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------

    def test_2xx_emits_info_log_with_full_payload(self):
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/ok")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("X-Request-ID", resp.headers)
        request_id = resp.headers["X-Request-ID"]
        self.assertTrue(request_id)

        # Exactly one log record per request.
        self.assertEqual(len(self.capture.records), 1)
        rec = self.capture.records[0]
        self.assertEqual(rec.levelno, logging.INFO)

        payload = json.loads(rec.getMessage())
        self.assertEqual(payload["evt"], "http_request")
        self.assertEqual(payload["method"], "GET")
        self.assertEqual(payload["route"], "/api/v1/ok")
        self.assertEqual(payload["status"], 200)
        self.assertEqual(payload["request_id"], request_id)
        self.assertIsInstance(payload["duration_ms"], int)
        self.assertGreaterEqual(payload["duration_ms"], 0)
        self.assertIsNone(payload["user_id"])
        self.assertIsNone(payload["tenant_id"])
        self.assertIsNone(payload["auth_method"])
        self.assertIsNone(payload["detail"])

    def test_inbound_request_id_is_propagated(self):
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get(
                "/api/v1/ok",
                headers={"X-Request-ID": "upstream-trace-9"},
            )

        self.assertEqual(resp.headers["X-Request-ID"], "upstream-trace-9")
        payload = json.loads(self.capture.records[0].getMessage())
        self.assertEqual(payload["request_id"], "upstream-trace-9")

    def test_malicious_inbound_request_id_is_rejected(self):
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get(
                "/api/v1/ok",
                headers={"X-Request-ID": "x\nY"},
            )

        # Header is rejected -> middleware generated a fresh uuid4 hex.
        echoed = resp.headers["X-Request-ID"]
        self.assertEqual(len(echoed), 32)
        self.assertNotIn("\n", echoed)

    # ------------------------------------------------------------------
    # State pickup
    # ------------------------------------------------------------------

    def test_state_user_id_and_tenant_id_are_logged(self):
        app = _build_app(populate_state=True)
        with TestClient(app) as client:
            resp = client.get("/api/v1/ok_state")

        self.assertEqual(resp.status_code, 200)
        payload = json.loads(self.capture.records[0].getMessage())
        self.assertEqual(payload["user_id"], "u-123")
        self.assertEqual(payload["tenant_id"], "t-abc")
        self.assertEqual(payload["auth_method"], "cookie")

    # ------------------------------------------------------------------
    # Error paths
    # ------------------------------------------------------------------

    def test_4xx_logs_at_warning(self):
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/forbidden")

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(len(self.capture.records), 1)
        rec = self.capture.records[0]
        self.assertEqual(rec.levelno, logging.WARNING)
        payload = json.loads(rec.getMessage())
        self.assertEqual(payload["status"], 403)
        self.assertIsNone(payload["detail"])  # no exception was raised

    def test_unhandled_exception_logs_at_error_with_detail_class(self):
        app = _build_app()
        # TestClient re-raises by default; suppress to inspect the log.
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/boom")

        # FastAPI's default 500 path returns 500 to the client.
        self.assertEqual(resp.status_code, 500)
        rec = self.capture.records[0]
        self.assertEqual(rec.levelno, logging.ERROR)
        payload = json.loads(rec.getMessage())
        self.assertEqual(payload["status"], 500)
        # Exception class name, not the message (no PII / SQL leak).
        self.assertEqual(payload["detail"], "RuntimeError")

    # ------------------------------------------------------------------
    # Skip list
    # ------------------------------------------------------------------

    def test_health_path_emits_no_log_but_still_carries_request_id(self):
        # Sanity: /health is in the skip list.
        self.assertIn("/health", _LOG_SKIP_PATHS)
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get("/health")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("X-Request-ID", resp.headers)
        # No log line emitted -- health probes don't pollute CloudWatch.
        self.assertEqual(len(self.capture.records), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
