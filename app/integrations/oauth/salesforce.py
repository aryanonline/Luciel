"""SalesforceOAuthProvider — native Salesforce CRM OAuth (Arc 17).

Mirrors :class:`GoogleCalendarOAuthProvider` exactly — the same three
real OAuth moments over Salesforce's OAuth 2.0 web-server-flow endpoints:
  * authorization_url  → <login_base>/services/oauth2/authorize consent.
  * exchange_code      → POST <login_base>/services/oauth2/token (code).
  * refresh            → POST <login_base>/services/oauth2/token
                         (grant_type=refresh_token).

The ``login_base`` host is configurable (production login domain by
default; a sandbox org points it at https://test.salesforce.com or its
My Domain) so the same provider serves prod and sandbox connected apps
without a code change.

DEPLOY-GATED: the network calls require ``salesforce_oauth_client_id`` +
``salesforce_oauth_client_secret`` to be populated (prod SSM). When
either is empty ``is_configured()`` returns False and the token methods
raise ``OAuthNotConfiguredError`` BEFORE any network call — so a session
with no Salesforce creds (dev / CI / test) never reaches the wire and the
crm connector stays an honest ``unconfigured`` + arc17_pending. The code
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

_AUTHORIZE_PATH = "/services/oauth2/authorize"
_TOKEN_PATH = "/services/oauth2/token"
_HTTP_TIMEOUT = 10.0


class SalesforceOAuthProvider(OAuthProvider):
    """Salesforce CRM OAuth 2.0 web-server-flow provider."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        login_base: str = "https://login.salesforce.com",
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        # Trim a trailing slash so path concatenation is unambiguous.
        self._login_base = (login_base or "https://login.salesforce.com").rstrip("/")

    @property
    def connection_type(self) -> str:
        return "crm"

    def is_configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def _require_configured(self) -> None:
        if not self.is_configured():
            raise OAuthNotConfiguredError(
                "Salesforce OAuth client credentials are absent; crm "
                "connector stays unconfigured (arc17_pending)."
            )

    def authorization_url(self, *, state: str) -> str:
        self._require_configured()
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "state": state,
        }
        return f"{self._login_base}{_AUTHORIZE_PATH}?{urlencode(params)}"

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
        # DEPLOY-GATED: live POST to Salesforce's token endpoint. Reached
        # only when is_configured() is True (real client creds present).
        endpoint = f"{self._login_base}{_TOKEN_PATH}"
        try:
            resp = httpx.post(endpoint, data=payload, timeout=_HTTP_TIMEOUT)
        except httpx.HTTPError as exc:  # pragma: no cover - DEPLOY-GATED
            raise OAuthError(f"Salesforce token request failed: {exc}") from exc
        if resp.status_code != 200:  # pragma: no cover - DEPLOY-GATED
            raise OAuthError(
                f"Salesforce token endpoint returned {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        body = resp.json()
        access_token = body.get("access_token")
        if not access_token:  # pragma: no cover - DEPLOY-GATED
            raise OAuthError("Salesforce token response had no access_token")
        return OAuthTokens(
            access_token=access_token,
            refresh_token=body.get("refresh_token"),
            # Salesforce omits expires_in on some flows; default 0 (caller
            # treats 0 as "unknown / refresh on next use").
            expires_in=int(body.get("expires_in", 0)),
            scope=body.get("scope", ""),
            token_type=body.get("token_type", "Bearer"),
        )
