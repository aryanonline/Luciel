"""Arc 17 deploy-gated LIVE connectors — behavioural tests.

Covers the three connectors the Arc 17 brief activates as DEPLOY-GATED
LIVE (full live code path, flipped purely by credentials landing in SSM,
NO code change at activation):

  * email_sender (send_email_tool)
  * sms_sender   (send_sms_tool)
  * push_to_crm  (native HubSpot / Salesforce OAuth)

For each connector the four states from the brief are asserted:
  (a) UNCONFIGURED → honest unconfigured/no-op, NO network.
  (b) CONFIGURED + master live-switch ON → the live path is invoked
      (transport mocked — never a real provider call).
  (c) CONFIGURED + live-switch OFF → an honest no-op receipt, NO send.
  (d) Token-refresh path for the OAuth providers (HubSpot + Salesforce),
      with httpx mocked so the wire is never touched.

The master live-switch defaults False, so the standard suite (no creds,
switch off) only ever exercises the honest no-network paths — a
mis-wired test can never bill a provider.
"""
from __future__ import annotations

import asyncio

import pytest

from app.tools.base import ToolContext


def _ctx() -> ToolContext:
    return ToolContext(admin_id="adm_1", instance_id=1)


# =====================================================================
# email_sender — send_email_tool
# =====================================================================


def test_send_email_unconfigured_no_send(monkeypatch) -> None:
    """(a) No verified sender identity → honest unconfigured, no SES."""
    from app.tools.implementations import send_email_tool as mod

    monkeypatch.setattr(mod.settings, "email_sender_from_address", "", raising=False)
    monkeypatch.setattr(mod.settings, "connectors_live_enabled", False, raising=False)

    out = asyncio.run(
        mod.SendEmailTool().execute(
            {"to": "x@y.com", "subject": "s", "body": "b"}, _ctx()
        )
    )
    assert out["success"] is False
    assert out["not_yet_available"] is True
    assert out["owning_arc"] == "ARC17"


def test_send_email_configured_live_off_noop(monkeypatch) -> None:
    """(c) Configured sender but live-switch OFF → no-op receipt, no SES."""
    from app.tools.implementations import send_email_tool as mod

    monkeypatch.setattr(
        mod.settings, "email_sender_from_address", "from@luciel.test", raising=False
    )
    monkeypatch.setattr(mod.settings, "connectors_live_enabled", False, raising=False)

    # Live transport must NEVER be reached on the off path.
    def _boom(*a, **k):
        raise AssertionError("live SES send must not run with live-switch off")

    monkeypatch.setattr(mod.SendEmailTool, "_send_live", _boom)

    out = asyncio.run(
        mod.SendEmailTool().execute(
            {"to": "x@y.com", "subject": "s", "body": "b"}, _ctx()
        )
    )
    assert out["success"] is True
    assert out["not_yet_available"] is False
    assert out["provider_message_id"].startswith("log-email-")


def test_send_email_configured_live_on_invokes_send(monkeypatch) -> None:
    """(b) Configured + live-switch ON → the live SES path is invoked."""
    from app.tools.implementations import send_email_tool as mod

    monkeypatch.setattr(
        mod.settings, "email_sender_from_address", "from@luciel.test", raising=False
    )
    monkeypatch.setattr(mod.settings, "connectors_live_enabled", True, raising=False)

    captured = {}

    def _fake_send_live(self, *, to, subject, body, from_address):
        captured.update(
            {"to": to, "subject": subject, "body": body, "from": from_address}
        )
        return "ses-msg-123"

    monkeypatch.setattr(mod.SendEmailTool, "_send_live", _fake_send_live)

    out = asyncio.run(
        mod.SendEmailTool().execute(
            {"to": "x@y.com", "subject": "Hi", "body": "Body"}, _ctx()
        )
    )
    assert out["success"] is True
    assert out["not_yet_available"] is False
    assert out["provider_message_id"] == "ses-msg-123"
    assert captured == {
        "to": "x@y.com",
        "subject": "Hi",
        "body": "Body",
        "from": "from@luciel.test",
    }


# =====================================================================
# sms_sender — send_sms_tool
# =====================================================================


def test_send_sms_unconfigured_no_send(monkeypatch) -> None:
    """(a) Twilio creds absent → honest unconfigured, no Twilio call."""
    from app.tools.implementations import send_sms_tool as mod

    monkeypatch.setattr(mod.settings, "twilio_account_sid", "", raising=False)
    monkeypatch.setattr(mod.settings, "twilio_auth_token", "", raising=False)
    monkeypatch.setattr(
        mod.settings, "channels_live_provisioning_enabled", False, raising=False
    )

    out = asyncio.run(
        mod.SendSmsTool().execute(
            {"to": "+15551234567", "body": "b"}, _ctx()
        )
    )
    assert out["success"] is False
    assert out["not_yet_available"] is True
    assert out["owning_arc"] == "ARC17"


def test_send_sms_configured_live_off_noop(monkeypatch) -> None:
    """(c) Twilio creds present but live-switch OFF → no-op, no Twilio."""
    from app.tools.implementations import send_sms_tool as mod

    monkeypatch.setattr(mod.settings, "twilio_account_sid", "ACxxxx", raising=False)
    monkeypatch.setattr(mod.settings, "twilio_auth_token", "tok", raising=False)
    monkeypatch.setattr(
        mod.settings, "channels_live_provisioning_enabled", False, raising=False
    )

    def _boom(*a, **k):
        raise AssertionError("live Twilio send must not run with live-switch off")

    monkeypatch.setattr(mod.SendSmsTool, "_send_live", _boom)

    out = asyncio.run(
        mod.SendSmsTool().execute(
            {"to": "+15551234567", "body": "b"}, _ctx()
        )
    )
    assert out["success"] is True
    assert out["not_yet_available"] is False
    assert out["provider_message_id"].startswith("SMfake")


def test_send_sms_configured_live_on_invokes_send(monkeypatch) -> None:
    """(b) Twilio creds + live-switch ON → the live Twilio path is invoked."""
    from app.tools.implementations import send_sms_tool as mod

    monkeypatch.setattr(mod.settings, "twilio_account_sid", "ACxxxx", raising=False)
    monkeypatch.setattr(mod.settings, "twilio_auth_token", "tok", raising=False)
    monkeypatch.setattr(
        mod.settings, "channels_live_provisioning_enabled", True, raising=False
    )

    captured = {}

    def _fake_send_live(self, *, to, body):
        captured.update({"to": to, "body": body})
        return "SM_real_sid"

    monkeypatch.setattr(mod.SendSmsTool, "_send_live", _fake_send_live)

    out = asyncio.run(
        mod.SendSmsTool().execute(
            {"to": "+15551234567", "body": "hello"}, _ctx()
        )
    )
    assert out["success"] is True
    assert out["not_yet_available"] is False
    assert out["provider_message_id"] == "SM_real_sid"
    assert captured == {"to": "+15551234567", "body": "hello"}


# =====================================================================
# push_to_crm — native HubSpot / Salesforce OAuth
# =====================================================================


def test_push_to_crm_unconfigured_no_push(monkeypatch) -> None:
    """(a) No native CRM OAuth creds → honest deferred, no network."""
    from app.tools.implementations import push_to_crm_tool as mod

    # Default settings: no hubspot/salesforce creds → provider unconfigured.
    monkeypatch.setattr(mod.settings, "hubspot_oauth_client_id", "", raising=False)
    monkeypatch.setattr(mod.settings, "hubspot_oauth_client_secret", "", raising=False)
    monkeypatch.setattr(mod.settings, "salesforce_oauth_client_id", "", raising=False)
    monkeypatch.setattr(
        mod.settings, "salesforce_oauth_client_secret", "", raising=False
    )
    monkeypatch.setattr(mod.settings, "connectors_live_enabled", False, raising=False)

    out = asyncio.run(
        mod.PushToCrmTool().execute(
            {"record_type": "lead", "payload": {"k": "v"}}, _ctx()
        )
    )
    assert out["success"] is False
    assert out["not_yet_available"] is True
    assert out["owning_arc"] == "ARC17"


def test_push_to_crm_configured_live_off_noop(monkeypatch) -> None:
    """(c) CRM OAuth configured but live-switch OFF → no-op, no network."""
    from app.tools.implementations import push_to_crm_tool as mod

    monkeypatch.setattr(
        mod.settings, "hubspot_oauth_client_id", "cid", raising=False
    )
    monkeypatch.setattr(
        mod.settings, "hubspot_oauth_client_secret", "sec", raising=False
    )
    monkeypatch.setattr(mod.settings, "connectors_live_enabled", False, raising=False)

    def _boom(*a, **k):
        raise AssertionError("live CRM push must not run with live-switch off")

    monkeypatch.setattr(mod.PushToCrmTool, "_push_live", _boom)

    out = asyncio.run(
        mod.PushToCrmTool().execute(
            {"record_type": "lead", "payload": {"k": "v"}}, _ctx()
        )
    )
    assert out["success"] is True
    assert out["not_yet_available"] is False
    assert out["provider"] == "crm"


def test_push_to_crm_configured_live_on_invokes_push(monkeypatch) -> None:
    """(b) CRM OAuth configured + live-switch ON → the live push runs."""
    from app.tools.implementations import push_to_crm_tool as mod

    monkeypatch.setattr(
        mod.settings, "hubspot_oauth_client_id", "cid", raising=False
    )
    monkeypatch.setattr(
        mod.settings, "hubspot_oauth_client_secret", "sec", raising=False
    )
    monkeypatch.setattr(mod.settings, "connectors_live_enabled", True, raising=False)

    captured = {}

    def _fake_push_live(self, *, provider, record_type, payload, context):
        captured.update({"record_type": record_type, "payload": payload})
        return {
            "success": True,
            "output": "CRM record created.",
            "not_yet_available": False,
            "provider": "crm",
        }

    monkeypatch.setattr(mod.PushToCrmTool, "_push_live", _fake_push_live)

    out = asyncio.run(
        mod.PushToCrmTool().execute(
            {"record_type": "contact", "payload": {"email": "a@b.com"}}, _ctx()
        )
    )
    assert out["success"] is True
    assert captured == {
        "record_type": "contact",
        "payload": {"email": "a@b.com"},
    }


# =====================================================================
# (d) OAuth provider token paths — HubSpot + Salesforce (httpx mocked).
# =====================================================================


class _FakeResp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


def test_hubspot_unconfigured_raises_before_network(monkeypatch) -> None:
    from app.integrations.oauth import OAuthNotConfiguredError
    from app.integrations.oauth.hubspot import HubSpotOAuthProvider

    provider = HubSpotOAuthProvider(
        client_id="", client_secret="", redirect_uri="https://x/cb"
    )
    assert provider.is_configured() is False

    # Any network attempt would be a bug — patch httpx.post to explode.
    import app.integrations.oauth.hubspot as hmod

    monkeypatch.setattr(
        hmod.httpx,
        "post",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("no network when unconfigured")
        ),
    )
    with pytest.raises(OAuthNotConfiguredError):
        provider.refresh(refresh_token="rt")
    with pytest.raises(OAuthNotConfiguredError):
        provider.exchange_code(code="c")
    with pytest.raises(OAuthNotConfiguredError):
        provider.authorization_url(state="s")


def test_hubspot_refresh_and_exchange_mocked(monkeypatch) -> None:
    from app.integrations.oauth.hubspot import HubSpotOAuthProvider
    import app.integrations.oauth.hubspot as hmod

    posted = {}

    def _fake_post(url, data=None, timeout=None):
        posted["url"] = url
        posted["data"] = data
        return _FakeResp(
            200,
            {
                "access_token": "at",
                "refresh_token": "new-rt",
                "expires_in": 1800,
                "scope": "crm.objects.contacts.write",
                "token_type": "bearer",
            },
        )

    monkeypatch.setattr(hmod.httpx, "post", _fake_post)
    provider = HubSpotOAuthProvider(
        client_id="cid", client_secret="sec", redirect_uri="https://x/cb"
    )
    tokens = provider.refresh(refresh_token="old-rt")
    assert tokens.access_token == "at"
    assert tokens.refresh_token == "new-rt"
    assert tokens.expires_in == 1800
    assert posted["url"] == "https://api.hubapi.com/oauth/v1/token"
    assert posted["data"]["grant_type"] == "refresh_token"
    assert posted["data"]["refresh_token"] == "old-rt"

    # exchange_code uses authorization_code grant.
    tokens2 = provider.exchange_code(code="abc")
    assert posted["data"]["grant_type"] == "authorization_code"
    assert posted["data"]["code"] == "abc"
    assert tokens2.access_token == "at"


def test_hubspot_token_error_surfaces_oauth_error(monkeypatch) -> None:
    from app.integrations.oauth import OAuthError
    from app.integrations.oauth.hubspot import HubSpotOAuthProvider
    import app.integrations.oauth.hubspot as hmod

    monkeypatch.setattr(
        hmod.httpx, "post", lambda *a, **k: _FakeResp(400, {"error": "invalid_grant"})
    )
    provider = HubSpotOAuthProvider(
        client_id="cid", client_secret="sec", redirect_uri="https://x/cb"
    )
    with pytest.raises(OAuthError):
        provider.refresh(refresh_token="bad")


def test_salesforce_refresh_mocked_and_login_base(monkeypatch) -> None:
    from app.integrations.oauth.salesforce import SalesforceOAuthProvider
    import app.integrations.oauth.salesforce as smod

    posted = {}

    def _fake_post(url, data=None, timeout=None):
        posted["url"] = url
        posted["data"] = data
        return _FakeResp(
            200,
            {"access_token": "sf-at", "refresh_token": None, "expires_in": 0},
        )

    monkeypatch.setattr(smod.httpx, "post", _fake_post)
    provider = SalesforceOAuthProvider(
        client_id="cid",
        client_secret="sec",
        redirect_uri="https://x/cb",
        login_base="https://test.salesforce.com/",
    )
    # Sandbox login base honoured (trailing slash trimmed).
    url = provider.authorization_url(state="s")
    assert url.startswith("https://test.salesforce.com/services/oauth2/authorize?")

    tokens = provider.refresh(refresh_token="rt")
    assert tokens.access_token == "sf-at"
    assert tokens.refresh_token is None  # Salesforce keeps the prior rt
    assert posted["url"] == "https://test.salesforce.com/services/oauth2/token"
    assert posted["data"]["grant_type"] == "refresh_token"


def test_factory_crm_prefers_hubspot_then_salesforce() -> None:
    from app.core.config import Settings
    from app.integrations.oauth import get_oauth_provider
    from app.integrations.oauth.hubspot import HubSpotOAuthProvider
    from app.integrations.oauth.salesforce import SalesforceOAuthProvider

    # Neither configured → Salesforce provider, unconfigured (no network).
    p = get_oauth_provider("crm", Settings())
    assert isinstance(p, SalesforceOAuthProvider)
    assert p.is_configured() is False

    # HubSpot configured → HubSpot wins.
    p2 = get_oauth_provider(
        "crm",
        Settings(
            hubspot_oauth_client_id="cid",
            hubspot_oauth_client_secret="sec",
            hubspot_oauth_redirect_uri="https://x/cb",
        ),
    )
    assert isinstance(p2, HubSpotOAuthProvider)
    assert p2.is_configured() is True

    # Only Salesforce configured → Salesforce, configured.
    p3 = get_oauth_provider(
        "crm",
        Settings(
            salesforce_oauth_client_id="cid",
            salesforce_oauth_client_secret="sec",
            salesforce_oauth_redirect_uri="https://x/cb",
        ),
    )
    assert isinstance(p3, SalesforceOAuthProvider)
    assert p3.is_configured() is True

    # email_sender / sms_sender are not OAuth-backed → None.
    assert get_oauth_provider("email_sender", Settings()) is None
    assert get_oauth_provider("sms_sender", Settings()) is None


def test_health_service_crm_unconfigured_honest(monkeypatch) -> None:
    """The connection health service keeps crm honest when the native
    OAuth client is absent (deploy-gated): unconfigured + arc17_pending,
    never connected."""
    from app.core.config import settings
    from app.services.connection_health_service import ConnectionHealthService

    class _Conn:
        id = 1
        connection_type = "crm"
        config_json = None
        credential_ref = None

    svc = ConnectionHealthService(settings)
    res = svc.check_health(_Conn())
    assert res.status == "unconfigured"
    assert res.arc17_pending is True
    assert res.status != "connected"
