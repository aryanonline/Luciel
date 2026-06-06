"""Unit 13g ITEM 3 — connector-readiness catalog (shape + readiness).

The read-only GET /admin/connections/catalog endpoint reports, per
supported connection_type, its §3.8.5 auth_class and whether the
connector CAN connect live in the deploy environment (``is_ready``):

  * api_key (record_source / outbound_webhook)  → always ready (the
    backing is per-tenant config supplied at configure time).
  * provisioned_resource (email_sender / sms_sender) → ready iff the
    platform sender identity is present in settings (same
    ``_provisioned_resource_identity`` gate the configure path uses).
  * oauth_token (calendar / crm) → ready iff the OAuth client creds are
    configured (same ``provider.is_configured()`` gate the connect path
    uses).

Behavioural (no live TestClient/DB) — exercises ``_connector_is_ready``
and ``connector_catalog`` directly with a stub settings, mirroring
tests/api/test_unit13c_connector_connect_paths.py. Also a small shape
assertion that the route is wired read-only with admin auth and carries
no secrets / per-tenant data.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

REPO_ROOT = Path(__file__).resolve().parents[2]
CONN_PATH = REPO_ROOT / "app" / "api" / "v1" / "admin_connections.py"


class _Settings:
    """Stub carrying only the fields the readiness gates read."""

    def __init__(self, **kw):
        # provisioned_resource gates
        self.email_sender_from_address = kw.get("email_sender_from_address", "")
        self.email_sender_from_name = kw.get("email_sender_from_name", "")
        self.twilio_account_sid = kw.get("twilio_account_sid", "")
        self.twilio_auth_token = kw.get("twilio_auth_token", "")
        self.twilio_messaging_service_sid = kw.get(
            "twilio_messaging_service_sid", ""
        )
        # oauth_token gates (calendar / crm)
        self.google_oauth_client_id = kw.get("google_oauth_client_id", "")
        self.google_oauth_client_secret = kw.get(
            "google_oauth_client_secret", ""
        )
        self.google_oauth_redirect_uri = kw.get(
            "google_oauth_redirect_uri", ""
        )
        self.hubspot_oauth_client_id = kw.get("hubspot_oauth_client_id", "")
        self.hubspot_oauth_client_secret = kw.get(
            "hubspot_oauth_client_secret", ""
        )
        self.hubspot_oauth_redirect_uri = kw.get(
            "hubspot_oauth_redirect_uri", ""
        )
        self.salesforce_oauth_client_id = kw.get(
            "salesforce_oauth_client_id", ""
        )
        self.salesforce_oauth_client_secret = kw.get(
            "salesforce_oauth_client_secret", ""
        )
        self.salesforce_oauth_redirect_uri = kw.get(
            "salesforce_oauth_redirect_uri", ""
        )
        self.salesforce_oauth_login_base = kw.get(
            "salesforce_oauth_login_base", "https://login.salesforce.com"
        )


# ---------------------------------------------------------------------
# api_key — always ready (no deploy gate).
# ---------------------------------------------------------------------


def test_api_key_connectors_always_ready() -> None:
    from app.api.v1.admin_connections import _connector_is_ready

    s = _Settings()  # nothing configured
    assert _connector_is_ready(s, "record_source") is True
    assert _connector_is_ready(s, "outbound_webhook") is True


# ---------------------------------------------------------------------
# provisioned_resource — ready iff platform sender identity present.
# ---------------------------------------------------------------------


def test_provisioned_resource_unready_when_identity_absent() -> None:
    from app.api.v1.admin_connections import _connector_is_ready

    s = _Settings()
    assert _connector_is_ready(s, "email_sender") is False
    assert _connector_is_ready(s, "sms_sender") is False


def test_email_sender_ready_when_from_address_present() -> None:
    from app.api.v1.admin_connections import _connector_is_ready

    s = _Settings(email_sender_from_address="noreply@x.com")
    assert _connector_is_ready(s, "email_sender") is True


def test_sms_sender_ready_requires_both_sid_and_token() -> None:
    from app.api.v1.admin_connections import _connector_is_ready

    # sid only → not ready (mirrors the send_sms gate)
    assert _connector_is_ready(
        _Settings(twilio_account_sid="ACxxxx"), "sms_sender"
    ) is False
    # both present → ready
    assert _connector_is_ready(
        _Settings(twilio_account_sid="ACxxxx", twilio_auth_token="tok"),
        "sms_sender",
    ) is True


# ---------------------------------------------------------------------
# oauth_token — ready iff OAuth client creds configured.
# ---------------------------------------------------------------------


def test_oauth_connectors_unready_when_creds_absent() -> None:
    from app.api.v1.admin_connections import _connector_is_ready

    s = _Settings()
    assert _connector_is_ready(s, "calendar") is False
    assert _connector_is_ready(s, "crm") is False


def test_calendar_ready_when_google_creds_present() -> None:
    from app.api.v1.admin_connections import _connector_is_ready

    s = _Settings(
        google_oauth_client_id="gid",
        google_oauth_client_secret="gsecret",
        google_oauth_redirect_uri="https://x/cb",
    )
    assert _connector_is_ready(s, "calendar") is True


def test_crm_ready_when_hubspot_creds_present() -> None:
    from app.api.v1.admin_connections import _connector_is_ready

    s = _Settings(
        hubspot_oauth_client_id="hid",
        hubspot_oauth_client_secret="hsecret",
        hubspot_oauth_redirect_uri="https://x/cb",
    )
    assert _connector_is_ready(s, "crm") is True


def test_crm_ready_when_only_salesforce_creds_present() -> None:
    from app.api.v1.admin_connections import _connector_is_ready

    # No HubSpot creds → factory falls through to Salesforce; ready iff
    # Salesforce client creds present.
    s = _Settings(
        salesforce_oauth_client_id="sid",
        salesforce_oauth_client_secret="ssecret",
        salesforce_oauth_redirect_uri="https://x/cb",
    )
    assert _connector_is_ready(s, "crm") is True


# ---------------------------------------------------------------------
# Catalog response shape — one entry per supported type, no secrets.
# ---------------------------------------------------------------------


def test_catalog_covers_every_connection_type_with_auth_class() -> None:
    from app.connections.instance_connection import (
        CONNECTION_TYPES,
        auth_class_for,
    )
    from app.schemas.connection import ConnectorCatalogEntry

    # The endpoint builds one entry per CONNECTION_TYPES; emulate its body
    # with a fully-configured stub so every auth_class resolves ready, and
    # assert the (connection_type, auth_class) shape is exhaustive.
    from app.api.v1.admin_connections import _connector_is_ready

    s = _Settings(
        email_sender_from_address="noreply@x.com",
        twilio_account_sid="ACxxxx",
        twilio_auth_token="tok",
        google_oauth_client_id="gid",
        google_oauth_client_secret="gsecret",
        google_oauth_redirect_uri="https://x/cb",
        hubspot_oauth_client_id="hid",
        hubspot_oauth_client_secret="hsecret",
        hubspot_oauth_redirect_uri="https://x/cb",
    )
    entries = [
        ConnectorCatalogEntry(
            connection_type=ct,
            auth_class=auth_class_for(ct),
            is_ready=_connector_is_ready(s, ct),
        )
        for ct in CONNECTION_TYPES
    ]
    assert {e.connection_type for e in entries} == set(CONNECTION_TYPES)
    for e in entries:
        assert e.auth_class == auth_class_for(e.connection_type)
        assert e.is_ready is True  # fully configured stub → all ready


def test_catalog_entry_carries_no_secret_fields() -> None:
    from app.schemas.connection import ConnectorCatalogEntry

    fields = set(ConnectorCatalogEntry.model_fields)
    assert fields == {"connection_type", "auth_class", "is_ready"}
    # No secret-shaped field name leaks into the catalog contract.
    for forbidden in (
        "secret",
        "token",
        "client_secret",
        "auth_token",
        "non_secret_config",
        "secret_ref",
    ):
        assert forbidden not in fields


def test_catalog_route_is_read_only_admin_scoped_no_secrets() -> None:
    for node in ast.walk(ast.parse(CONN_PATH.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef) and node.name == "connector_catalog":
            src = ast.unparse(node)
            break
    else:  # pragma: no cover
        raise AssertionError("connector_catalog route not found")
    # Authenticated admin context like the other connections routes.
    assert "_require_admin_id" in src
    # Read-only: no write/persist/secret-store call in the route body.
    for forbidden in (
        "get_secret_store",
        "AdminAuditRepository",
        ".configure(",
        ".commit(",
        "secret_ref",
    ):
        assert forbidden not in src
