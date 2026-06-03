"""SecretStore ABC — the value-side of a connection's credential.

The contract is intentionally tiny. A store maps an opaque ``ref``
(the string persisted in ``instance_connections.credential_ref``) to a
secret value. The ref is the ONLY thing that ever touches Postgres; the
value never does.

``put`` returns the ref the caller must persist. ``get`` resolves a ref
back to its value (used by the token-refresh worker to read the stored
refresh token). ``delete`` removes the secret (lifecycle cascade /
secret cleanup). ``rotate`` overwrites the value behind an existing ref
and returns the (possibly unchanged) ref — used when a silent token
refresh yields a new refresh token.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class SecretStoreError(RuntimeError):
    """Raised when a store operation fails (missing ref, backend error).

    Callers translate this into an honest connection ``status`` — never
    a silent success.
    """


class SecretStore(ABC):
    """Abstract value store for connection credentials."""

    @abstractmethod
    def put(self, name: str, value: str) -> str:
        """Store ``value`` under a logical ``name``; return the ref to
        persist in ``credential_ref``. The ref MAY equal ``name`` (fake)
        or be a fully-qualified ARN (AWS)."""

    @abstractmethod
    def get(self, ref: str) -> str:
        """Resolve a ref to its secret value. Raises
        ``SecretStoreError`` if the ref is unknown."""

    @abstractmethod
    def delete(self, ref: str) -> None:
        """Delete the secret behind ``ref``. Idempotent: deleting an
        already-absent ref is not an error."""

    @abstractmethod
    def rotate(self, ref: str, value: str) -> str:
        """Overwrite the value behind an existing ``ref``; return the
        ref (unchanged for both shipped stores). Raises
        ``SecretStoreError`` if the ref is unknown."""
