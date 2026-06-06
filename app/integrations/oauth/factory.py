"""get_oauth_provider — OAuth provider selector by connection_type.

Maps an OAuth-backed ``connection_type`` to its provider, reading client
credentials from settings. Google Calendar is the reference provider;
the native CRM connectors (HubSpot + Salesforce) follow the SAME shape.
``email_sender`` / ``sms_sender`` are NOT OAuth — they authenticate via
their channel transport (SES / Twilio), so they have no OAuth provider
here and the factory returns ``None`` for them. Callers treat ``None``
exactly like an unconfigured provider (honest ``unconfigured`` +
arc17_pending).

CRM provider selection (native, no owning arc — built per founder
instruction, DOC GAP flagged in the Arc 17 connectors RESULT): ``crm``
maps to HubSpot when its client creds are present, else Salesforce. When
NEITHER CRM credential set is configured the factory returns an
(unconfigured) Salesforce provider so ``is_configured()`` is False and
the connector round-trips an honest ``unconfigured`` — the custom-webhook
CRM path (Arc 12 WU6) is unaffected and stays live.

No network, no creds required to call this — it only constructs a
provider object. The provider's ``is_configured()`` is the honesty gate.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from app.integrations.oauth.base import OAuthProvider
from app.integrations.oauth.google_calendar import (
    GoogleCalendarOAuthProvider,
)
from app.integrations.oauth.hubspot import HubSpotOAuthProvider
from app.integrations.oauth.salesforce import SalesforceOAuthProvider

if TYPE_CHECKING:  # pragma: no cover
    from app.core.config import Settings


def get_oauth_provider(
    connection_type: str, settings: "Settings"
) -> Optional[OAuthProvider]:
    """Return the OAuth provider for ``connection_type`` or ``None``.

    ``None`` means no OAuth provider backs that connector (e.g.
    email_sender / sms_sender authenticate via their channel transport);
    the caller round-trips an honest ``unconfigured``.
    """
    if connection_type == "calendar":
        return GoogleCalendarOAuthProvider(
            client_id=settings.google_oauth_client_id,
            client_secret=settings.google_oauth_client_secret,
            redirect_uri=settings.google_oauth_redirect_uri,
        )
    if connection_type == "crm":
        # Prefer HubSpot when its creds are present; otherwise Salesforce.
        # When neither is configured the Salesforce provider's
        # is_configured() is False → honest unconfigured (no network).
        hubspot = HubSpotOAuthProvider(
            client_id=settings.hubspot_oauth_client_id,
            client_secret=settings.hubspot_oauth_client_secret,
            redirect_uri=settings.hubspot_oauth_redirect_uri,
        )
        if hubspot.is_configured():
            return hubspot
        return SalesforceOAuthProvider(
            client_id=settings.salesforce_oauth_client_id,
            client_secret=settings.salesforce_oauth_client_secret,
            redirect_uri=settings.salesforce_oauth_redirect_uri,
            login_base=settings.salesforce_oauth_login_base,
        )
    return None
