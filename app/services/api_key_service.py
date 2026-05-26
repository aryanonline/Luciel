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
- admin_id type corrected to `str | None` — aligns with 26b.1 DB
  migration 3447ac8b45b4 (nullable) and ApiKeyCreate schema. Required
  for platform-admin keys with admin_id=NULL per Invariant 5.
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
    ACTION_CREATE,
    ACTION_DEACTIVATE,
    ACTION_KEY_ROTATED_ON_ROLE_CHANGE,
    RESOURCE_API_KEY,
)
from app.models.instance import Instance as LucielInstance
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

    Step 29.y C24: Overwrite=True so rotation is a single atomic SSM call.
    Previously Overwrite=False with a comment 'caller deletes stale params
    first' — but no caller and no IAM role actually performed that delete,
    making the production path stable-but-unrotatable. The DB hash-chained
    audit log already provides full key-history attribution; SSM does not
    need its own version guard. Tags are written via a second AddTagsToResource
    call because put_parameter rejects Tags + Overwrite=True in one call.

    boto3 is imported lazily so dev/test paths that never hit this branch
    do not need boto3 installed.
    """
    import boto3  # lazy import — keeps dev paths dependency-free

    path = ssm_path if ssm_path is not None else SSM_BOOTSTRAP_PATH.format(key_id=key_id)
    ssm = boto3.client("ssm", region_name=region)
    is_rotation = ssm_path is not None  # production path is stable across re-mints

    # Step 29.y C24: rotation-friendly (Overwrite=True) for the durable
    # production path; first-write-only (Overwrite=False) for bootstrap
    # path which carries key_id and is unique per mint.
    put_kwargs = dict(
        Name=path,
        Value=raw_key,
        Type="SecureString",
        Overwrite=is_rotation,
        Description=(
            f"Luciel platform-admin key id={key_id} at {path}. "
            f"Read by operator or task role; managed via SSM."
        ),
    )
    tags = [
        {"Key": "luciel:purpose", "Value": (
            "platform-admin-key" if is_rotation else "bootstrap-admin-key"
        )},
        {"Key": "luciel:key_id", "Value": str(key_id)},
    ]
    if not is_rotation:
        # Bootstrap path: tags can be set inline because Overwrite=False.
        put_kwargs["Tags"] = tags

    ssm.put_parameter(**put_kwargs)

    if is_rotation:
        # Production path: AWS rejects Tags + Overwrite=True in one call,
        # so apply tags via a follow-up AddTagsToResource. Failures here
        # should NOT roll back the DB — the key is already in SSM and the
        # DB row exists; tags are metadata. We log and continue.
        try:
            ssm.add_tags_to_resource(
                ResourceType="Parameter",
                ResourceId=path,
                Tags=tags,
            )
        except Exception as tag_err:  # noqa: BLE001
            logger.warning(
                "SSM tag write failed (non-fatal) path=%s key_id=%d err=%s",
                path, key_id, tag_err,
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
        admin_id: str | None,                      # Step 27a: was `str`
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
        audit_ctx: AuditContext | None = None,
        # ----- Step 30b commit (a) of step-30b-embed-key-issuance -----
        # Four keyword-only kwargs surfacing the embed-key columns added
        # by alembic migration a7c1f4e92b85. All default to admin-key
        # behavior so every existing caller (admin-key endpoint, SSM
        # bootstrap, rotation cascade, tests) continues to work
        # unchanged. The new embed-key issuance endpoint passes all
        # four. ssm_write is incompatible with key_kind='embed' (the
        # customer must read the raw key out of the response, not
        # SSM); enforced below.
        key_kind: str = "admin",
        allowed_origins: list[str] | None = None,
        rate_limit_per_minute: int | None = None,
        widget_config: dict | None = None,
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

        Step 28 P3-B (Phase 3, Commit 3): emits an ACTION_CREATE audit
        row in the same transaction as the api_keys INSERT, upholding
        Invariant 4 (audit-before-commit). Previously the audit row was
        emitted at the API endpoint layer (app/api/v1/admin.py
        create_api_key) AFTER service.create_key() had already auto-
        committed -- two transactions, with a window where the key
        existed in the DB without an audit row. Now any caller of
        create_key (admin endpoint, scripts, future internal callers)
        automatically lands an audit row atomically with the key.

        ``audit_ctx`` is keyword-only and optional for backward
        compatibility. When omitted (legacy callers, system-internal
        flows like SSM bootstrap scripts), an
        AuditContext.system(label="create_key") actor is used so the
        audit row is always attributable. New callers (admin endpoints,
        operator scripts) MUST thread the request-scoped AuditContext
        through.

        Step 30b commit (a) of step-30b-embed-key-issuance: four new
        keyword-only kwargs surface the embed-key columns added by
        alembic migration a7c1f4e92b85.
          - key_kind         : 'admin' (default) or 'embed'
          - allowed_origins  : NULL (default) for admin keys; non-empty
                               list for embed keys
          - rate_limit_per_minute : NULL (default) for admin keys; positive
                                    int for embed keys
          - widget_config    : NULL (default) for admin keys; three-knob
                               JSONB-serializable dict for embed keys
        Shape invariants for embed keys are NOT enforced here -- the
        EmbedKeyCreate Pydantic schema and the admin endpoint enforce
        them upstream. This service intentionally accepts any kwarg
        combination so unit tests for negative paths can construct
        invalid rows without monkey-patching. The one rule we DO
        enforce here is mutual exclusion between embed keys and
        ssm_write, because that combination has no legitimate use
        case and would cost real time to debug if it slipped through.
        """
        if key_kind == "embed" and ssm_write:
            # Embed keys are customer-facing; the customer needs the
            # raw value to paste into their HTML, and customers cannot
            # read SSM parameters owned by Luciel. Refusing this combo
            # at the service layer prevents a future caller from
            # accidentally minting an unrecoverable embed key.
            raise ValueError(
                "ssm_write=True is incompatible with key_kind='embed'. "
                "Embed keys must be returned to the customer at issuance "
                "time so they can paste the value into their site."
            )

        raw_key = generate_raw_key()
        hashed = hash_key(raw_key)

        api_key = ApiKey(
            key_hash=hashed,
            key_prefix=raw_key[:12],
            admin_id=admin_id,
            domain_id=domain_id,
            agent_id=agent_id,
            luciel_instance_id=luciel_instance_id,   # Step 24.5
            display_name=display_name,
            permissions=permissions or ["chat", "sessions"],
            rate_limit=rate_limit,
            active=True,
            created_by=created_by,
            # Step 30b commit (a) of step-30b-embed-key-issuance:
            # forward the four widget columns. Defaults preserve
            # admin-key behavior; admin-key endpoint never sets these.
            key_kind=key_kind,
            allowed_origins=allowed_origins,
            rate_limit_per_minute=rate_limit_per_minute,
            widget_config=widget_config,
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

        # --- Step 28 P3-B: audit-before-commit (Invariant 4) ----------
        # Emit the ACTION_CREATE audit row in the same transaction as the
        # api_keys INSERT, BEFORE the commit. autocommit=False so the
        # audit row rides our commit boundary -- if the commit fails or
        # is rolled back by a later step (e.g. ssm_write retry path),
        # the audit row is rolled back with it.
        # Step 30b commit (a) of step-30b-embed-key-issuance: extend
        # the audit-row payload with the four widget columns when
        # they are non-default, so the audit log captures whether a
        # row landed as an embed key with which origins/cap/branding.
        # We do NOT record the widget_config verbatim because greeting
        # and display_name are customer-facing strings; we record only
        # the keys that were set, which is enough to prove the row's
        # shape without leaking content into the audit trail. The
        # raw branding text is recoverable from api_keys directly.
        audit_after: dict = {
            "display_name": display_name,
            "permissions": api_key.permissions,
            "rate_limit": rate_limit,
            "bound_to_luciel_instance": luciel_instance_id is not None,
            "ssm_write": ssm_write,
        }
        if key_kind != "admin":
            audit_after["key_kind"] = key_kind
        if allowed_origins:
            audit_after["allowed_origins_count"] = len(allowed_origins)
        if rate_limit_per_minute is not None:
            audit_after["rate_limit_per_minute"] = rate_limit_per_minute
        if widget_config:
            audit_after["widget_config_keys"] = sorted(widget_config.keys())

        AdminAuditRepository(self.db).record(
            ctx=audit_ctx if audit_ctx is not None else AuditContext.system(
                label="create_key"
            ),
            admin_id=admin_id or SYSTEM_ACTOR_TENANT,
            action=ACTION_CREATE,
            resource_type=RESOURCE_API_KEY,
            resource_pk=key_id,
            resource_natural_id=api_key.key_prefix,
            domain_id=domain_id,
            agent_id=agent_id,
            luciel_instance_id=luciel_instance_id,
            after=audit_after,
            note=None,
            autocommit=False,
        )

        if auto_commit:
            self.db.commit()
            self.db.refresh(api_key)

        logger.info(
            "Created API key id=%d tenant=%s prefix=%s ssm=%s",
            key_id, admin_id, api_key.key_prefix, ssm_write,
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

    def list_keys(self, admin_id: str | None = None) -> list[ApiKey]:
        """List API keys, optionally filtered by tenant."""
        stmt = select(ApiKey).order_by(ApiKey.created_at.desc())
        if admin_id:
            stmt = stmt.where(ApiKey.admin_id == admin_id)
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
            admin_id=api_key.admin_id or SYSTEM_ACTOR_TENANT,
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
        """Arc 5 Path A — V2 collapse stub.

        The V1 implementation walked Agent → LucielInstance bindings to
        rotate keys on role-change. V2 has no Agent layer; keys are
        bound to (admin_id, instance_id) directly. The V2 cascade
        rewrite (lands at Arc 6 alongside the cookied-Admin role
        management surface) will replace this stub with an
        ``admin_id``-keyed rotation; until then the method is a no-op
        and returns ``0``, matching the agent-not-found branch of the
        legacy implementation. The cascade caller (ScopeAssignmentService
        .end_assignment) was already gutted at Commit A5 and no longer
        invokes this method.
        """
        logger.info(
            "rotate_keys_for_agent: Arc 5 Path A V2 no-op stub "
            "agent_id_pk=%d reason=%r (no rotation performed; "
            "V2 cascade replaces this in Arc 6)",
            agent_id_pk,
            reason[:80],
        )
        return 0


    def deactivate_all_for_tenant(
        self,
        *,
        admin_id: str,
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
                ApiKey.admin_id == admin_id,
                ApiKey.active.is_(True),
            )
            .all()
        )
        affected_pks = [pk for pk, _ in affected]
        affected_prefixes = [prefix for _, prefix in affected]

        updated = (
            self.db.query(ApiKey)
            .filter(
                ApiKey.admin_id == admin_id,
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
                admin_id=admin_id,
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
                note=f"Cascade from tenant {admin_id} deactivation",
                autocommit=False,
            )

        if autocommit:
            self.db.commit()
        logger.info(
            "ApiKey cascade-deactivated count=%d tenant=%s",
            updated,
            admin_id,
        )
        return int(updated)