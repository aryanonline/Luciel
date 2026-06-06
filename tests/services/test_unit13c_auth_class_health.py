"""Unit 13c — auth_class-driven §3.8.5 health service.

Pins the per-auth_class cadence + action contract of
``ConnectionHealthService`` after the rewrite from the hardcoded
LIVE/DEFERRED fork to auth_class dispatch:

  * cadence_for() returns the §3.8.5 interval per class (constants).
  * oauth_token: refresh-fail → expired + notify_admin marker (the
    admin-reconnect seam); refresh-success → connected.
  * provisioned_resource: sender-identity presence → connected / error,
    no refresh.
  * api_key / long_lived_token: config-presence liveness, NO auto-refresh;
    a 12-month-old static credential surfaces a hygiene nudge in detail
    ONLY (never a status change).
  * next_backoff(): exponential 2× from 1-min base, capped at 1h.

All checks run against InstanceConnection-shaped stubs + fakes (no DB, no
network), mirroring tests/services/test_arc17_connections_completion.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import app.services.connection_health_service as mod
from app.services.connection_health_service import (
    API_KEY_CHECK_INTERVAL,
    LONG_LIVED_CHECK_INTERVAL,
    OAUTH_CHECK_INTERVAL,
    PROVISIONED_RESOURCE_CHECK_INTERVAL,
    RATE_LIMIT_BACKOFF_BASE,
    RATE_LIMIT_BACKOFF_MAX,
    ConnectionHealthService,
    cadence_for,
    next_backoff,
)


class _StubConn:
    """Minimal InstanceConnection-shaped stub. ``auth_class`` defaults to
    None so the service derives it from connection_type (matching a
    pre-migration / SQLite-stub row)."""

    def __init__(
        self,
        *,
        connection_type,
        non_secret_config=None,
        secret_ref=None,
        auth_class=None,
        last_health_check_at=None,
        created_at=None,
    ):
        self.id = 1
        self.connection_type = connection_type
        self.non_secret_config = non_secret_config
        self.secret_ref = secret_ref
        self.auth_class = auth_class
        self.last_health_check_at = last_health_check_at
        self.created_at = created_at


def _settings():
    from app.core.config import settings

    return settings


# ---------------------------------------------------------------------
# Cadence constants — one per §3.8.5 class, with a conservative default.
# ---------------------------------------------------------------------


def test_cadence_per_class_matches_constants() -> None:
    assert cadence_for("oauth_token") == OAUTH_CHECK_INTERVAL == timedelta(minutes=15)
    assert cadence_for("long_lived_token") == LONG_LIVED_CHECK_INTERVAL == timedelta(minutes=60)
    assert cadence_for("api_key") == API_KEY_CHECK_INTERVAL == timedelta(minutes=60)
    assert (
        cadence_for("provisioned_resource")
        == PROVISIONED_RESOURCE_CHECK_INTERVAL
        == timedelta(hours=4)
    )


def test_cadence_unknown_class_falls_back_to_shortest() -> None:
    # An unknown class is checked MORE often (the shortest cadence), never
    # less — a mis-classified row fails safe.
    assert cadence_for("not_a_class") == OAUTH_CHECK_INTERVAL


# ---------------------------------------------------------------------
# 429 backoff math — exponential, capped.
# ---------------------------------------------------------------------


def test_next_backoff_starts_at_base_and_doubles() -> None:
    assert next_backoff(None) == RATE_LIMIT_BACKOFF_BASE
    assert next_backoff(timedelta(0)) == RATE_LIMIT_BACKOFF_BASE
    assert next_backoff(timedelta(minutes=1)) == timedelta(minutes=2)
    assert next_backoff(timedelta(minutes=2)) == timedelta(minutes=4)


def test_next_backoff_caps_at_max() -> None:
    assert next_backoff(timedelta(minutes=40)) == RATE_LIMIT_BACKOFF_MAX
    assert next_backoff(RATE_LIMIT_BACKOFF_MAX) == RATE_LIMIT_BACKOFF_MAX


# ---------------------------------------------------------------------
# oauth_token — refresh action; expired carries notify_admin.
# ---------------------------------------------------------------------


def test_oauth_refresh_fail_expired_sets_notify_admin() -> None:
    from app.integrations.oauth import OAuthError
    from app.integrations.secrets import LocalFakeSecretStore

    store = LocalFakeSecretStore()
    cred_ref = store.put("oauth/refresh/x", "stale")

    class _FakeProvider:
        def is_configured(self):
            return True

        def refresh(self, *, refresh_token):
            raise OAuthError("invalid_grant")

    svc = ConnectionHealthService(_settings(), secret_store=store)
    orig = mod.get_oauth_provider
    mod.get_oauth_provider = lambda ct, s: _FakeProvider()
    try:
        res = svc.check_health(
            _StubConn(connection_type="crm", secret_ref=cred_ref)
        )
    finally:
        mod.get_oauth_provider = orig

    assert res.status == "expired"
    assert res.notify_admin is True
    assert res.checked_at is not None
    assert res.detail  # human-readable reconnect line


def test_oauth_refresh_success_connected_no_notify() -> None:
    from app.integrations.oauth import OAuthTokens
    from app.integrations.secrets import LocalFakeSecretStore

    store = LocalFakeSecretStore()
    cred_ref = store.put("oauth/refresh/y", "old")

    class _FakeProvider:
        def is_configured(self):
            return True

        def refresh(self, *, refresh_token):
            return OAuthTokens(
                access_token="a", refresh_token="new", expires_in=3600
            )

    svc = ConnectionHealthService(_settings(), secret_store=store)
    orig = mod.get_oauth_provider
    mod.get_oauth_provider = lambda ct, s: _FakeProvider()
    try:
        res = svc.check_health(
            _StubConn(connection_type="calendar", secret_ref=cred_ref)
        )
    finally:
        mod.get_oauth_provider = orig

    assert res.status == "connected"
    assert res.notify_admin is False


# ---------------------------------------------------------------------
# provisioned_resource — sender-identity presence, no refresh.
# ---------------------------------------------------------------------


def test_provisioned_email_sender_identity_present_connected() -> None:
    svc = ConnectionHealthService(_settings())
    res = svc.check_health(
        _StubConn(
            connection_type="email_sender",
            non_secret_config={"from_address": "noreply@x.com"},
        )
    )
    assert res.status == "connected"
    assert res.checked_at is not None
    assert res.new_secret_ref is None  # never rotates a per-tenant secret


def test_provisioned_sms_sender_identity_absent_error() -> None:
    svc = ConnectionHealthService(_settings())
    res = svc.check_health(
        _StubConn(connection_type="sms_sender", non_secret_config={})
    )
    assert res.status == "error"
    assert res.checked_at is not None


# ---------------------------------------------------------------------
# api_key / long_lived_token — liveness, no auto-refresh, hygiene nudge.
# ---------------------------------------------------------------------


def test_api_key_liveness_no_auto_refresh() -> None:
    svc = ConnectionHealthService(_settings())
    res = svc.check_health(
        _StubConn(
            connection_type="record_source",
            non_secret_config={"store_ref": "s3://x"},
        )
    )
    assert res.status == "connected"
    assert res.new_secret_ref is None  # no rotation for static keys


def test_long_lived_token_degrades_to_liveness() -> None:
    # No live provider backs long_lived_token today; an explicit
    # auth_class='long_lived_token' row degrades to the api_key config-
    # presence liveness (outbound_webhook needs 'url').
    svc = ConnectionHealthService(_settings())
    res = svc.check_health(
        _StubConn(
            connection_type="outbound_webhook",
            auth_class="long_lived_token",
            non_secret_config={"url": "https://hook.x.com/abc"},
        )
    )
    assert res.status == "connected"


def test_api_key_hygiene_nudge_in_detail_only_not_status() -> None:
    old = datetime.now(timezone.utc) - timedelta(days=400)
    svc = ConnectionHealthService(_settings())
    res = svc.check_health(
        _StubConn(
            connection_type="record_source",
            non_secret_config={"store_ref": "s3://x"},
            last_health_check_at=old,
        )
    )
    # Status is unaffected by the nudge; the advice is in detail only.
    assert res.status == "connected"
    assert "rotat" in res.detail.lower()


def test_api_key_recent_credential_no_hygiene_nudge() -> None:
    recent = datetime.now(timezone.utc) - timedelta(days=10)
    svc = ConnectionHealthService(_settings())
    res = svc.check_health(
        _StubConn(
            connection_type="record_source",
            non_secret_config={"store_ref": "s3://x"},
            last_health_check_at=recent,
        )
    )
    assert res.status == "connected"
    assert "rotat" not in res.detail.lower()
