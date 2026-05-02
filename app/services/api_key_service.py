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

from app.models.admin_audit_log import (
    ACTION_CASCADE_DEACTIVATE,
    ACTION_DEACTIVATE,
    ACTION_KEY_ROTATED_ON_ROLE_CHANGE,
    RESOURCE_API_KEY,
)
from app.models.luciel_instance import LucielInstance
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
    SYSTEM_ACTOR_TENANT,
)

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
                    key_id=key_id, raw_key=raw_key, region=region, ssm_path=ssm_path,
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

    def deactivate_key(
        self,
        key_id: int,
        *,
        audit_ctx: AuditContext | None = None,
    ) -> bool:
        """Deactivate an API key.

        Step 28 D5 (Phase 1, Commit 6): emits an ACTION_DEACTIVATE
        audit row in the same transaction as the active=False UPDATE,
        upholding Invariant 4 (audit-before-commit). This brings the
        single-key admin DELETE path in line with the cascade path
        in rotate_keys_for_agent, which has emitted audit rows since
        Step 24.5b.

        ``audit_ctx`` is keyword-only and optional for backward
        compatibility. When omitted (legacy callers, system-internal
        flows), an AuditContext.system(label="deactivate_key") actor
        is used so the audit row is always attributable. New callers
        (admin endpoints, scripts) MUST thread the request-scoped
        AuditContext through.
        """
        api_key = self.db.get(ApiKey, key_id)
        if not api_key:
            return False

        was_active = api_key.active
        api_key.active = False

        audit_repo = AdminAuditRepository(self.db)
        audit_repo.record(
            ctx=audit_ctx if audit_ctx is not None else AuditContext.system(
                label="deactivate_key"
            ),
            tenant_id=api_key.tenant_id or SYSTEM_ACTOR_TENANT,
            action=ACTION_DEACTIVATE,
            resource_type=RESOURCE_API_KEY,
            resource_pk=api_key.id,
            resource_natural_id=api_key.key_prefix,
            domain_id=api_key.domain_id,
            agent_id=api_key.agent_id,
            luciel_instance_id=api_key.luciel_instance_id,
            before={"active": was_active},
            after={"active": False},
            note=None,
            autocommit=False,
        )

        self.db.commit()
        logger.info("Deactivated API key id=%d", key_id)
        return True

    def get_key_by_id(self, key_id: int) -> ApiKey | None:
        return self.db.query(ApiKey).filter(ApiKey.id == key_id).first()
    def rotate_keys_for_agent(
        self,
        *,
        agent_id_pk: int,
        reason: str,
        audit_ctx: AuditContext | None = None,
    ) -> int:
        """Rotate (deactivate) every active ApiKey bound to an Agent.

        Step 24.5b. Q6 mandatory key rotation cascade. Hard rotation,
        no grace period (Step 24.5b decision A). Called by
        ScopeAssignmentService.end_assignment when an assignment ends
        for any reason (PROMOTED / DEMOTED / REASSIGNED / DEPARTED /
        DEACTIVATED).

        Walks two scopes of bound keys:
        1. Direct: ApiKey.agent_id matches the Agent's natural slug
           AND ApiKey.tenant_id matches the Agent's tenant.
        2. Indirect: ApiKey.luciel_instance_id references any
           LucielInstance whose owning agent is this one. A chat-key
           pinned to a Luciel owned by an ending-role Agent must
           stop working immediately -- otherwise the User's chat
           experience continues working past the role change, which
           is exactly the security gap Q6 closes.

        Returns the count of keys rotated (sum of both scopes).

        Emits per-key ACTION_KEY_ROTATED_ON_ROLE_CHANGE audit rows
        in the same txn (Invariant 4). Each audit row includes:
        - resource_type=RESOURCE_API_KEY
        - resource_pk=<api_key.id>
        - resource_natural_id=<api_key.key_prefix>
        - tenant_id=<api_key.tenant_id or SYSTEM_ACTOR_TENANT>
        - before={"active": True}, after={"active": False}
        - note=reason (the cascade reason from ScopeAssignmentService)

        Idempotent: keys already inactive are skipped (no-op + no
        duplicate audit rows). This protects against re-entry during
        cascade sequences (e.g. UserService.deactivate_user looping
        end_assignment over many assignments where some Agent rows
        share keys via shared LucielInstances).

        Does NOT commit -- caller (ScopeAssignmentService.end_assignment)
        owns the txn boundary. The cascade contract is: assignment
        end + key rotation in same txn, single commit at end.

        Step 28 D5 note (closed): deactivate_key() now also emits
        ACTION_DEACTIVATE audit rows via AdminAuditRepository.record
        in the same txn as the active=False UPDATE. This method
        remains the cascade entry point for role-change rotations
        because it walks both scope dimensions (direct ApiKey.agent_id
        binding + indirect LucielInstance ownership); single-key
        admin DELETEs go through deactivate_key().
        """
        # Look up the Agent so we can resolve both scope dimensions:
        # the Agent's natural agent_id slug (for direct ApiKey matches)
        # and its primary key (for LucielInstance ownership traversal).
        from app.models.agent import Agent

        agent = self.db.get(Agent, agent_id_pk)
        if agent is None:
            logger.warning(
                "rotate_keys_for_agent: agent_id_pk=%d not found, "
                "no rotation performed",
                agent_id_pk,
            )
            return 0

        rotated_count = 0
        audit_repo = AdminAuditRepository(self.db)

        # ------ Scope 1: direct ApiKey bindings ------
        # Match on (tenant_id, agent_id) natural-key tuple. Agent.agent_id
        # is the slug; ApiKey.agent_id stores that same slug.
        direct_stmt = select(ApiKey).where(
            ApiKey.tenant_id == agent.tenant_id,
            ApiKey.agent_id == agent.agent_id,
            ApiKey.active.is_(True),
        )
        direct_keys = list(self.db.scalars(direct_stmt).all())

        # ------ Scope 2: LucielInstance-pinned ApiKey bindings ------
        # Find every LucielInstance owned by this Agent (one Agent can
        # own multiple Luciels per Step 24.5 doctrine), then find every
        # active ApiKey pinned to any of those LucielInstance rows.
        owned_luciels_stmt = select(LucielInstance.id).where(
            LucielInstance.scope_owner_agent_id == agent.agent_id,
            LucielInstance.scope_owner_tenant_id == agent.tenant_id,
        )
        owned_luciel_ids = [row for row in self.db.scalars(owned_luciels_stmt).all()]

        indirect_keys: list[ApiKey] = []
        if owned_luciel_ids:
            indirect_stmt = select(ApiKey).where(
                ApiKey.luciel_instance_id.in_(owned_luciel_ids),
                ApiKey.active.is_(True),
            )
            indirect_keys = list(self.db.scalars(indirect_stmt).all())

        # ------ Rotate ------
        # De-dupe across the two scopes (a key can technically be both
        # agent-bound and luciel-bound depending on mint pattern).
        seen_ids: set[int] = set()
        all_keys = []
        for key in direct_keys + indirect_keys:
            if key.id in seen_ids:
                continue
            seen_ids.add(key.id)
            all_keys.append(key)

        for key in all_keys:
            # Idempotency guard: already-inactive keys are a no-op.
            # Defensive against re-entry during cascade sequences.
            if not key.active:
                continue

            key.active = False
            rotated_count += 1

            audit_repo.record(
                ctx=audit_ctx if audit_ctx is not None else AuditContext.system(
                    label="rotate_keys_for_agent"
                ),
                tenant_id=key.tenant_id or SYSTEM_ACTOR_TENANT,
                action=ACTION_KEY_ROTATED_ON_ROLE_CHANGE,
                resource_type=RESOURCE_API_KEY,
                resource_pk=key.id,
                resource_natural_id=key.key_prefix,
                domain_id=key.domain_id,
                agent_id=key.agent_id,
                luciel_instance_id=key.luciel_instance_id,
                before={"active": True},
                after={"active": False},
                note=reason,
                autocommit=False,
            )

        # Flush so the UPDATE + audit INSERTs hit the DB before the
        # caller's commit boundary. Doesn't commit -- ScopeAssignmentService
        # owns the txn.
        self.db.flush()

        logger.info(
            "rotate_keys_for_agent agent_pk=%d agent_id=%s tenant=%s "
            "direct_keys=%d indirect_keys=%d rotated=%d reason=%r",
            agent_id_pk,
            agent.agent_id,
            agent.tenant_id,
            len(direct_keys),
            len(indirect_keys),
            rotated_count,
            reason[:80],
        )
        return rotated_count


    def deactivate_all_for_tenant(
        self,
        *,
        tenant_id: str,
        audit_ctx: AuditContext | None = None,
        autocommit: bool = True,
    ) -> int:
        """Cascade: deactivate every active ApiKey for a tenant.

        Used by AdminService.deactivate_tenant_with_cascade. Returns
        the number of rows updated. Writes one cascade audit row
        (only when updated > 0, matching LucielInstanceRepository's
        per-tenant cascade pattern).

        autocommit=True by default for standalone callers. The tenant-
        cascade spine passes autocommit=False so the whole cascade
        commits in a single transaction.
        """
        affected = (
            self.db.query(ApiKey.id, ApiKey.key_prefix)
            .filter(
                ApiKey.tenant_id == tenant_id,
                ApiKey.active.is_(True),
            )
            .all()
        )
        affected_pks = [pk for pk, _ in affected]
        affected_prefixes = [prefix for _, prefix in affected]

        updated = (
            self.db.query(ApiKey)
            .filter(
                ApiKey.tenant_id == tenant_id,
                ApiKey.active.is_(True),
            )
            .update(
                {ApiKey.active: False},
                synchronize_session=False,
            )
        )

        if audit_ctx is not None and updated:
            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                tenant_id=tenant_id,
                action=ACTION_CASCADE_DEACTIVATE,
                resource_type=RESOURCE_API_KEY,
                resource_pk=None,
                resource_natural_id=None,
                after={
                    "count": int(updated),
                    "affected_pks": affected_pks,
                    "affected_key_prefixes": affected_prefixes,
                    "trigger": "tenant_deactivate",
                },
                note=f"Cascade from tenant {tenant_id} deactivation",
                autocommit=False,
            )

        if autocommit:
            self.db.commit()
        logger.info(
            "ApiKey cascade-deactivated count=%d tenant=%s",
            updated,
            tenant_id,
        )
        return int(updated)