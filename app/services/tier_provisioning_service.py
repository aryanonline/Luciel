"""Tier provisioning service — V2 (Arc 5 Path A).

Post-checkout pre-minting of the V2 Admin's primary ``Instance``.

V2 doctrine (CANONICAL_RECAP §11 Q1, §14):
* Hierarchy is ``Admin → Instance → Lead``. No Domain layer, no Agent layer.
* Every paying admin (Pro or Enterprise) gets **exactly one Instance** minted
  at checkout time. Multi-instance allowance (10 for Pro, ∞ for Enterprise)
  is for *later self-serve creation*, not at provisioning. The signup-time
  pre-mint is always 1.
* Free admins lazy-mint via the magic-link signup path elsewhere — this
  service is only invoked from the Stripe webhook after a paid checkout.

This service is the **single place** that turns a freshly-onboarded Admin
into a tier-shaped Admin. It is called from the webhook AFTER the
``Subscription`` row has been committed. A failure here does NOT roll back
the subscription — the customer is paid for and the webhook returns
success to Stripe; a reconciler can re-run pre-minting later.

All writes are atomic within ``premint_for_tier``: a failure on any step
rolls back the entire pre-mint set (no half-provisioned admins). The audit
row(s) ride the same transaction as the mutations they describe
(Invariant 4).

V1 surface deletions (Arc 5 Path A):
* ``_ensure_primary_agent`` — removed. V2 has no Agent layer.
* Domain-scope and Tenant-scope LucielInstance branches — removed. V2 has
  no scope hierarchy beyond Admin → Instance.
* ``TIER_INDIVIDUAL`` / ``TIER_TEAM`` / ``TIER_COMPANY`` imports — replaced
  with ``TIER_PRO`` / ``TIER_ENTERPRISE`` (Free admins do not flow here).
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.models.admin import TIER_FREE
from app.models.subscription import TIER_PRO, TIER_ENTERPRISE
from app.models.admin_audit_log import (
    ACTION_UPDATE,
    RESOURCE_ADMIN,
)
from app.repositories.admin_audit_repository import AdminAuditRepository, AuditContext
from app.repositories.scope_assignment_repository import ScopeAssignmentRepository
from app.services.admin_service import AdminService
from app.services.instance_service import InstanceService

if TYPE_CHECKING:
    from app.models.admin import AdminConfig
    from app.models.user import User

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------

# Deterministic instance slug for the primary buyer's Instance. Re-running
# pre-mint after a partial failure surfaces as a ``DuplicateInstanceError``
# (409) rather than a silent second copy because the V2 unique constraint
# is (admin_id, instance_slug).
_INSTANCE_SLUG_PRIMARY = "primary"

# Audit / created_by label.
_CREATED_BY = "tier_provisioning"

# Role string on the owner-side ScopeAssignment minted at self-serve
# checkout. Buyer becomes "owner" of their own Admin.
_OWNER_ROLE = "owner"

# Arc 6 Commit 8 (2026-05-23) -- Domain-collapse sentinel.
#
# The ``scope_assignments.domain_id`` column is declared
# ``nullable=False`` at the DB layer (born under the Step 24.5b Q6
# resolution before Arc 5 collapsed the Domain layer). V2 doctrine
# says "no Domain layer" but the COLUMN survives until the schema
# subtractive revision that drops it (out of scope here -- it's a
# Pro/Enterprise schema cleanup tracked separately).
#
# Until that drop lands, every ScopeAssignment insert must satisfy
# the NOT-NULL by writing a sentinel string. ``"default"`` is the
# value chosen because:
#   1. It matches the V1 legacy default (the only legitimate value
#      in the post-Arc-5 world; "general" was the legacy fixture but
#      is no longer enforced as a real domain).
#   2. It collides with itself across all V2 ScopeAssignments under
#      the same Admin, which is the correct V2 behaviour (V2 has
#      ONE domain per admin, full stop).
#   3. A future Domain-drop migration can backfill in-place from
#      this sentinel without ambiguity.
#
# Pre-Arc-6-Commit-8 this code passed ``domain_id=None`` which would
# IntegrityError at flush. The Pro pre-mint path was latent-broken;
# only Free signup (this commit) exercises the same column today.
# Fixing both with one constant keeps the rule legible.
_DOMAIN_COLLAPSE_SENTINEL = "default"


# ---------------------------------------------------------------------
# Email shape validation
# ---------------------------------------------------------------------

# Mirrors the precedent in ``app/identity/resolver.py`` — liberal-but-non-
# degenerate email contract. Synthetic ``*.luciel.local`` emails minted by
# the identity resolver pass; obvious garbage rejects with a clean
# ``TierProvisioningValidationError`` (4xx-class, do not retry).
_EMAIL_MAX_LEN = 320
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class TierUpgradeNoopError(ValueError):
    """Raised by ``upgrade_admin_tier`` when the new tier is not strictly
    higher than the current tier. The webhook traps this as a benign
    replay condition and logs.
    """


class TierDowngradeNoopError(ValueError):
    """Raised by ``downgrade_admin_tier`` when the new tier is not
    strictly LOWER than the current tier.

    Symmetric counterpart to ``TierUpgradeNoopError``. The webhook traps
    this as a benign replay condition when the V2 downgrade branch
    runs twice (Stripe redeliver of ``subscription.deleted`` after a
    partial success that already flipped the Admin row).

    Same-tier and upgrade-target inputs are also a no-op: there is no
    legal scenario where a downgrade call should resolve to a higher
    tier than current. The check is intentionally symmetric to the
    upgrade path's strictly-higher guard.
    """


class TierProvisioningValidationError(ValueError):
    """Raised when ``premint_for_tier`` is called with structurally
    invalid input (today: an unparseable ``primary_user.email``).

    Subclasses ``ValueError`` so the webhook's existing
    ``except ValueError`` trap path catches it without modification.
    """


def _validate_email_shape(email: str | None) -> str:
    """Liberal email shape gate — accepts synthetic emails, rejects
    obvious garbage. Returns the case-folded, whitespace-stripped email
    on success; raises ``TierProvisioningValidationError`` on failure.
    """
    if email is None:
        raise TierProvisioningValidationError(
            "primary_user.email is required (got None)"
        )
    if not isinstance(email, str):
        raise TierProvisioningValidationError(
            f"primary_user.email must be str (got {type(email).__name__})"
        )
    candidate = email.strip().lower()
    if not candidate:
        raise TierProvisioningValidationError(
            "primary_user.email is empty / whitespace-only"
        )
    if len(candidate) > _EMAIL_MAX_LEN:
        raise TierProvisioningValidationError(
            f"primary_user.email exceeds RFC 5321 max length "
            f"({_EMAIL_MAX_LEN}); got {len(candidate)} chars"
        )
    if not _EMAIL_SHAPE.match(candidate):
        raise TierProvisioningValidationError(
            "primary_user.email is not a valid email shape "
            "(must be `local@domain.tld` with no embedded whitespace)"
        )
    return candidate


# ---------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------

class TierProvisioningService:
    """Pre-mints the V2 primary Instance for a freshly-onboarded paying Admin.

    Lifetime: one instance per webhook call — never reuse across requests,
    the bound ``Session`` is request-scoped.
    """

    def __init__(self, db: Session) -> None:
        self.db = db
        self.admin = AdminService(db)
        self.luciel = InstanceService(db, admin_service=self.admin)
        self.audit = AdminAuditRepository(db)

    # -----------------------------------------------------------------
    # Public entry point
    # -----------------------------------------------------------------

    def premint_for_tier(
        self,
        *,
        admin_id: str,
        tier: str,
        primary_user: "User",
        audit_ctx: AuditContext,
    ) -> dict:
        """Pre-mint the V2 primary Instance for ``admin_id``.

        V2 contract: every paying tier (Pro, Enterprise) gets exactly one
        Instance at signup. Free admins are not provisioned through this
        path (they lazy-mint via magic-link signup).

        Returns a dict describing what was created — useful for tests and
        observability. The webhook does NOT consume the return; the dict
        shape may evolve.

        Idempotency: re-running pre-mint after a partial success raises
        ``DuplicateInstanceError`` from ``InstanceService.create_instance``
        on the ``(admin_id, instance_slug)`` collision. The caller (the
        webhook) catches and logs but does NOT roll back the subscription
        — a reconciler is expected to re-attempt after the duplicates are
        cleaned up.

        Raises any exception from the underlying repos / service; the
        webhook treats this as "best effort" and traps it.
        """
        # Arc 6 Commit 8 (2026-05-23) -- accept TIER_FREE alongside paid
        # tiers. The pre-mint shape (owner ScopeAssignment + exactly one
        # primary Instance) is IDENTICAL for Free, Pro, and Enterprise --
        # only the downstream entitlement caps differ (§14). Rejecting
        # Free here forced the unified-signup route to inline the same
        # logic; routing through this service keeps the rule "there is
        # exactly ONE place that turns an Admin into a tier-shaped Admin"
        # honest.
        if tier not in (TIER_FREE, TIER_PRO, TIER_ENTERPRISE):
            raise ValueError(
                f"TierProvisioningService.premint_for_tier: unknown or "
                f"unsupported tier {tier!r}"
            )

        _validate_email_shape(
            getattr(primary_user, "email", None) if primary_user is not None else None
        )

        admin = self.admin.get_tenant_config(admin_id)
        if admin is None or not getattr(admin, "active", False):
            raise ValueError(
                f"TierProvisioningService.premint_for_tier: admin {admin_id!r} "
                f"missing or inactive at pre-mint time"
            )

        created: dict = {
            "admin_id": admin_id,
            "tier": tier,
            "instance": None,
        }

        # 1. Mint the owner-side ScopeAssignment binding the buyer to the
        #    Admin with role="owner". Without this row the buyer has no
        #    Admin binding and every cookied admin route fails 403.
        #    Drift: D-step-30a-owner-scopeassignment-missing-self-serve-
        #    checkout-2026-05-17.
        self._ensure_owner_scope_assignment(
            admin=admin,
            primary_user=primary_user,
            audit_ctx=audit_ctx,
        )

        # 2. Mint exactly one Instance — the buyer's primary Luciel they
        #    chat with from day one. Pro tier can add up to 9 more via
        #    self-serve; Enterprise unlimited; this is the seed.
        instance = self.luciel.create_instance(
            audit_ctx=audit_ctx,
            admin_id=admin_id,
            instance_slug=_INSTANCE_SLUG_PRIMARY,
            display_name=f"{admin.name} Luciel",
            description=(
                f"Pre-minted at {tier} signup — primary Instance for the Admin."
            ),
            created_by=_CREATED_BY,
        )
        created["instance"] = {
            "instance_slug": instance.instance_slug,
            "pk": instance.id,
        }

        logger.info(
            "tier_provisioning: pre-minted admin=%s tier=%s instance=%s",
            admin_id, tier, instance.instance_slug,
        )
        return created

    # -----------------------------------------------------------------
    # Tier UPGRADE  (Arc 6 / Commit 8.5a)
    # -----------------------------------------------------------------

    # Tier ordinal -- defines what counts as an "upgrade" vs "downgrade".
    # Higher ordinal = strictly more capabilities. Free<Pro<Enterprise.
    _TIER_ORDINAL: dict = {
        TIER_FREE: 0,
        TIER_PRO: 1,
        TIER_ENTERPRISE: 2,
    }

    def upgrade_admin_tier(
        self,
        *,
        admin_id: str,
        new_tier: str,
        new_tier_source: str,
        audit_ctx: AuditContext,
    ) -> dict:
        """Flip an existing Admin's tier upward.

        Used by the webhook upgrade-branch (``_on_checkout_completed``
        with ``luciel_admin_id`` in metadata) to convert a Free Admin
        into a paid Admin, or to elevate Pro -> Enterprise, WITHOUT
        re-running the full ``premint_for_tier`` flow.

        What this method does:
          1. Verifies the Admin exists and is active.
          2. Verifies the new tier is strictly higher than the current
             tier (else raises ValueError -- callers must not call this
             with a same-tier or downgrade target).
          3. Updates ``admins.tier`` and ``admins.tier_source`` in a
             single committed transaction with an audit row.

        What this method DOES NOT do:
          * Re-mint the primary Instance (it already exists from the
             original ``premint_for_tier`` call at the Admin's first
             provisioning -- Free signup or Pro signup).
          * Touch the owner ScopeAssignment (the buyer is already the
             Admin's owner; the role doesn't change with tier).
          * Provision the per-tier delta-cap rows. Entitlement caps
             are read at request time from
             ``policy.entitlements.get_caps_for_tier(admin.tier)``, so
             flipping the tier column automatically unlocks the new
             caps for every subsequent API call -- no row-level changes
             needed for caps to take effect.
          * Issue Stripe-side changes. The Subscription row is written
             by the webhook's existing Subscription-create path; this
             method only mutates the Admin row.

        Idempotency:
          A Stripe webhook redeliver after a partial success that
          already updated the Admin row will re-call this method. The
          "strictly higher tier" guard naturally short-circuits: a
          replay attempts new_tier=current_tier and is rejected with
          ``TierUpgradeNoopError``. The webhook traps this and logs.

        Raises:
          ValueError              -- unknown new_tier, or admin missing/inactive.
          TierUpgradeNoopError    -- new_tier <= current_tier (replay safety).
        """
        if new_tier not in self._TIER_ORDINAL:
            raise ValueError(
                f"TierProvisioningService.upgrade_admin_tier: unknown new_tier {new_tier!r}"
            )
        if new_tier == TIER_FREE:
            # Free is never an upgrade target. Callers must not reach
            # here with new_tier='free' -- the route layer validates
            # this and the downgrade path lives in Commit 8.5b.
            raise ValueError(
                "upgrade_admin_tier: new_tier='free' is a downgrade, "
                "not an upgrade. Use the downgrade path (Commit 8.5b)."
            )

        admin = self.admin.get_tenant_config(admin_id)
        if admin is None or not getattr(admin, "active", False):
            raise ValueError(
                f"upgrade_admin_tier: admin {admin_id!r} missing or inactive"
            )

        old_tier = admin.tier
        old_tier_source = admin.tier_source

        if self._TIER_ORDINAL[new_tier] <= self._TIER_ORDINAL.get(old_tier, -1):
            raise TierUpgradeNoopError(
                f"upgrade_admin_tier: new_tier={new_tier!r} is not strictly "
                f"higher than current tier={old_tier!r} on admin={admin_id!r}"
            )

        # Single committed update. We do NOT call
        # ``AdminService.update_tenant_config`` because that method
        # commits without writing an audit row; we want the tier-flip
        # mutation and its audit row to commit atomically (Invariant 4).
        admin.tier = new_tier
        admin.tier_source = new_tier_source
        self.audit.record(
            ctx=audit_ctx,
            tenant_id=admin_id,
            action=ACTION_UPDATE,
            resource_type=RESOURCE_ADMIN,
            resource_natural_id=admin_id,
            before={"tier": old_tier, "tier_source": old_tier_source},
            after={"tier": new_tier, "tier_source": new_tier_source},
            note=f"Tier upgrade {old_tier} -> {new_tier} via {new_tier_source}",
            autocommit=False,
        )
        self.db.commit()

        logger.info(
            "tier_provisioning: upgraded admin=%s tier %s -> %s source=%s",
            admin_id, old_tier, new_tier, new_tier_source,
        )
        return {
            "admin_id": admin_id,
            "old_tier": old_tier,
            "new_tier": new_tier,
            "new_tier_source": new_tier_source,
        }

    # -----------------------------------------------------------------
    # Tier downgrade — Arc 6 Commit 8.5b.
    # -----------------------------------------------------------------

    def downgrade_admin_tier(
        self,
        *,
        admin_id: str,
        new_tier: str,
        new_tier_source: str,
        audit_ctx: AuditContext,
    ) -> dict:
        """Flip an existing Admin's tier downward.

        Used by the webhook V2 downgrade-branch
        (``_on_subscription_deleted`` with the sub's
        ``pending_downgrade_target`` set) to convert a paid Admin into
        Free, or to demote Enterprise -> Pro, AFTER Stripe has fired
        ``subscription.deleted`` at ``current_period_end``.

        What this method does:
          1. Verifies the Admin exists and is active.
          2. Verifies the new tier is strictly LOWER than the current
             tier (else raises ``TierDowngradeNoopError`` -- callers
             must not call this with a same-tier or upgrade target).
          3. Updates ``admins.tier`` and ``admins.tier_source`` in a
             single committed transaction with an audit row.

        What this method DOES NOT do:
          * Archive overflow rows. That lives in
            ``DowngradeArchiveService.archive_overflow_for_admin``,
            called by the webhook AFTER this method returns.
            Separation: this method touches only ``admins``; the
            archive service touches ``instances`` / ``api_keys`` /
            ``admin_widget_domains`` / ``scope_assignments``.
          * Cancel any Stripe state. The webhook fires AFTER Stripe
            has already cancelled at period_end; nothing here calls
            into the Stripe API.
          * Remove or null out the buyer's owner ScopeAssignment.
            The owner seat is exempt from overflow archive (see
            ``DowngradeArchiveService._is_owner_seat``); a downgraded
            admin still owns their Admin row.
          * Touch the ``Subscription`` row. The webhook handles the
            sub-row updates (active=False, status=canceled,
            pending_downgrade_target=None) in its own atomic write.

        Symmetric to ``upgrade_admin_tier``: same audit-shape, same
        atomicity guarantee, mirrored strictly-lower guard.

        Idempotency:
          A Stripe webhook redeliver after a partial success that
          already updated the Admin row will re-call this method. The
          "strictly lower tier" guard naturally short-circuits: a
          replay attempts new_tier=current_tier and is rejected with
          ``TierDowngradeNoopError``. The webhook traps this and logs.

        Raises:
          ValueError                -- unknown new_tier, or admin missing/inactive.
          TierDowngradeNoopError    -- new_tier >= current_tier (replay safety).
        """
        if new_tier not in self._TIER_ORDINAL:
            raise ValueError(
                f"TierProvisioningService.downgrade_admin_tier: unknown new_tier {new_tier!r}"
            )
        if new_tier == TIER_ENTERPRISE:
            # Enterprise is never a downgrade target -- it is the top
            # tier. Callers must not reach here with this argument; the
            # route layer validates it, the schema CHECK on
            # subscriptions.pending_downgrade_target also rejects it,
            # and this is the third layer of the same gate.
            raise ValueError(
                "downgrade_admin_tier: new_tier='enterprise' is an upgrade, "
                "not a downgrade. Use upgrade_admin_tier."
            )

        admin = self.admin.get_tenant_config(admin_id)
        if admin is None or not getattr(admin, "active", False):
            raise ValueError(
                f"downgrade_admin_tier: admin {admin_id!r} missing or inactive"
            )

        old_tier = admin.tier
        old_tier_source = admin.tier_source

        # Strictly-lower guard. Symmetric to upgrade_admin_tier's
        # strictly-higher guard. Same-tier and upward-target inputs
        # are both rejected as TierDowngradeNoopError.
        if self._TIER_ORDINAL[new_tier] >= self._TIER_ORDINAL.get(old_tier, 999):
            raise TierDowngradeNoopError(
                f"downgrade_admin_tier: new_tier={new_tier!r} is not strictly "
                f"lower than current tier={old_tier!r} on admin={admin_id!r}"
            )

        # Single committed update + audit row, atomic per Invariant 4.
        # Same pattern as upgrade_admin_tier; deliberately mirrored
        # for symmetry and reviewer ergonomics.
        admin.tier = new_tier
        admin.tier_source = new_tier_source
        self.audit.record(
            ctx=audit_ctx,
            tenant_id=admin_id,
            action=ACTION_UPDATE,
            resource_type=RESOURCE_ADMIN,
            resource_natural_id=admin_id,
            before={"tier": old_tier, "tier_source": old_tier_source},
            after={"tier": new_tier, "tier_source": new_tier_source},
            note=f"Tier downgrade {old_tier} -> {new_tier} via {new_tier_source}",
            autocommit=False,
        )
        self.db.commit()

        logger.info(
            "tier_provisioning: downgraded admin=%s tier %s -> %s source=%s",
            admin_id, old_tier, new_tier, new_tier_source,
        )
        return {
            "admin_id": admin_id,
            "old_tier": old_tier,
            "new_tier": new_tier,
            "new_tier_source": new_tier_source,
        }

    # -----------------------------------------------------------------
    # Owner ScopeAssignment provisioning
    # -----------------------------------------------------------------

    def _ensure_owner_scope_assignment(
        self,
        *,
        admin: "AdminConfig",
        primary_user: "User",
        audit_ctx: AuditContext,
    ) -> None:
        """Resolve-or-create the owner-role ScopeAssignment for the buyer.

        Idempotent on retry: a Stripe webhook redeliver after a partial
        success must not create a second active assignment. We look up
        any currently-active assignment for (user, admin) first — there
        should be at most one per (user, admin) in steady state. If we
        find one, we log and return without touching it.

        Writes an ACTION_CREATE / RESOURCE_SCOPE_ASSIGNMENT audit row in
        the same transaction as the INSERT (Invariant 4), via the repo's
        ``audit_ctx`` passthrough.

        Commits so the immediate subsequent Instance create sees the same
        transactional state.
        """
        sar = ScopeAssignmentRepository(self.db)

        # ScopeAssignmentRepository.get_active_for_user_in_tenant() still
        # carries the legacy kwarg name ``tenant_id`` during the migration
        # window; the new Admin.id replaces tenant_configs.tenant_id 1:1
        # (same String(100) semantic slug per Q1 lock). The kwarg renames
        # to ``admin_id`` in a follow-up after Revision C lands.
        existing = sar.get_active_for_user_in_tenant(
            user_id=primary_user.id,
            tenant_id=admin.id,
        )
        if existing is not None:
            logger.info(
                "tier_provisioning: reusing existing owner scope assignment "
                "admin=%s user=%s assignment_id=%s role=%s",
                admin.id,
                primary_user.id,
                existing.id,
                existing.role,
            )
            return

        sar.create(
            user_id=primary_user.id,
            tenant_id=admin.id,
            # Arc 6 Commit 8 -- write the Domain-collapse sentinel rather
            # than None. The column is nullable=False at the DB layer
            # (V1 inheritance), and the V2 model has not yet had the
            # column dropped. See _DOMAIN_COLLAPSE_SENTINEL docstring.
            domain_id=_DOMAIN_COLLAPSE_SENTINEL,
            role=_OWNER_ROLE,
            autocommit=False,  # we commit below, after the audit row lands
            audit_ctx=audit_ctx,
        )

        self.db.commit()
        logger.info(
            "tier_provisioning: created owner scope assignment admin=%s "
            "user=%s role=%s",
            admin.id,
            primary_user.id,
            _OWNER_ROLE,
        )
