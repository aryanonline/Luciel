"""HubSpotOAuthProvider — native HubSpot CRM OAuth (Arc 17 connectors).

Mirrors :class:`GoogleCalendarOAuthProvider` exactly — the same three
real OAuth moments (authorization URL, code→token exchange, refresh) over
HubSpot's OAuth 2.0 endpoints:
  * authorization_url  → app.hubspot.com/oauth/authorize consent URL.
  * exchange_code      → POST api.hubapi.com/oauth/v1/token (auth code).
  * refresh            → POST api.hubapi.com/oauth/v1/token
                         (grant_type=refresh_token).

DEPLOY-GATED: the network calls require ``hubspot_oauth_client_id`` +
``hubspot_oauth_client_secret`` to be populated (prod SSM). When either
is empty ``is_configured()`` returns False and the token methods raise
``OAuthNotConfiguredError`` BEFORE any network call — so a session with
no HubSpot creds (dev / CI / test) never reaches the wire and the crm
connector stays an honest ``unconfigured`` + arc17_pending. The code
path is identical in prod; only the credential presence differs.
"""
from __future__ import annotations

from urllib.parse import urlencode

import httpx

from app.integrations.oauth.base import (
    OAuthError,
    OAuthNotConfiguredError,
    OAuthProvider,
    OAuthTokens,
)

_AUTH_ENDPOINT = "https://app.hubspot.com/oauth/authorize"
_TOKEN_ENDPOINT = "https://api.hubapi.com/oauth/v1/token"
# CRM read/write scopes for lead/contact push; HubSpot space-delimits.
_CRM_SCOPE = "crm.objects.contacts.write crm.objects.contacts.read"
_HTTP_TIMEOUT = 10.0


class HubSpotOAuthProvider(OAuthProvider):
    """HubSpot CRM OAuth 2.0 web-server-flow provider."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

    @property
    def connection_type(self) -> str:
        return "crm"

    def is_configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def _require_configured(self) -> None:
        if not self.is_configured():
            raise OAuthNotConfiguredError(
                "HubSpot OAuth client credentials are absent; crm "
                "connector stays unconfigured (arc17_pending)."
            )

    def authorization_url(self, *, state: str) -> str:
        self._require_configured()
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "scope": _CRM_SCOPE,
            "state": state,
        }
        return f"{_AUTH_ENDPOINT}?{urlencode(params)}"

    def exchange_code(self, *, code: str) -> OAuthTokens:
        self._require_configured()
        payload = {
            "code": code,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "redirect_uri": self._redirect_uri,
            "grant_type": "authorization_code",
        }
        return self._token_request(payload)

    def refresh(self, *, refresh_token: str) -> OAuthTokens:
        self._require_configured()
        payload = {
            "refresh_token": refresh_token,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "refresh_token",
        }
        return self._token_request(payload)

    def _token_request(self, payload: dict) -> OAuthTokens:
        # DEPLOY-GATED: live POST to HubSpot's token endpoint. Reached only
        # when is_configured() is True (real client creds present).
        try:
            resp = httpx.post(
                _TOKEN_ENDPOINT, data=payload, timeout=_HTTP_TIMEOUT
            )
        except httpx.HTTPError as exc:  # pragma: no cover - DEPLOY-GATED
            raise OAuthError(f"HubSpot token request failed: {exc}") from exc
        if resp.status_code != 200:  # pragma: no cover - DEPLOY-GATED
            raise OAuthError(
                f"HubSpot token endpoint returned {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        body = resp.json()
        access_token = body.get("access_token")
        if not access_token:  # pragma: no cover - DEPLOY-GATED
            raise OAuthError("HubSpot token response had no access_token")
        return OAuthTokens(
            access_token=access_token,
            refresh_token=body.get("refresh_token"),
            expires_in=int(body.get("expires_in", 0)),
            scope=body.get("scope", ""),
            token_type=body.get("token_type", "Bearer"),
        )
