"""
API Key service.

Handles key generation, hashing, and validation.

Keys follow the format: luc_sk_<random>
The raw key is returned only once at creation.
We store a SHA-256 hash for lookup.
"""

from __future__ import annotations

import hashlib
import logging
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.api_key import ApiKey

logger = logging.getLogger(__name__)

KEY_PREFIX = "luc_sk_"


def generate_raw_key() -> str:
    """Generate a random API key."""
    random_part = secrets.token_urlsafe(32)
    return f"{KEY_PREFIX}{random_part}"


def hash_key(raw_key: str) -> str:
    """Hash a key using SHA-256."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


class ApiKeyService:

    def __init__(self, db: Session) -> None:
        self.db = db

    def create_key(
        self,
        *,
        tenant_id: str,
        domain_id: str | None = None,
        agent_id: str | None = None,
        luciel_instance_id: int | None = None,   # Step 24.5
        display_name: str,
        permissions: list[str] | None = None,
        rate_limit: int = 1000,
        created_by: str | None = None,
        auto_commit: bool = True,
    ) -> tuple[ApiKey, str]:
        """Create a new API key. Returns (ApiKey model, raw_key)."""
        raw_key = generate_raw_key()
        hashed = hash_key(raw_key)

        api_key = ApiKey(
            key_hash=hashed,
            key_prefix=raw_key[:12],
            tenant_id=tenant_id,
            domain_id=domain_id,
            agent_id=agent_id,
            luciel_instance_id=luciel_instance_id,   # Step 24.5
            display_name=display_name,
            permissions=permissions or ["chat", "sessions"],
            rate_limit=rate_limit,
            active=True,
            created_by=created_by,
        )
        self.db.add(api_key)
        if auto_commit:                    # ADD THIS
            self.db.commit()
            self.db.refresh(api_key)
        else:                              # ADD THIS
            self.db.flush()                # ADD THIS

        logger.info("Created API key for tenant %s (prefix: %s)", tenant_id, api_key.key_prefix)
        return api_key, raw_key

    def validate_key(self, raw_key: str) -> ApiKey | None:
        """
        Validate a raw API key and return the matching record.
        Returns None if the key is invalid or inactive.
        """
        key_hash = hash_key(raw_key)
        stmt = select(ApiKey).where(
            ApiKey.key_hash == key_hash,
            ApiKey.active.is_(True),
        )
        return self.db.scalars(stmt).first()

    def list_keys(self, tenant_id: str | None = None) -> list[ApiKey]:
        """List API keys, optionally filtered by tenant."""
        stmt = select(ApiKey).order_by(ApiKey.created_at.desc())
        if tenant_id:
            stmt = stmt.where(ApiKey.tenant_id == tenant_id)
        return list(self.db.scalars(stmt).all())

    def deactivate_key(self, key_id: int) -> bool:
        """Deactivate an API key."""
        api_key = self.db.get(ApiKey, key_id)
        if not api_key:
            return False
        api_key.active = False
        self.db.commit()
        logger.info("Deactivated API key id=%d", key_id)
        return True

    def get_key_by_id(self, key_id: int) -> ApiKey | None:
        return self.db.query(ApiKey).filter(ApiKey.id == key_id).first()