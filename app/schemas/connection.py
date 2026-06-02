"""Arc 15 WU4 — connection-config admin API schemas (Arc 17 slice).

Request/response shapes for ``/admin/instances/{id}/connections``. The
contracts mirror the real Arc 17 ``instance_connections`` table
(Architecture §3.8.2), narrowed in surface for this slice.

Honesty invariants enforced here + at the route:
* ``config_json`` carries NON-SECRET config ONLY (CSV store ref, webhook
  URL, sender address, allowlisted domain). Secrets NEVER land in a
  request body field that writes ``config_json``.
* A row is only ever serialized with ``status == 'connected'`` when it
  has a real backing (CSV / webhook in this slice). Deferred connectors
  (calendar / crm) round-trip as ``unconfigured`` with an
  ``arc17_pending`` marker on the create response.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from app.models.instance_connection import (
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
    "property_source",
    "outbound_webhook",
]
ConnectionStatus = Literal[
    "unconfigured",
    "connected",
    "error",
    "expired",
]

# Connectors that connect LIVE in this slice (real backing exists) vs
# connectors that are DEFERRED to full Arc 17 (land unconfigured). The
# route consults these sets; they are the single source of truth for the
# "no fake connected" honesty invariant.
LIVE_CONNECTION_TYPES: frozenset[str] = frozenset(
    {"property_source", "outbound_webhook"}
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

    ``config_json`` holds NON-SECRET config only. The route validates the
    per-type required keys (e.g. CSV needs a store reference; webhook
    needs a URL) and refuses anything that looks like a secret landing in
    ``config_json``.
    """

    connection_type: ConnectionType
    provider: str = Field(..., min_length=1, max_length=64)
    config_json: Optional[dict[str, Any]] = None


class ConnectionView(BaseModel):
    """One connection row in a list/create response."""

    id: int
    instance_id: int
    admin_id: str
    connection_type: ConnectionType
    provider: str
    status: ConnectionStatus
    config_json: Optional[dict[str, Any]]
    last_verified_at: Optional[datetime]
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


class ConnectionDeleteResponse(BaseModel):
    """DELETE response — soft-delete acknowledgement."""

    instance_id: int
    connection_id: int
    disconnected: bool


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
    "ConnectionDeleteResponse",
    "CONNECTION_TYPES",
    "CONNECTION_STATUSES",
]
