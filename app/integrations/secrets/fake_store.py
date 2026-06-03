"""LocalFakeSecretStore — in-memory secret store for dev / CI / tests.

A plain dict keyed by ref. No persistence, no network, no AWS. This is
the store ``get_secret_store`` returns whenever
``connections_live_secrets_enabled`` is False (the boot-safe default),
so the entire connection code path — configure, refresh, token-refresh
worker, lifecycle cleanup — runs end to end with NO AWS dependency.

The ref scheme mirrors the AWS naming convention
(``luciel/connections/{name}``) so a test asserts the SAME ref shape it
would see in production, and so a grep test can prove the ref is a
pointer string and never the value.
"""
from __future__ import annotations

import threading

from app.integrations.secrets.base import SecretStore, SecretStoreError

_REF_PREFIX = "luciel/connections/"


class LocalFakeSecretStore(SecretStore):
    """Thread-safe in-memory ``SecretStore``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, str] = {}

    @staticmethod
    def _ref_for(name: str) -> str:
        return name if name.startswith(_REF_PREFIX) else f"{_REF_PREFIX}{name}"

    def put(self, name: str, value: str) -> str:
        ref = self._ref_for(name)
        with self._lock:
            self._store[ref] = value
        return ref

    def get(self, ref: str) -> str:
        with self._lock:
            if ref not in self._store:
                raise SecretStoreError(f"unknown secret ref: {ref!r}")
            return self._store[ref]

    def delete(self, ref: str) -> None:
        with self._lock:
            self._store.pop(ref, None)

    def rotate(self, ref: str, value: str) -> str:
        with self._lock:
            if ref not in self._store:
                raise SecretStoreError(f"unknown secret ref: {ref!r}")
            self._store[ref] = value
        return ref
