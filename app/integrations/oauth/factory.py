"""get_oauth_provider — OAuth provider selector by connection_type.

Maps a deferred ``connection_type`` to its provider, reading client
credentials from settings. Google Calendar is the only provider wired
this arc (the reference); the other deferred connectors (crm /
email_sender / sms_sender) return ``None`` until their providers land —
callers treat ``None`` exactly like an unconfigured provider (honest
``unconfigured`` + arc17_pending).

No network, no creds required to call this — it only constructs a
provider object. The provider's ``is_configured()`` is the honesty gate.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from app.integrations.oauth.base import OAuthProvider
from app.integrations.oauth.google_calendar import (
    GoogleCalendarOAuthProvider,
)

if TYPE_CHECKING:  # pragma: no cover
    from app.core.config import Settings


def get_oauth_provider(
    connection_type: str, settings: "Settings"
) -> Optional[OAuthProvider]:
    """Return the OAuth provider for ``connection_type`` or ``None``.

    ``None`` means no provider is wired for that connector yet; the
    caller round-trips an honest ``unconfigured``.
    """
    if connection_type == "calendar":
        return GoogleCalendarOAuthProvider(
            client_id=settings.google_oauth_client_id,
            client_secret=settings.google_oauth_client_secret,
            redirect_uri=settings.google_oauth_redirect_uri,
        )
    return None
