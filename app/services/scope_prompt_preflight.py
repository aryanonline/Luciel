"""
Scope-prompt preflight for embed-key issuance.

Arc 5 Path A (V2 collapse): the Domain layer was eliminated. The
issuance-time guardrail historically verified a domain-scoped prompt
condition before a domain-scoped embed key could be minted.

In V2, widget keys are scoped to an ``Instance`` (under an ``Admin``)
instead of a Domain, so this preflight is no longer the bottleneck —
the issuance routes already validate the target ``admin_id`` /
``instance_slug`` directly.

This module is preserved as a no-op shim so any caller that still
imports ``ScopePromptPreflight`` / ``ScopePromptMissingError``
compiles. The ``check`` method returns immediately. The exception
class is preserved for tests that assert on it.

Step 30d was the original deliverable; Step 30d itself is preserved in
the legacy V1 history. The guardrail was retired at Arc 5 Path A
because the V2 issuance path already gates on Admin + Instance
existence at the route layer.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session


class ScopePromptMissingError(Exception):
    """Retained for legacy tests. V2 callers never raise this.

    Attributes
    ----------
    reason : str
        One of {"missing_domain_config", "empty_system_prompt"}.
    admin_id : str
    domain_id : str
    """

    def __init__(self, reason: str, admin_id: str, domain_id: str, message: str):
        self.reason = reason
        self.admin_id = admin_id
        self.domain_id = domain_id
        super().__init__(message)


class ScopePromptPreflight:
    """V2 no-op preflight. The Domain layer is gone; issuance gating
    moved to the route-layer Admin + Instance existence check.
    """

    @staticmethod
    def check(
        db: Session,
        admin_id: str,
        domain_id: Optional[str],
    ) -> None:
        """V2: always succeeds. Domain layer no longer exists.

        Parameters retained for source compatibility with legacy callers.
        """
        return None
