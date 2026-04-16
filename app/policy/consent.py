"""
Consent policy — decides whether memory operations are allowed
based on user consent state.

This is the gatekeeper: if consent is not granted,
memory persistence is silently skipped (chat still works).
"""
from __future__ import annotations

import logging

from app.repositories.consent_repository import ConsentRepository

logger = logging.getLogger(__name__)


class ConsentPolicy:
    def __init__(self, consent_repository: ConsentRepository) -> None:
        self.consent_repository = consent_repository

    def can_persist_memory(
        self,
        *,
        user_id: str,
        tenant_id: str,
    ) -> bool:
        """
        Returns True if the user has granted memory_persistence consent.
        Returns False otherwise — memory extraction should be skipped.
        """
        allowed = self.consent_repository.has_consent(
            user_id=user_id,
            tenant_id=tenant_id,
            consent_type="memory_persistence",
        )
        if not allowed:
            logger.debug(
                "Memory persistence blocked: no consent for user=%s tenant=%s",
                user_id,
                tenant_id,
            )
        return allowed