"""Step 30a.7 -- one-shot backfill for cascade-orphan rows.

Repairs tenants that were soft-deactivated BEFORE the Step 30a.7 cascade
extension shipped and therefore left behind privilege-bearing rows that
the post-30a.7 cascade would have caught. Concretely: for every tenant
whose ``tenant_configs.active = False``, find any rows in the four new
privilege layers that are still in their pre-cascade state, and stamp
them as if the cascade had visited them at the moment of deactivation.

The four target layers (matching the in-function cascade, layers 9-12):
  9.  scope_assignments  WHERE tenant_id = :t AND active = TRUE
                         -> active=False, ended_at=now(),
                            ended_reason=DEACTIVATED,
                            ended_by_api_key_id=NULL
  10. user_invites       WHERE tenant_id = :t AND status = 'pending'
                         -> status='revoked', revoked_at=now()
  11. sessions           WHERE tenant_id = :t AND status = 'active'
                         -> status='revoked'
  12. synthetic-only users  (post-layer-9 candidates with zero remaining
                            active scope_assignments AND synthetic=True)
                         -> active=False

Each layer emits one audit row per orphan repaired, mirroring the
in-function cascade audit shape exactly. actor_label='backfill_30a7'
distinguishes backfill rows from real-time cascade rows in audit search.

Idempotent. Re-runnable. Safe to dry-run repeatedly.

Closes the operational arm of:
  D-tenant-cascade-privilege-revocation-hardening-2026-05-20 (umbrella)
  D-cascade-missing-scope-assignments-layer-2026-05-20
  D-cascade-missing-user-invites-revocation-2026-05-20
  D-cascade-missing-sessions-revocation-2026-05-20
  D-cascade-missing-synthetic-users-orphan-layer-2026-05-20

Usage:
    # Dry-run (no mutations, prints blast radius) -- DEFAULT.
    python -m scripts.backfill_cascade_orphans

    # Apply (writes rows + emits audit rows). Requires explicit flag.
    python -m scripts.backfill_cascade_orphans --apply

    # Scope to a single tenant (useful for surgical co-354c5056 repair).
    python -m scripts.backfill_cascade_orphans --tenant co-354c5056 --apply

    # Verbose per-row logging.
    python -m scripts.backfill_cascade_orphans --apply --verbose

Production runbook (Step 30a.7 PROD-TIME):
    aws ecs execute-command --cluster luciel-cluster \\
        --task <TASK_ARN> --container luciel-backend --interactive \\
        --command "python -m scripts.backfill_cascade_orphans"
    # ... review dry-run output ...
    aws ecs execute-command --cluster luciel-cluster \\
        --task <TASK_ARN> --container luciel-backend --interactive \\
        --command "python -m scripts.backfill_cascade_orphans --apply"

Exit codes:
    0 -- success (dry-run OR apply completed cleanly)
    1 -- usage / argument error
    2 -- runtime error (DB connect failed, etc.)
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass

from sqlalchemy import func

from app.db.session import SessionLocal
from app.models.admin_audit_log import (
    ACTION_CASCADE_DEACTIVATE,
    ACTION_INVITE_REVOKED,
    RESOURCE_SCOPE_ASSIGNMENT,
    RESOURCE_SESSION,
    RESOURCE_USER,
    RESOURCE_USER_INVITE,
)
from app.models.scope_assignment import EndReason, ScopeAssignment
from app.models.session import SessionModel
from app.models.tenant import TenantConfig
from app.models.user import User
from app.models.user_invite import InviteStatus, UserInvite
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)

logger = logging.getLogger("backfill_cascade_orphans")

BACKFILL_ACTOR_LABEL = "backfill_30a7"


# ---------------------------------------------------------------------
# Summary accumulator
# ---------------------------------------------------------------------


@dataclass
class TenantOrphanSummary:
    tenant_id: str
    scope_assignment_orphans: int = 0
    user_invite_orphans: int = 0
    session_orphans: int = 0
    synthetic_user_orphans: int = 0

    @property
    def total(self) -> int:
        return (
            self.scope_assignment_orphans
            + self.user_invite_orphans
            + self.session_orphans
            + self.synthetic_user_orphans
        )


# ---------------------------------------------------------------------
# Core enumeration / repair
# ---------------------------------------------------------------------


def enumerate_orphan_tenants(db, tenant_filter: str | None) -> list[str]:
    """Return tenant_ids whose tenant_configs.active=False AND that have
    at least one orphan row in any of the four target layers."""
    q = db.query(TenantConfig.tenant_id).filter(TenantConfig.active.is_(False))
    if tenant_filter:
        q = q.filter(TenantConfig.tenant_id == tenant_filter)
    candidate_tenant_ids = [t for (t,) in q.all()]

    orphan_tenant_ids: list[str] = []
    for tenant_id in candidate_tenant_ids:
        summary = inspect_tenant(db, tenant_id)
        if summary.total > 0:
            orphan_tenant_ids.append(tenant_id)
    return orphan_tenant_ids


def inspect_tenant(db, tenant_id: str) -> TenantOrphanSummary:
    """Read-only orphan count for one deactivated tenant."""
    summary = TenantOrphanSummary(tenant_id=tenant_id)

    summary.scope_assignment_orphans = (
        db.query(ScopeAssignment.id)
        .filter(
            ScopeAssignment.tenant_id == tenant_id,
            ScopeAssignment.active.is_(True),
        )
        .count()
    )
    summary.user_invite_orphans = (
        db.query(UserInvite.id)
        .filter(
            UserInvite.tenant_id == tenant_id,
            UserInvite.status == InviteStatus.PENDING,
        )
        .count()
    )
    summary.session_orphans = (
        db.query(SessionModel.id)
        .filter(
            SessionModel.tenant_id == tenant_id,
            SessionModel.status == "active",
        )
        .count()
    )
    # Synthetic-user orphans are derived from scope_assignment orphans
    # AFTER the layer-9 repair; on dry-run we estimate by inspecting
    # the current scope_assignment_orphans set.
    sa_user_ids = [
        uid
        for (uid,) in db.query(ScopeAssignment.user_id)
        .filter(
            ScopeAssignment.tenant_id == tenant_id,
            ScopeAssignment.active.is_(True),
        )
        .all()
    ]
    if sa_user_ids:
        synthetic_candidates = (
            db.query(User.id)
            .filter(
                User.id.in_(sa_user_ids),
                User.synthetic.is_(True),
                User.active.is_(True),
            )
            .all()
        )
        # Count those whose ONLY active scope_assignment is on this tenant.
        for (uid,) in synthetic_candidates:
            total_active = (
                db.query(ScopeAssignment.id)
                .filter(
                    ScopeAssignment.user_id == uid,
                    ScopeAssignment.active.is_(True),
                )
                .count()
            )
            other_tenant_active = (
                db.query(ScopeAssignment.id)
                .filter(
                    ScopeAssignment.user_id == uid,
                    ScopeAssignment.active.is_(True),
                    ScopeAssignment.tenant_id != tenant_id,
                )
                .count()
            )
            # If all active rows for this user are on this dead tenant,
            # they'll become an orphan once layer 9 runs.
            if total_active > 0 and other_tenant_active == 0:
                summary.synthetic_user_orphans += 1
    return summary


def repair_tenant(
    db, audit_ctx: AuditContext, tenant_id: str, *, verbose: bool
) -> TenantOrphanSummary:
    """Apply the 4-layer backfill repair to one tenant.

    Caller MUST commit (or rollback) the session after this returns.
    Each layer emits one audit row mirroring the in-function cascade.
    """
    summary = TenantOrphanSummary(tenant_id=tenant_id)
    repo = AdminAuditRepository(db)

    # ---- Layer 9: scope_assignments ---------------------------------
    affected_sa = (
        db.query(ScopeAssignment.id, ScopeAssignment.user_id)
        .filter(
            ScopeAssignment.tenant_id == tenant_id,
            ScopeAssignment.active.is_(True),
        )
        .all()
    )
    sa_pks = [pk for pk, _ in affected_sa]
    sa_user_ids = [uid for _, uid in affected_sa]
    if sa_pks:
        sa_updated = (
            db.query(ScopeAssignment)
            .filter(
                ScopeAssignment.tenant_id == tenant_id,
                ScopeAssignment.active.is_(True),
            )
            .update(
                {
                    ScopeAssignment.active: False,
                    ScopeAssignment.ended_at: func.now(),
                    ScopeAssignment.ended_reason: EndReason.DEACTIVATED,
                    ScopeAssignment.ended_by_api_key_id: None,
                },
                synchronize_session=False,
            )
        )
        summary.scope_assignment_orphans = int(sa_updated)
        repo.record(
            ctx=audit_ctx,
            tenant_id=tenant_id,
            action=ACTION_CASCADE_DEACTIVATE,
            resource_type=RESOURCE_SCOPE_ASSIGNMENT,
            resource_pk=None,
            resource_natural_id=None,
            after={
                "count": int(sa_updated),
                "affected_pks": sa_pks,
                "affected_user_ids": [str(uid) for uid in sa_user_ids],
                "table": "scope_assignments",
                "ended_reason": EndReason.DEACTIVATED.value,
                "trigger": "backfill_30a7",
            },
            note=(
                f"Backfill scope_assignments for tenant {tenant_id} "
                f"(Step 30a.7 cascade-integrity repair)"
            ),
            autocommit=False,
        )
        if verbose:
            logger.info(
                "  Layer 9 (scope_assignments): %d row(s) repaired",
                sa_updated,
            )

    # ---- Layer 10: user_invites -------------------------------------
    affected_invites = (
        db.query(UserInvite.id)
        .filter(
            UserInvite.tenant_id == tenant_id,
            UserInvite.status == InviteStatus.PENDING,
        )
        .all()
    )
    ui_pks = [pk for (pk,) in affected_invites]
    if ui_pks:
        ui_updated = (
            db.query(UserInvite)
            .filter(
                UserInvite.tenant_id == tenant_id,
                UserInvite.status == InviteStatus.PENDING,
            )
            .update(
                {
                    UserInvite.status: InviteStatus.REVOKED,
                    UserInvite.revoked_at: func.now(),
                },
                synchronize_session=False,
            )
        )
        summary.user_invite_orphans = int(ui_updated)
        repo.record(
            ctx=audit_ctx,
            tenant_id=tenant_id,
            action=ACTION_INVITE_REVOKED,
            resource_type=RESOURCE_USER_INVITE,
            resource_pk=None,
            resource_natural_id=None,
            after={
                "count": int(ui_updated),
                "affected_pks": ui_pks,
                "table": "user_invites",
                "revoked_via": "backfill_30a7",
                "trigger": "backfill_30a7",
            },
            note=(
                f"Backfill user_invites revoke for tenant {tenant_id} "
                f"(Step 30a.7 cascade-integrity repair)"
            ),
            autocommit=False,
        )
        if verbose:
            logger.info(
                "  Layer 10 (user_invites): %d row(s) revoked",
                ui_updated,
            )

    # ---- Layer 11: sessions -----------------------------------------
    affected_sessions = (
        db.query(SessionModel.id)
        .filter(
            SessionModel.tenant_id == tenant_id,
            SessionModel.status == "active",
        )
        .all()
    )
    sess_pks = [pk for (pk,) in affected_sessions]
    if sess_pks:
        sess_updated = (
            db.query(SessionModel)
            .filter(
                SessionModel.tenant_id == tenant_id,
                SessionModel.status == "active",
            )
            .update(
                {SessionModel.status: "revoked"},
                synchronize_session=False,
            )
        )
        summary.session_orphans = int(sess_updated)
        repo.record(
            ctx=audit_ctx,
            tenant_id=tenant_id,
            action=ACTION_CASCADE_DEACTIVATE,
            resource_type=RESOURCE_SESSION,
            resource_pk=None,
            resource_natural_id=None,
            after={
                "count": int(sess_updated),
                "affected_pks": [str(pk) for pk in sess_pks],
                "table": "sessions",
                "previous_status": "active",
                "new_status": "revoked",
                "trigger": "backfill_30a7",
            },
            note=(
                f"Backfill sessions revoke for tenant {tenant_id} "
                f"(Step 30a.7 cascade-integrity repair)"
            ),
            autocommit=False,
        )
        if verbose:
            logger.info(
                "  Layer 11 (sessions): %d row(s) revoked",
                sess_updated,
            )

    # ---- Layer 12: synthetic_orphan_users ---------------------------
    # Same narrow logic as in-function cascade: only flip a user if
    # synthetic=True AND zero remaining active scope_assignments.
    if sa_user_ids:
        synthetic_candidates = (
            db.query(User.id)
            .filter(
                User.id.in_(sa_user_ids),
                User.synthetic.is_(True),
                User.active.is_(True),
            )
            .all()
        )
        for (uid,) in synthetic_candidates:
            remaining = (
                db.query(ScopeAssignment.id)
                .filter(
                    ScopeAssignment.user_id == uid,
                    ScopeAssignment.active.is_(True),
                )
                .count()
            )
            if remaining == 0:
                (
                    db.query(User)
                    .filter(User.id == uid)
                    .update(
                        {User.active: False},
                        synchronize_session=False,
                    )
                )
                summary.synthetic_user_orphans += 1
                repo.record(
                    ctx=audit_ctx,
                    tenant_id=tenant_id,
                    action=ACTION_CASCADE_DEACTIVATE,
                    resource_type=RESOURCE_USER,
                    resource_pk=None,
                    resource_natural_id=str(uid),
                    after={
                        "user_id": str(uid),
                        "synthetic": True,
                        "remaining_active_scopes": 0,
                        "table": "users",
                        "trigger": "backfill_30a7",
                    },
                    note=(
                        f"Backfill deactivate synthetic orphan user "
                        f"{uid} for tenant {tenant_id} "
                        f"(Step 30a.7 cascade-integrity repair)"
                    ),
                    autocommit=False,
                )
                if verbose:
                    logger.info(
                        "  Layer 12 (synthetic_orphan_users): user %s deactivated",
                        uid,
                    )

    return summary


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Step 30a.7 cascade-orphan backfill. Default is dry-run; "
            "pass --apply to write rows."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the repair (writes rows + audit). Without this flag, "
        "the script runs in read-only dry-run mode.",
    )
    parser.add_argument(
        "--tenant",
        type=str,
        default=None,
        help="Scope to a single tenant_id (e.g. co-354c5056). Default: "
        "all deactivated tenants with orphans.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Per-layer per-row logging.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        help="Logging level (DEBUG, INFO, WARNING, ERROR). Default INFO.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    mode = "APPLY" if args.apply else "DRY-RUN"
    logger.info("=" * 70)
    logger.info("Step 30a.7 cascade-orphan backfill -- mode=%s", mode)
    if args.tenant:
        logger.info("Scoped to tenant: %s", args.tenant)
    logger.info("=" * 70)

    db = SessionLocal()
    exit_code = 0
    try:
        orphan_tenants = enumerate_orphan_tenants(db, args.tenant)
        logger.info(
            "Found %d deactivated tenant(s) with orphan privilege rows.",
            len(orphan_tenants),
        )
        if not orphan_tenants:
            logger.info("Nothing to backfill. Exiting cleanly.")
            return 0

        total_orphans = 0
        total_repaired = 0
        audit_ctx = AuditContext.system(label=BACKFILL_ACTOR_LABEL)

        for tenant_id in orphan_tenants:
            if not args.apply:
                # Dry-run: report only.
                preview = inspect_tenant(db, tenant_id)
                logger.info(
                    "[dry-run] tenant=%s scope_assignments=%d "
                    "user_invites=%d sessions=%d synthetic_users=%d "
                    "TOTAL=%d",
                    preview.tenant_id,
                    preview.scope_assignment_orphans,
                    preview.user_invite_orphans,
                    preview.session_orphans,
                    preview.synthetic_user_orphans,
                    preview.total,
                )
                total_orphans += preview.total
            else:
                # Apply mode.
                logger.info("[apply]  tenant=%s repairing...", tenant_id)
                try:
                    summary = repair_tenant(
                        db, audit_ctx, tenant_id, verbose=args.verbose
                    )
                    db.commit()
                    logger.info(
                        "[apply]  tenant=%s scope_assignments=%d "
                        "user_invites=%d sessions=%d synthetic_users=%d "
                        "TOTAL=%d (committed)",
                        summary.tenant_id,
                        summary.scope_assignment_orphans,
                        summary.user_invite_orphans,
                        summary.session_orphans,
                        summary.synthetic_user_orphans,
                        summary.total,
                    )
                    total_repaired += summary.total
                except Exception as exc:
                    db.rollback()
                    logger.error(
                        "[apply]  tenant=%s FAILED -- rolled back. error=%s",
                        tenant_id,
                        exc,
                    )
                    exit_code = 2
                    # Continue with remaining tenants; partial repair is OK.

        logger.info("=" * 70)
        if args.apply:
            logger.info(
                "Backfill complete. Tenants processed=%d, rows repaired=%d.",
                len(orphan_tenants),
                total_repaired,
            )
        else:
            logger.info(
                "Dry-run complete. Tenants with orphans=%d, total orphan rows=%d.",
                len(orphan_tenants),
                total_orphans,
            )
            logger.info("To apply, re-run with --apply.")
        logger.info("=" * 70)
    except Exception as exc:
        logger.exception("Fatal error during backfill: %s", exc)
        return 2
    finally:
        db.close()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
