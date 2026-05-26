"""Arc 9.2 PR #98 — HTTP-boundary alias between ``tenant_id`` and ``admin_id``.

Option A collapses ``tenant_id`` into ``admin_id`` at the data layer.
At the HTTP boundary we want a graceful overlap window where callers
(internal services, the widget, the Luciel-Website frontend, external
integrations) can use either key during their own renames.

This module exposes one Pydantic mixin (post-PR #100):

  * ``TenantAdminOutputAlias`` — for response models.  After model
    construction, mirrors the populated key into the other so the JSON
    response always contains BOTH ``tenant_id`` and ``admin_id`` with
    the same value.

Arc 9.2 PR #100: ``TenantAdminInputAlias`` removed.  All known callers
(frontend dashboard, widget, internal services) emit ``admin_id`` only.

Removal plan:
  PR #101 deletes this module wholesale once the column itself is gone.
"""
from __future__ import annotations

from pydantic import BaseModel, model_validator


class TenantAdminOutputAlias(BaseModel):
    """Pydantic mixin: emit both ``tenant_id`` and ``admin_id`` on output."""

    @model_validator(mode="after")
    def _emit_tenant_admin_alias_out(self) -> "TenantAdminOutputAlias":
        tenant = getattr(self, "tenant_id", None)
        admin = getattr(self, "admin_id", None)
        if tenant is not None and admin is None:
            try:
                object.__setattr__(self, "admin_id", tenant)
            except (AttributeError, TypeError):
                pass
        elif admin is not None and tenant is None:
            try:
                object.__setattr__(self, "tenant_id", admin)
            except (AttributeError, TypeError):
                pass
        return self
