"""
API Key service.

Handles key generation, hashing, and validation.

Keys follow the format: luc_sk_<random>
The raw key is returned only once at creation.
We store a SHA-256 hash for lookup.

Step 27a additions:
- `create_key(..., ssm_write=False)` — when True, writes the raw key to
  AWS SSM Parameter Store as SecureString at
  /luciel/bootstrap/admin_key_<id> and returns (api_key, None) instead
  of exposing the raw value. Closes the CloudWatch-exposure surface
  identified in 26b Phase 7.5 bootstrap.
- tenant_id type corrected to `str | None` — aligns with 26b.1 DB
  migration 3447ac8b45b4 (nullable) and ApiKeyCreate schema. Required
  for platform-admin keys with tenant_id=NULL per Invariant 5.
- boto3 is lazy-imported inside the ssm_write branch; dev/test paths
  that don't set ssm_write=True do not require boto3 installation.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.api_key import ApiKey

logger = logging.getLogger(__name__)

KEY_PREFIX = "luc_sk_"

# Step 27a: SSM parameter path template for bootstrap admin keys.
# Path is <ns>/admin_key_<id> so multiple bootstraps don't collide and each
# parameter is independently deletable/rotatable.
SSM_BOOTSTRAP_PATH = "/luciel/bootstrap/admin_key_{key_id}"
SSM_DEFAULT_REGION = "ca-central-1"


def generate_raw_key() -> str:
    """Generate a random API key."""
    random_part = secrets.token_urlsafe(32)
    return f"{KEY_PREFIX}{random_part}"


def hash_key(raw_key: str) -> str:
    """Hash a key using SHA-256."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _write_key_to_ssm(*, key_id: int, raw_key: str, region: str,  ssm_path: str | None = None,) -> str:
    """
    Step 27a: Write a freshly-minted raw key to AWS SSM Parameter Store
    as SecureString. Returns the parameter path on success. Raises on
    failure — caller decides whether to roll back the DB insert.

    Step 27c-final: when ssm_path is provided, it is used verbatim (no
    .format() substitution). This supports durable production paths like
    /luciel/production/platform-admin-key that should NOT carry the
    key_id in the URL (the production path is stable across re-mints).
    When ssm_path is None (default), behavior is identical to 27a:
    SSM_BOOTSTRAP_PATH.format(key_id=key_id) is used.

    boto3 is imported lazily so dev/test paths that never hit this branch
    do not need boto3 installed.
    """
    import boto3  # lazy import — keeps dev paths dependency-free

    path = ssm_path if ssm_path is not None else SSM_BOOTSTRAP_PATH.format(key_id=key_id)
    ssm = boto3.client("ssm", region_name=region)
    ssm.put_parameter(
        Name=path,
        Value=raw_key,
        Type="SecureString",
        Overwrite=False,  # refuse to clobber; caller deletes stale params first
        Description=(
            f"Luciel platform-admin key id={key_id} at {path}. "
            f"Read by operator or task role; managed via SSM."
        ),
        Tags=[
            {"Key": "luciel:purpose", "Value": (
                "platform-admin-key" if ssm_path is not None else "bootstrap-admin-key"
            )},
            {"Key": "luciel:key_id", "Value": str(key_id)},
        ],
    )
    logger.info(
        "SSM bootstrap key written: path=%s key_id=%d region=%s",
        path, key_id, region,
    )
    return path


class ApiKeyService:

    def __init__(self, db: Session) -> None:
        self.db = db

    def create_key(
        self,
        *,
        tenant_id: str | None,                      # Step 27a: was `str`
        domain_id: str | None = None,
        agent_id: str | None = None,
        luciel_instance_id: int | None = None,      # Step 24.5
        display_name: str,
        permissions: list[str] | None = None,
        rate_limit: int = 1000,
        created_by: str | None = None,
        auto_commit: bool = True,
        ssm_write: bool = False,                    # Step 27a
        ssm_region: str | None = None,              # Step 27a
        ssm_path: str | None = None,
    ) -> tuple[ApiKey, str | None]:
        """
        Create a new API key.

        Returns (ApiKey model, raw_key_or_None).

        Step 27a: when ssm_write=True, the raw key is written to AWS SSM
        Parameter Store at /luciel/bootstrap/admin_key_<id> as a
        SecureString, and the returned raw_key is None. Caller reads the
        raw key out-of-band from SSM (e.g. via `aws ssm get-parameter
        --with-decryption`) and then deletes the parameter.

        When ssm_write=False (default), behavior is unchanged from pre-27a:
        raw_key is returned directly in the tuple. Dev and legacy paths
        continue to work without modification.

        ssm_write=True requires:
          - boto3 installed (lazy-imported)
          - AWS credentials resolvable by the default chain (task role in
            prod, profile/env in dev)
          - The invoking identity must have ssm:PutParameter on the
            bootstrap path prefix.

        If SSM write fails, the DB transaction is rolled back (when
        auto_commit=True) to keep DB and SSM in sync. No orphan row lands
        in api_keys.
        """
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

        # Flush first so we have a concrete key_id for the SSM path,
        # regardless of auto_commit mode.
        self.db.flush()
        key_id = api_key.id

        if ssm_write:
            region = ssm_region or os.environ.get(
                "AWS_REGION", SSM_DEFAULT_REGION
            )
            try:
                _write_key_to_ssm(
                    key_id=key_id, raw_key=raw_key, region=region,
                )
            except Exception as exc:
                # Roll back so we don't leave an un-retrievable key row.
                logger.error(
                    "SSM bootstrap write failed for key_id=%d: %s",
                    key_id, exc,
                )
                if auto_commit:
                    self.db.rollback()
                raise

        if auto_commit:
            self.db.commit()
            self.db.refresh(api_key)

        logger.info(
            "Created API key id=%d tenant=%s prefix=%s ssm=%s",
            key_id, tenant_id, api_key.key_prefix, ssm_write,
        )

        # Never return the raw key when it was persisted to SSM —
        # forces the caller to read out-of-band.
        return api_key, (None if ssm_write else raw_key)

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