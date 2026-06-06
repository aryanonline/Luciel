"""Connections Layer (Architecture §3.8).

Per-instance external-system connections: the ``InstanceConnection``
model + repository and the secret-cleanup transactional outbox model +
repository. Relocated here from ``app.models`` / ``app.repositories``
(Unit 12 §8 doctrine-path normalization) so the Connections Layer is a
cohesive feature package rather than scattered across the model/repo
layering.

The connection ORM models register on ``Base.metadata`` as a side-effect
of their module executing; ``app.models.__init__`` imports those
submodules (bare ``import``, not ``from … import name``) so Alembic
autogenerate via ``import app.models`` still discovers the connection
tables. The package-level symbols are re-exported lazily (PEP 562
``__getattr__``) so a cold direct import of a connection submodule cannot
deadlock on the ``app.models`` ⇄ ``app.connections`` import cycle.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - type-checkers only
    from app.connections.instance_connection import (  # noqa: F401
        CONNECTION_STATUSES,
        CONNECTION_TYPES,
        InstanceConnection,
    )
    from app.connections.secret_cleanup_outbox import (  # noqa: F401
        OUTBOX_STATUSES,
        SecretCleanupOutbox,
    )

_INSTANCE_CONNECTION_EXPORTS = {
    "InstanceConnection",
    "CONNECTION_TYPES",
    "CONNECTION_STATUSES",
}
_OUTBOX_EXPORTS = {"SecretCleanupOutbox", "OUTBOX_STATUSES"}

__all__ = sorted(_INSTANCE_CONNECTION_EXPORTS | _OUTBOX_EXPORTS)


def __getattr__(name: str):
    if name in _INSTANCE_CONNECTION_EXPORTS:
        from app.connections import instance_connection

        return getattr(instance_connection, name)
    if name in _OUTBOX_EXPORTS:
        from app.connections import secret_cleanup_outbox

        return getattr(secret_cleanup_outbox, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
