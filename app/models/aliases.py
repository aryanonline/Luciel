"""Aggressive-cleanup compatibility shim (Arc 5 B1 → B6).

Per ``D-arc5-aggressive-cleanup-doctrine-amendment-2026-05-23``: B1
deletes the legacy ``Tenant`` / ``TenantConfig`` / ``LucielInstance`` /
``Agent`` / ``DomainConfig`` ORM classes outright and routes any
remaining transitional call-site through this shim.

Mappings
--------
* ``Tenant``, ``TenantConfig`` → :class:`app.models.admin.Admin`
* ``LucielInstance`` → :class:`app.models.instance.Instance`
* ``DomainConfig``, ``Agent`` → access raises :class:`ImportError`
  because the Domain + Agent layers do not exist in the V2 doctrine.

Lifecycle
---------
This module is **deleted at B6** once all call-sites in
``app/`` / ``tests/`` / ``scripts/`` have been migrated to the V2 names.
Any new code authored after B6 must import directly from
``app.models.admin`` and ``app.models.instance``.
"""

from __future__ import annotations

from app.models.admin import Admin, AdminConfig
from app.models.instance import Instance


Tenant = Admin
TenantConfig = AdminConfig
LucielInstance = Instance

# Legacy scope-level constants from the removed ``LucielInstance`` model.
# V2 has no Domain/Agent layers — these literals are kept as exact-string
# compatibility for call-sites that still reference them pending B2-B5
# rewrite. The values match the pre-B1 module so any persisted strings
# in fixtures or audit rows still compare equal.
SCOPE_LEVEL_TENANT = "tenant"
SCOPE_LEVEL_DOMAIN = "domain"
SCOPE_LEVEL_AGENT = "agent"
ALLOWED_SCOPE_LEVELS = (SCOPE_LEVEL_TENANT, SCOPE_LEVEL_DOMAIN, SCOPE_LEVEL_AGENT)


class _RemovedV1Class:
    """Sentinel raised when call-sites import a V1-only class.

    Domain and Agent layers were collapsed into Admin → Instance per
    CANONICAL_RECAP §11 Q1. Importers must migrate to ``Admin`` /
    ``Instance`` or have their feature deleted outright.
    """

    def __init__(self, name: str) -> None:
        self._name = name

    def __call__(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise ImportError(
            f"{self._name} was removed in Arc 5 B1 (aggressive-cleanup amendment). "
            f"The Domain and Agent layers no longer exist in the V2 Admin → "
            f"Instance → Lead doctrine. Migrate the caller to "
            f"app.models.admin.Admin or app.models.instance.Instance."
        )

    def __getattr__(self, attr: str):
        raise ImportError(
            f"{self._name} was removed in Arc 5 B1 (aggressive-cleanup "
            f"amendment); cannot access attribute {attr!r}."
        )


DomainConfig = _RemovedV1Class("DomainConfig")
Agent = _RemovedV1Class("Agent")


__all__ = [
    "Tenant",
    "TenantConfig",
    "LucielInstance",
    "DomainConfig",
    "Agent",
]
