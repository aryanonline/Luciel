"""Arc 15 WU4 — connection-config admin API schemas (Arc 17 slice).

Request/response shapes for ``/admin/instances/{id}/connections``. The
contracts mirror the real Arc 17 ``instance_connections`` table
(Architecture §3.8.2), narrowed in surface for this slice.

Honesty invariants enforced here + at the route:
* ``non_secret_config`` carries NON-SECRET config ONLY (CSV store ref, webhook
  URL, sender address, allowlisted domain). Secrets NEVER land in a
  request body field that writes ``non_secret_config``.
* A row is only ever serialized with ``status == 'connected'`` when it
  has a real backing. When the live provider credential / OAuth consent
  is absent the row round-trips as ``unconfigured`` with an
  ``arc17_pending`` (deploy-gated-pending) marker on the create response —
  never a fake ``connected``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from app.connections.instance_connection import (
    CONNECTION_STATUSES,
    CONNECTION_TYPES,
)

# Re-export the closed vocabularies as typing Literals so the API layer
# rejects out-of-vocabulary input at the Pydantic boundary (422) before
# the route ever runs.
ConnectionType = Literal[
    "calendar",
    "email_sender",
    "sms_sender",
    "crm",
    "record_source",
    "outbound_webhook",
]
ConnectionStatus = Literal[
    "unconfigured",
    "connected",
    "error",
    "expired",
]

# Connectors whose connect path completes from a config-presence check
# alone (real backing already present: CSV store ref / webhook URL) vs
# connectors whose live connect is DEPLOY-GATED on a provider credential /
# OAuth consent and so land ``unconfigured`` until that lands. The two
# sets partition the vocabulary and back the "no fake connected" honesty
# invariant. (Names retained for API/test stability; "DEFERRED" here means
# deploy-gated, not unbuilt.)
LIVE_CONNECTION_TYPES: frozenset[str] = frozenset(
    {"record_source", "outbound_webhook"}
)
DEFERRED_CONNECTION_TYPES: frozenset[str] = frozenset(
    {"calendar", "crm", "email_sender", "sms_sender"}
)

# Sanity: the two partitions cover the full vocabulary exactly once.
assert LIVE_CONNECTION_TYPES | DEFERRED_CONNECTION_TYPES == set(
    CONNECTION_TYPES
)
assert not (LIVE_CONNECTION_TYPES & DEFERRED_CONNECTION_TYPES)


class ConnectionCreate(BaseModel):
    """POST body — configure a connection for an instance.

    ``non_secret_config`` holds NON-SECRET config only. The route validates the
    per-type required keys (e.g. CSV needs a store reference; webhook
    needs a URL) and refuses anything that looks like a secret landing in
    ``non_secret_config``.
    """

    connection_type: ConnectionType
    provider: str = Field(..., min_length=1, max_length=64)
    non_secret_config: Optional[dict[str, Any]] = None


class ConnectionView(BaseModel):
    """One connection row in a list/create response."""

    id: int
    instance_id: int
    admin_id: str
    connection_type: ConnectionType
    provider: str
    status: ConnectionStatus
    # §3.8.5 credential-shape class driving the health/refresh worker.
    auth_class: str
    non_secret_config: Optional[dict[str, Any]]
    last_health_check_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class ConnectionListResponse(BaseModel):
    """GET response — the instance's live connection rows."""

    instance_id: int
    admin_id: str
    connections: list[ConnectionView]


class Arc17Pending(BaseModel):
    """Structured marker returned for a DEFERRED connector configure.

    Honest: the row is created in ``unconfigured`` status (never a fake
    ``connected``). The UI renders the message; the WU5 dispatch gate
    keeps refusing the dependent tool until full Arc 17 supplies a real
    backing.
    """

    deferred: bool = True
    connection_type: ConnectionType
    available_in: str = "arc17"
    message: str


class ConnectionCreateResponse(BaseModel):
    """POST response — the created row plus an optional deferral marker."""

    connection: ConnectionView
    arc17_pending: Optional[Arc17Pending] = None


class ConnectionRefreshResponse(BaseModel):
    """POST .../refresh response — the re-verified row plus an optional
    deferral marker.

    The embedded ``connection`` view carries the HONEST post-check
    ``status`` and updated ``last_health_check_at``. ``arc17_pending`` is
    populated (and ``status`` stays ``unconfigured``) for a deferred OAuth
    connector whose live flow is deploy-gated — the endpoint NEVER fakes
    ``connected``. ``detail`` is a short human-readable outcome line.
    """

    connection: ConnectionView
    arc17_pending: Optional[Arc17Pending] = None
    detail: str = ""


class ConnectionDeleteResponse(BaseModel):
    """DELETE response — soft-delete acknowledgement."""

    instance_id: int
    connection_id: int
    disconnected: bool
    # Arc 17 — set True when the disconnect enqueued a secret-cleanup
    # outbox row (the revoked row carried a non-null secret_ref). The
    # drain worker performs the actual SecretStore.delete out of band.
    secret_cleanup_enqueued: bool = False


# ---------------------------------------------------------------------
# Arc 17 — OAuth initiate + callback (the consent-flow endpoints).
# ---------------------------------------------------------------------


class OAuthInitiateResponse(BaseModel):
    """POST .../oauth/{connection_type}/initiate response.

    The admin UI opens ``authorization_url`` (the provider consent
    screen). ``state`` is the signed, opaque value round-tripped through
    the provider; the callback authorizes off it. ``connection_id`` is the
    ensured (status='unconfigured') row the consent will complete.
    """

    authorization_url: str
    state: str
    connection_id: int
    connection_type: ConnectionType
    provider: str
    status: ConnectionStatus = "unconfigured"


class ConnectorCatalogEntry(BaseModel):
    """One supported connection_type's readiness in the deploy environment.

    ``is_ready`` reports whether this connector CAN connect live in the
    current environment, by auth_class:
      * ``api_key`` — always ready (the backing is per-tenant config the
        tenant supplies at configure time; nothing is deploy-gated).
      * ``provisioned_resource`` — ready iff the platform sender identity
        is present in settings (same check the configure path + send tools
        use).
      * ``oauth_token`` — ready iff the OAuth client credentials are
        configured (same ``provider.is_configured()`` gate the connect
        path uses).
    Carries NO secrets and no per-tenant data — it is a static, read-only
    description of the deploy's connector capability.
    """

    connection_type: ConnectionType
    auth_class: str
    is_ready: bool


class ConnectorCatalogResponse(BaseModel):
    """GET .../connections/catalog response — the readiness catalog."""

    connectors: list[ConnectorCatalogEntry]


__all__ = [
    "ConnectionType",
    "ConnectionStatus",
    "LIVE_CONNECTION_TYPES",
    "DEFERRED_CONNECTION_TYPES",
    "ConnectionCreate",
    "ConnectionView",
    "ConnectionListResponse",
    "Arc17Pending",
    "ConnectionCreateResponse",
    "ConnectionRefreshResponse",
    "ConnectionDeleteResponse",
    "OAuthInitiateResponse",
    "ConnectorCatalogEntry",
    "ConnectorCatalogResponse",
    "CONNECTION_TYPES",
    "CONNECTION_STATUSES",
]
