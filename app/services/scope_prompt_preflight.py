"""
Scope-prompt preflight for embed-key issuance.

Step 30d, Deliverable A — Issuance-time guardrail.

When an admin (or the mint_embed_key CLI) requests a new widget embed key
scoped to a specific (tenant_id, domain_id), this preflight verifies that
the target `domain_configs` row exists AND has a non-empty
`system_prompt_additions` value.

Rationale
---------
The widget surface (ARCHITECTURE §3.2.2) intentionally ships *only* a
domain-scoped system prompt to the LLM. If `system_prompt_additions` is
NULL or whitespace, the widget will answer with an unconstrained base
persona — which violates the per-tenant scoping contract publicly visible
on the customer's site.

We block the mint at issuance time (HTTP 422 / CLI exit 2) rather than at
chat time, so that:
  * Existing widget keys are not retroactively bricked (matters for the
    staging widget today, and for any customer mid-onboarding).
  * The audit trail clearly records *who* attempted to mint an
    unscoped key and *when*.
  * Operators get the failure during a controlled admin action, not from
    a stranger's browser on a customer site.

Tenant-wide mints
-----------------
Tenant-wide mints (`domain_id is None`) intentionally skip this preflight.
A tenant-wide key is governed by `TenantConfig.system_prompt` at chat
time and may legitimately exist before any per-domain config is created.
The caller is expected to surface a non-fatal warning in this case
(see admin route).

This module raises `ScopePromptMissingError` on failure. Callers translate
that into their surface's native failure mode (HTTPException, CLI exit,
audit script row).
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.models.domain_config import DomainConfig


class ScopePromptMissingError(Exception):
    """Raised when a per-domain embed-key mint is requested but the target
    domain_configs row is missing or has an empty system_prompt_additions.

    Attributes
    ----------
    reason : str
        One of {"missing_domain_config", "empty_system_prompt"}.
        Used by callers to render distinct user-facing messages.
    tenant_id : str
        The tenant_id that was being minted for.
    domain_id : str
        The domain_id that was being minted for.
    """

    def __init__(self, reason: str, tenant_id: str, domain_id: str, message: str):
        self.reason = reason
        self.tenant_id = tenant_id
        self.domain_id = domain_id
        super().__init__(message)


class ScopePromptPreflight:
    """Verify scope-prompt readiness before issuing a widget embed key."""

    @staticmethod
    def check(
        db: Session,
        tenant_id: str,
        domain_id: Optional[str],
    ) -> None:
        """Validate that the target domain has a non-empty scope prompt.

        Parameters
        ----------
        db : Session
            Active SQLAlchemy session. The preflight performs read-only
            SELECTs only.
        tenant_id : str
            Tenant the key is being minted for.
        domain_id : Optional[str]
            Domain the key will be scoped to. If `None`, this is a
            tenant-wide mint and the preflight returns immediately
            (caller should warn but not block).

        Raises
        ------
        ScopePromptMissingError
            With `reason="missing_domain_config"` if no row exists for
            (tenant_id, domain_id), or `reason="empty_system_prompt"` if
            the row exists but `system_prompt_additions` is NULL / empty
            / whitespace-only.
        """

        # Tenant-wide mints are governed by TenantConfig.system_prompt,
        # not by domain_configs. Skip the preflight in that case.
        if domain_id is None:
            return

        row: Optional[DomainConfig] = (
            db.query(DomainConfig)
            .filter(
                DomainConfig.tenant_id == tenant_id,
                DomainConfig.domain_id == domain_id,
            )
            .one_or_none()
        )

        if row is None:
            raise ScopePromptMissingError(
                reason="missing_domain_config",
                tenant_id=tenant_id,
                domain_id=domain_id,
                message=(
                    f"Cannot mint widget embed key: no domain_configs row "
                    f"for tenant_id={tenant_id!r}, domain_id={domain_id!r}. "
                    f"Create the domain config (with a non-empty "
                    f"system_prompt_additions) before issuing a widget key."
                ),
            )

        prompt = row.system_prompt_additions
        if prompt is None or not prompt.strip():
            raise ScopePromptMissingError(
                reason="empty_system_prompt",
                tenant_id=tenant_id,
                domain_id=domain_id,
                message=(
                    f"Cannot mint widget embed key: domain_configs row "
                    f"for tenant_id={tenant_id!r}, domain_id={domain_id!r} "
                    f"has an empty system_prompt_additions. Set a "
                    f"non-empty scope prompt before issuing a widget key "
                    f"(see ARCHITECTURE §3.2.2 'Issuance')."
                ),
            )
