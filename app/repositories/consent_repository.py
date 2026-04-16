"""
Consent repository — persistence layer for user consent records.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.user_consent import UserConsent

logger = logging.getLogger(__name__)


class ConsentRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_consent(
        self,
        *,
        user_id: str,
        tenant_id: str,
        consent_type: str = "memory_persistence",
    ) -> UserConsent | None:
        return (
            self.db.query(UserConsent)
            .filter(
                UserConsent.user_id == user_id,
                UserConsent.tenant_id == tenant_id,
                UserConsent.consent_type == consent_type,
            )
            .first()
        )

    def grant_consent(
        self,
        *,
        user_id: str,
        tenant_id: str,
        consent_type: str = "memory_persistence",
        collection_method: str = "api",
        consent_text: str | None = None,
        consent_context: str | None = None,
    ) -> UserConsent:
        existing = self.get_consent(
            user_id=user_id,
            tenant_id=tenant_id,
            consent_type=consent_type,
        )
        if existing:
            existing.granted = True
            existing.collection_method = collection_method
            existing.consent_text = consent_text
            existing.consent_context = consent_context
            self.db.commit()
            self.db.refresh(existing)
            return existing

        record = UserConsent(
            user_id=user_id,
            tenant_id=tenant_id,
            consent_type=consent_type,
            granted=True,
            collection_method=collection_method,
            consent_text=consent_text,
            consent_context=consent_context,
        )
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def withdraw_consent(
        self,
        *,
        user_id: str,
        tenant_id: str,
        consent_type: str = "memory_persistence",
    ) -> UserConsent | None:
        existing = self.get_consent(
            user_id=user_id,
            tenant_id=tenant_id,
            consent_type=consent_type,
        )
        if existing:
            existing.granted = False
            self.db.commit()
            self.db.refresh(existing)
        return existing

    def has_consent(
        self,
        *,
        user_id: str,
        tenant_id: str,
        consent_type: str = "memory_persistence",
    ) -> bool:
        record = self.get_consent(
            user_id=user_id,
            tenant_id=tenant_id,
            consent_type=consent_type,
        )
        return record is not None and record.granted