"""Arc 8 WU-6 Phase C regression tests -- ses_events route shape.

Closes (at the test layer) the WU-6 cohort drifts:
  * D-ses-feedback-loop-not-wired-2026-05-22
  * D-ses-suppression-app-layer-not-implemented-2026-05-22

The Phase A test surface already pinned the service-layer shape
(``tests/services/test_email_suppression_service.py``); this Phase C
test pins the HTTP-binding-layer shape per the same AST/text-assertion
doctrine the WU-6 cohort follows.

Test strategy (mirroring Phase A):
  - AST/text assertions against the shipped route source + the router
    + the auth middleware skip-list + the settings reservation. The
    HTTP path is not executed end-to-end (that would need a Postgres
    fixture, the Alembic chain applied, and a TestClient against a
    fully-booted app); the static-text pins are the regression gate.
  - Cases cover:
      1. Route module exists at app/api/v1/ses_events.py.
      2. Route module exposes ``router`` (APIRouter) with one POST
         handler at "/ses-events".
      3. Handler uses the get_db dep and reads settings.ses_sns_topic_arn.
      4. The three SNS message types are handled.
      5. The two-check trust gate is present (TopicArn allowlist +
         SigningCertURL host check).
      6. The handler imports and uses ``record_suppression`` from
         email_suppression_service.
      7. The handler imports and uses SES_EVENT_BOUNCE +
         SES_EVENT_COMPLAINT + SES_EVENT_TYPES from the event model.
      8. The handler imports SUPPRESSION_REASON_HARD_BOUNCE +
         SUPPRESSION_REASON_COMPLAINT from the suppression model.
      9. The handler creates EmailSendEvent rows and catches
         IntegrityError for the duplicate-SNS-delivery idempotency
         path.
     10. The handler is wired into ``app/api/router.py``.
     11. The route is in SKIP_AUTH_PATHS in ``app/middleware/auth.py``.
     12. The settings reserve ``ses_sns_topic_arn`` field with
         empty-string default.

Pattern E: pure addition. No existing tests mutated.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
ROUTE_PATH = REPO_ROOT / "app" / "api" / "v1" / "ses_events.py"
ROUTER_PATH = REPO_ROOT / "app" / "api" / "router.py"
MIDDLEWARE_PATH = REPO_ROOT / "app" / "middleware" / "auth.py"
SETTINGS_PATH = REPO_ROOT / "app" / "core" / "config.py"


@pytest.fixture(scope="module")
def route_src() -> str:
    return ROUTE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def route_ast(route_src: str) -> ast.Module:
    return ast.parse(route_src)


@pytest.fixture(scope="module")
def router_src() -> str:
    return ROUTER_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def middleware_src() -> str:
    return MIDDLEWARE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def settings_src() -> str:
    return SETTINGS_PATH.read_text(encoding="utf-8")


# -----------------------------------------------------------------
# Test 1 -- route module exists.
# -----------------------------------------------------------------


def test_route_module_exists() -> None:
    assert ROUTE_PATH.exists(), (
        "Arc 8 WU-6 Phase C expects app/api/v1/ses_events.py to exist as "
        "the HTTP-binding layer for the SES feedback / suppression sink."
    )


# -----------------------------------------------------------------
# Test 2 -- route module exposes APIRouter named ``router``.
# -----------------------------------------------------------------


def test_route_module_exposes_router(route_src: str) -> None:
    assert "router = APIRouter()" in route_src, (
        "ses_events.py must expose ``router = APIRouter()`` so it can be "
        "include_router'd by app/api/router.py."
    )


# -----------------------------------------------------------------
# Test 3 -- exactly one @router.post for "/ses-events".
# -----------------------------------------------------------------


def test_route_module_has_ses_events_post(route_src: str) -> None:
    assert '@router.post("/ses-events")' in route_src, (
        "ses_events.py must register POST /ses-events (the SNS HTTPS "
        "subscription endpoint)."
    )


# -----------------------------------------------------------------
# Test 4 -- handler uses get_db dep + reads settings.ses_sns_topic_arn.
# -----------------------------------------------------------------


def test_handler_uses_get_db(route_src: str) -> None:
    assert "from app.db.session import get_db" in route_src
    assert "Depends(get_db)" in route_src, (
        "Handler must use Depends(get_db) for the DB session injection "
        "(standard pattern; see e.g. app/api/v1/sessions.py)."
    )


def test_handler_reads_topic_arn_setting(route_src: str) -> None:
    assert "settings.ses_sns_topic_arn" in route_src, (
        "Handler must read settings.ses_sns_topic_arn as one half of "
        "the two-check trust gate."
    )


# -----------------------------------------------------------------
# Test 5 -- three SNS message types handled.
# -----------------------------------------------------------------


def test_sns_message_types_handled(route_src: str) -> None:
    for sns_type in (
        "SubscriptionConfirmation",
        "Notification",
        "UnsubscribeConfirmation",
    ):
        assert sns_type in route_src, (
            f"Handler must reference the SNS Type {sns_type!r} "
            "(see route module docstring section 'SNS-to-HTTPS contract')."
        )


# -----------------------------------------------------------------
# Test 6 -- two-check trust gate present.
# -----------------------------------------------------------------


def test_trust_gate_topicarn_check(route_src: str) -> None:
    assert "TopicArn" in route_src
    assert "TopicArn mismatch" in route_src or "TopicArn not allowed" in route_src, (
        "Handler must reject messages whose TopicArn does not match "
        "settings.ses_sns_topic_arn (when set) with a 403."
    )


def test_trust_gate_signing_cert_url_check(route_src: str) -> None:
    assert "SigningCertURL" in route_src
    assert "amazonaws.com" in route_src, (
        "Handler must reject messages whose SigningCertURL host is not "
        "under *.amazonaws.com with a 403."
    )


# -----------------------------------------------------------------
# Test 7 -- handler imports and uses record_suppression.
# -----------------------------------------------------------------


def test_handler_imports_record_suppression(route_src: str) -> None:
    assert (
        "from app.services.email_suppression_service import" in route_src
    ), "Handler must import from email_suppression_service."
    assert "record_suppression" in route_src
    # Confirm at least two call sites (HardBounce branch + Complaint branch).
    assert route_src.count("record_suppression(") >= 2, (
        "Handler must call record_suppression at minimum twice -- once "
        "from the HardBounce branch and once from the Complaint branch."
    )


# -----------------------------------------------------------------
# Test 8 -- event model + suppression model constants imported.
# -----------------------------------------------------------------


def test_handler_imports_event_model_constants(route_src: str) -> None:
    for sym in ("SES_EVENT_BOUNCE", "SES_EVENT_COMPLAINT", "SES_EVENT_TYPES"):
        assert sym in route_src, (
            f"Handler must import / reference {sym} from "
            "app.models.email_send_event."
        )


def test_handler_imports_suppression_reasons(route_src: str) -> None:
    for sym in (
        "SUPPRESSION_REASON_HARD_BOUNCE",
        "SUPPRESSION_REASON_COMPLAINT",
    ):
        assert sym in route_src, (
            f"Handler must import {sym} from app.models.email_suppression "
            "to pass as the ``reason`` arg to record_suppression."
        )


# -----------------------------------------------------------------
# Test 9 -- handler creates EmailSendEvent + handles IntegrityError.
# -----------------------------------------------------------------


def test_handler_creates_email_send_event(route_src: str) -> None:
    assert "EmailSendEvent(" in route_src, (
        "Handler must construct EmailSendEvent rows from the SES event payload."
    )


def test_handler_catches_integrity_error(route_src: str) -> None:
    assert "from sqlalchemy.exc import IntegrityError" in route_src
    assert "except IntegrityError" in route_src, (
        "Handler must catch IntegrityError on the email_send_event INSERT "
        "to honour SNS at-least-once-delivery idempotency on duplicate "
        "MessageId."
    )


# -----------------------------------------------------------------
# Test 10 -- handler wired into app/api/router.py.
# -----------------------------------------------------------------


def test_handler_wired_into_router(router_src: str) -> None:
    assert "from app.api.v1 import ses_events" in router_src, (
        "app/api/router.py must import ses_events."
    )
    assert "api_router.include_router(ses_events.router)" in router_src, (
        "app/api/router.py must include_router(ses_events.router)."
    )


# -----------------------------------------------------------------
# Test 11 -- /api/v1/ses-events in SKIP_AUTH_PATHS.
# -----------------------------------------------------------------


def test_route_in_skip_auth_paths(middleware_src: str) -> None:
    assert '"/api/v1/ses-events"' in middleware_src, (
        "app/middleware/auth.py SKIP_AUTH_PATHS must include "
        "'/api/v1/ses-events' -- SNS POSTs do not carry an api key; "
        "the route's own two-check trust gate is the auth gate."
    )


# -----------------------------------------------------------------
# Test 12 -- settings reserves ses_sns_topic_arn with empty default.
# -----------------------------------------------------------------


def test_settings_reserves_ses_sns_topic_arn(settings_src: str) -> None:
    assert 'ses_sns_topic_arn: str = ""' in settings_src, (
        "app/core/config.py must reserve ``ses_sns_topic_arn: str = '' "
        "as the configurable SNS-topic-allowlist field (empty default "
        "means do-not-enforce, used in tests and pre-subscription "
        "bring-up)."
    )


# -----------------------------------------------------------------
# Test 13 -- the rate limiter is applied to the route.
# -----------------------------------------------------------------


def test_route_is_rate_limited(route_src: str) -> None:
    assert "from app.middleware.rate_limit import limiter" in route_src
    assert "@limiter.limit(" in route_src, (
        "POST /ses-events must be rate-limited (per the public-surface "
        "doctrine -- compare app/api/v1/health.py /version)."
    )


# -----------------------------------------------------------------
# Test 14 -- the AST parses cleanly (catches syntax regressions).
# -----------------------------------------------------------------


def test_route_module_ast_parses(route_ast: ast.Module) -> None:
    # If route_src didn't parse, the fixture would have raised.
    # Confirm at least one async function def named receive_ses_event.
    func_names = {
        node.name
        for node in ast.walk(route_ast)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "receive_ses_event" in func_names, (
        "ses_events.py must define receive_ses_event (the SNS POST handler)."
    )
