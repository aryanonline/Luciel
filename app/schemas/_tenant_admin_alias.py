"""Arc 9.2 PR #98 — HTTP-boundary alias between ``tenant_id`` and ``admin_id``.

Option A collapses ``tenant_id`` into ``admin_id`` at the data layer.
At the HTTP boundary we want a graceful overlap window where callers
(internal services, the widget, the Luciel-Website frontend, external
integrations) can use either key during their own renames.

This module exposes two Pydantic mixins:

  * ``TenantAdminInputAlias`` — for request-body models.  If a payload
    includes ``admin_id`` but not ``tenant_id``, copies the value into
    ``tenant_id`` BEFORE field validation runs (and vice versa).  This
    lets existing ``tenant_id: str`` fields keep working while callers
    migrate.

  * ``TenantAdminOutputAlias`` — for response models.  After model
    construction, mirrors the populated key into the other so the JSON
    response always contains BOTH ``tenant_id`` and ``admin_id`` with
    the same value.

The two mixins are independent.  Read/write models can inherit from
both.  Models that don't declare one of the columns simply no-op on
that side.

Removal plan:
  PR #100 removes ``TenantAdminInputAlias`` (callers have switched).
  PR #101 deletes this module wholesale once the column itself is gone.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, model_validator


class TenantAdminInputAlias(BaseModel):
    """Pydantic mixin: accept either ``tenant_id`` or ``admin_id`` on input."""

    @model_validator(mode="before")
    @classmethod
    def _accept_tenant_admin_alias_in(cls, data: Any) -> Any:
        if isinstance(data, dict):
            admin = data.get("admin_id")
            tenant = data.get("tenant_id")
            if admin is not None and tenant is None:
                data["tenant_id"] = admin
            elif tenant is not None and admin is None:
                data["admin_id"] = tenant
        return data


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
