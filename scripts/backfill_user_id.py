"""
Step 24.5b -- one-shot backfill for agents.user_id and
memory_items.actor_user_id.

Idempotent. Re-runnable. Audit-row-emitting. Handles three populations
in two phases:

Phase A -- agents.user_id backfill:
  For every Agent row where user_id IS NULL:
    1. If contact_email is non-NULL: synthesize email = contact_email,
       synthetic=False (real human identity claimed by the brokerage
       at Agent creation time).
    2. If contact_email is NULL: synthesize email =
       agent-{agent_id}@{tenant_id}.luciel.local, synthetic=True
       (auto-generated stub for Step 23 Option B onboarding rows
       that never carried an email).
    3. Call UserService.get_or_create_by_email(...) -- returns
       (user, was_created).
    4. Set agent.user_id = user.id.
    5. Audit row: ACTION_UPDATE + RESOURCE_AGENT, before
       {"user_id": null}, after {"user_id": str(user.id)}, note
       describing the synthesis path.

Phase B -- memory_items.actor_user_id backfill:
  For every MemoryItem row where actor_user_id IS NULL:
    1. Resolve the bound Agent via (tenant_id, agent_id) natural key.
    2. If Agent exists and has user_id: set
       memory_item.actor_user_id = agent.user_id.
    3. If Agent doesn't exist (orphan memory) or has no user_id
       (Phase A backfill incomplete): log warning, leave NULL.
       Reported in the summary as orphans skipped.

Phase A must complete before Phase B runs (Phase B reads agents.user_id
which Phase A populates). The script enforces this; --phase b without
--skip-residual-check will refuse to start if Phase A residuals remain.

Usage:
    # Local development -- dry-run first
    python -m scripts.backfill_user_id --dry-run --verbose

    # Local development -- live run
    python -m scripts.backfill_user_id

    # Production -- dry-run via ECS exec
    python -m scripts.backfill_user_id --dry-run

    # Production -- live run via ECS exec, after dry-run sanity check
    python -m scripts.backfill_user_id

    # Operator wants to run only Phase A (for partial-failure recovery)
    python -m scripts.backfill_user_id --phase a

    # Smoke against first 10 rows only
    python -m scripts.backfill_user_id --limit 10 --dry-run

Exit codes:
    0  -- all targets backfilled, residual NULL counts are zero
    1  -- residual NULL rows remain (script printed details, operator
          should investigate orphans before File 3.8 NOT NULL flip
          migration runs)
    2  -- fatal error (DB unavailable, dependency missing, etc.)

Step 24.5b drift D6 resolution: User binding to new Agents was
deferred from service-layer onboarding to this one-shot backfill
(Option D). Once File 3.8's migration flips agents.user_id to NOT
NULL, the service layer's NULL-tolerant paths become dead code and
this script becomes a one-time historical artifact.
"""
from __future__ import annotations

import argparse
import logging
import sys
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.admin_audit_log import (
    ACTION_UPDATE,
    RESOURCE_AGENT,
    RESOURCE_MEMORY,
)
from app.models.agent import Agent
from app.models.memory import MemoryItem
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
    SYSTEM_ACTOR_TENANT,
)
from app.services.user_service import UserService


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------
# Result accumulator for end-of-run summary
# --------------------------------------------------------------------

@dataclass
class BackfillStats:
    phase_a_agents_seen: int = 0
    phase_a_agents_backfilled: int = 0
    phase_a_users_created: int = 0
    phase_a_users_reused: int = 0
    phase_a_synthetic_count: int = 0
    phase_a_real_count: int = 0
    phase_b_memory_seen: int = 0
    phase_b_memory_backfilled: int = 0
    phase_b_orphans_no_agent: int = 0
    phase_b_orphans_no_user_id: int = 0
    phase_b_orphans_no_agent_id: int = 0
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Step 24.5b one-shot backfill for agents.user_id and "
            "memory_items.actor_user_id."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print what would change but don't write. Mandatory pre-flight "
            "for any production run."
        ),
    )
    p.add_argument(
        "--phase",
        choices=["a", "b", "both"],
        default="both",
        help=(
            "Which phase(s) to run. 'a' = agents.user_id only, "
            "'b' = memory_items.actor_user_id only, 'both' = a then b. "
            "Default: both."
        ),
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Rows per commit batch. Larger batches faster, smaller safer. Default 100.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after N rows backfilled per phase (smoke testing only).",
    )
    p.add_argument(
        "--skip-residual-check",
        action="store_true",
        help=(
            "Skip the Phase A residual check before Phase B. Use only "
            "when running phases on separate invocations and you know "
            "Phase A has already completed."
        ),
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="DEBUG-level logging.",
    )
    return p.parse_args()


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------

def _synthesize_email(*, contact_email: str | None, agent_id: str, tenant_id: str) -> tuple[str, bool]:
    """Decide which email and synthetic flag to use for an Agent.

    Returns (email, synthetic).
    """
    if contact_email and contact_email.strip():
        return contact_email.strip().lower(), False
    return f"agent-{agent_id}@{tenant_id}.luciel.local", True


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _build_audit_ctx() -> AuditContext:
    return AuditContext.system(label="step24.5b-backfill")


def _print_banner(title: str) -> None:
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)

# --------------------------------------------------------------------
# Phase A: agents.user_id backfill
# --------------------------------------------------------------------

def phase_a_backfill_agents(
    db: Session,
    *,
    dry_run: bool,
    batch_size: int,
    limit: int | None,
    stats: BackfillStats,
) -> None:
    """Walk every Agent with user_id IS NULL, bind to a User identity.

    Real-email Agents (contact_email set) bind to a real User
    (synthetic=False). Email-less Agents bind to a synthetic stub
    (synthetic=True, email=agent-{slug}@{tenant}.luciel.local) so
    PIPEDA flows can distinguish auto-generated rows from real users.

    Idempotent via UserService.get_or_create_by_email -- second run
    returns (existing_user, False) and skips creation.
    """
    user_service = UserService(db)
    audit_ctx = _build_audit_ctx()

    stmt = select(Agent).where(Agent.user_id.is_(None)).order_by(Agent.id.asc())
    if limit is not None:
        stmt = stmt.limit(limit)

    agents = list(db.scalars(stmt).all())
    stats.phase_a_agents_seen = len(agents)
    logger.info("Phase A: found %d Agents with user_id=NULL", len(agents))

    if not agents:
        logger.info("Phase A: nothing to do.")
        return

    pending_in_batch = 0

    for agent in agents:
        try:
            email, is_synthetic = _synthesize_email(
                contact_email=agent.contact_email,
                agent_id=agent.agent_id,
                tenant_id=agent.tenant_id,
            )

            if dry_run:
                logger.info(
                    "[dry-run] Phase A would bind agent.id=%d "
                    "(tenant=%s, agent_id=%s) -> email=%s synthetic=%s",
                    agent.id, agent.tenant_id, agent.agent_id,
                    email, is_synthetic,
                )
                stats.phase_a_agents_backfilled += 1
                if is_synthetic:
                    stats.phase_a_synthetic_count += 1
                else:
                    stats.phase_a_real_count += 1
                continue

            user, was_created = user_service.get_or_create_by_email(
                email=email,
                display_name=agent.display_name,
                synthetic=is_synthetic,
                audit_ctx=audit_ctx,
            )

            if was_created:
                stats.phase_a_users_created += 1
            else:
                stats.phase_a_users_reused += 1

            # Snapshot before so audit diff reflects only the change.
            before_user_id = None  # by definition NULL on this branch
            agent.user_id = user.id

            # Audit row in same txn as the column write (Invariant 4).
            AdminAuditRepository(db).record(
                ctx=audit_ctx,
                tenant_id=agent.tenant_id,
                action=ACTION_UPDATE,
                resource_type=RESOURCE_AGENT,
                resource_pk=agent.id,
                resource_natural_id=agent.agent_id,
                domain_id=agent.domain_id,
                agent_id=agent.agent_id,
                before={"user_id": before_user_id},
                after={"user_id": str(user.id)},
                note=(
                    f"step24.5b backfill: bound to user via "
                    f"{'synthetic' if is_synthetic else 'real'} "
                    f"email={email}"
                ),
                autocommit=False,
            )

            stats.phase_a_agents_backfilled += 1
            if is_synthetic:
                stats.phase_a_synthetic_count += 1
            else:
                stats.phase_a_real_count += 1
            pending_in_batch += 1

            if pending_in_batch >= batch_size:
                db.commit()
                logger.debug("Phase A: committed batch of %d", pending_in_batch)
                pending_in_batch = 0

        except Exception as exc:
            db.rollback()
            msg = (
                f"Phase A error on agent.id={agent.id} "
                f"(tenant={agent.tenant_id}, agent_id={agent.agent_id}): "
                f"{type(exc).__name__}: {exc}"
            )
            logger.error(msg)
            stats.errors.append(msg)
            pending_in_batch = 0
            # Continue with the next agent rather than aborting the run.

    if pending_in_batch > 0 and not dry_run:
        db.commit()
        logger.debug("Phase A: committed final batch of %d", pending_in_batch)


# --------------------------------------------------------------------
# Phase B: memory_items.actor_user_id backfill
# --------------------------------------------------------------------

def phase_b_backfill_memory(
    db: Session,
    *,
    dry_run: bool,
    batch_size: int,
    limit: int | None,
    stats: BackfillStats,
) -> None:
    """Walk every MemoryItem with actor_user_id IS NULL, bind through Agent.

    Resolves the bound Agent via (tenant_id, agent_id) natural key,
    reads agent.user_id, sets memory_item.actor_user_id. Three orphan
    classes tracked in stats:
      - memory.agent_id IS NULL              -> orphan_no_agent_id
      - Agent row not found at natural key   -> orphan_no_agent
      - Agent.user_id is NULL                -> orphan_no_user_id
        (means Phase A skipped that Agent or hit an error)

    Orphans are logged but do NOT abort the run -- the residual count
    surfaces at the end and File 3.8's NOT NULL flip migration's
    pre-flight will refuse to run if any remain.
    """
    audit_ctx = _build_audit_ctx()
    audit_repo = AdminAuditRepository(db)

    stmt = (
        select(MemoryItem)
        .where(MemoryItem.actor_user_id.is_(None))
        .order_by(MemoryItem.id.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    rows = list(db.scalars(stmt).all())
    stats.phase_b_memory_seen = len(rows)
    logger.info(
        "Phase B: found %d MemoryItem rows with actor_user_id=NULL",
        len(rows),
    )

    if not rows:
        logger.info("Phase B: nothing to do.")
        return

    pending_in_batch = 0

    for mem in rows:
        try:
            if mem.agent_id is None:
                stats.phase_b_orphans_no_agent_id += 1
                logger.debug(
                    "Phase B: skipping memory.id=%d -- agent_id=NULL "
                    "(pre-Step-24.5 row, no Agent binding to walk)",
                    mem.id,
                )
                continue

            agent = db.scalars(
                select(Agent).where(
                    Agent.tenant_id == mem.tenant_id,
                    Agent.agent_id == mem.agent_id,
                ).limit(1)
            ).first()

            if agent is None:
                stats.phase_b_orphans_no_agent += 1
                logger.warning(
                    "Phase B: memory.id=%d references missing Agent "
                    "(tenant=%s, agent_id=%s) -- orphan",
                    mem.id, mem.tenant_id, mem.agent_id,
                )
                continue

            if agent.user_id is None:
                stats.phase_b_orphans_no_user_id += 1
                logger.warning(
                    "Phase B: memory.id=%d -> agent.id=%d has user_id=NULL "
                    "(Phase A may have skipped or errored on this agent)",
                    mem.id, agent.id,
                )
                continue

            if dry_run:
                logger.info(
                    "[dry-run] Phase B would bind memory.id=%d -> "
                    "actor_user_id=%s (via agent.id=%d)",
                    mem.id, agent.user_id, agent.id,
                )
                stats.phase_b_memory_backfilled += 1
                continue

            before_actor = None  # by definition NULL on this branch
            mem.actor_user_id = agent.user_id

            audit_repo.record(
                ctx=audit_ctx,
                tenant_id=mem.tenant_id,
                action=ACTION_UPDATE,
                resource_type=RESOURCE_MEMORY,
                resource_pk=mem.id,
                resource_natural_id=f"memory_id={mem.id}",
                domain_id=None,  # MemoryItem doesn't carry domain_id directly
                agent_id=mem.agent_id,
                luciel_instance_id=mem.luciel_instance_id,
                before={"actor_user_id": before_actor},
                after={"actor_user_id": str(agent.user_id)},
                note=(
                    f"step24.5b backfill: resolved via agent.id={agent.id}"
                ),
                autocommit=False,
            )

            stats.phase_b_memory_backfilled += 1
            pending_in_batch += 1

            if pending_in_batch >= batch_size:
                db.commit()
                logger.debug("Phase B: committed batch of %d", pending_in_batch)
                pending_in_batch = 0

        except Exception as exc:
            db.rollback()
            msg = (
                f"Phase B error on memory.id={mem.id}: "
                f"{type(exc).__name__}: {exc}"
            )
            logger.error(msg)
            stats.errors.append(msg)
            pending_in_batch = 0

    if pending_in_batch > 0 and not dry_run:
        db.commit()
        logger.debug("Phase B: committed final batch of %d", pending_in_batch)


# --------------------------------------------------------------------
# Residual NULL check
# --------------------------------------------------------------------

def count_residual_nulls(db: Session) -> tuple[int, int]:
    """Return (agent_residual, memory_residual) NULL counts after backfill."""
    agent_residual = db.scalars(
        select(Agent.id).where(Agent.user_id.is_(None))
    ).all()
    memory_residual = db.scalars(
        select(MemoryItem.id).where(MemoryItem.actor_user_id.is_(None))
    ).all()
    return len(agent_residual), len(memory_residual)
# --------------------------------------------------------------------
# Summary printer
# --------------------------------------------------------------------

def print_summary(
    *,
    stats: BackfillStats,
    dry_run: bool,
    agent_residual: int,
    memory_residual: int,
) -> None:
    """Print the end-of-run summary banner. Mirrors the structured-stdout
    pattern from scripts/mint_platform_admin_ssm.py."""
    _print_banner(
        "STEP 24.5b BACKFILL SUMMARY"
        + (" [DRY-RUN]" if dry_run else "")
    )
    print()
    print("  Phase A -- agents.user_id")
    print(f"    seen          : {stats.phase_a_agents_seen}")
    print(f"    backfilled    : {stats.phase_a_agents_backfilled}")
    print(f"      real users   : {stats.phase_a_real_count}")
    print(f"      synthetic    : {stats.phase_a_synthetic_count}")
    print(f"    users created : {stats.phase_a_users_created}")
    print(f"    users reused  : {stats.phase_a_users_reused}")
    print()
    print("  Phase B -- memory_items.actor_user_id")
    print(f"    seen                : {stats.phase_b_memory_seen}")
    print(f"    backfilled          : {stats.phase_b_memory_backfilled}")
    print(f"    orphans (no agent_id) : {stats.phase_b_orphans_no_agent_id}")
    print(f"    orphans (agent missing): {stats.phase_b_orphans_no_agent}")
    print(f"    orphans (no user_id)  : {stats.phase_b_orphans_no_user_id}")
    print()
    print("  Errors")
    if stats.errors:
        for msg in stats.errors[:20]:
            print(f"    - {msg}")
        if len(stats.errors) > 20:
            print(f"    ... and {len(stats.errors) - 20} more")
    else:
        print("    none")
    print()
    print("  Residual NULL counts (post-run)")
    print(f"    agents.user_id IS NULL              : {agent_residual}")
    print(f"    memory_items.actor_user_id IS NULL  : {memory_residual}")
    print()
    print("=" * 72)
    if dry_run:
        print("DRY-RUN: no rows were written. Re-run without --dry-run to apply.")
    elif agent_residual == 0 and memory_residual == 0:
        print("ALL CLEAR: zero residuals. File 3.8 NOT NULL flip migration is unblocked.")
    else:
        print(
            "RESIDUALS REMAIN: investigate orphans before running the "
            "File 3.8 NOT NULL flip migration. Exit code 1."
        )
    print("=" * 72)


# --------------------------------------------------------------------
# Main
# --------------------------------------------------------------------

def main() -> int:
    args = parse_args()
    _setup_logging(args.verbose)

    _print_banner(
        "STEP 24.5b BACKFILL"
        + (" [DRY-RUN]" if args.dry_run else "")
    )
    print(f"  phase            : {args.phase}")
    print(f"  batch_size       : {args.batch_size}")
    print(f"  limit            : {args.limit if args.limit is not None else 'no limit'}")
    print(f"  dry_run          : {args.dry_run}")
    print(f"  skip_residual_check (between phases): {args.skip_residual_check}")
    print()

    db = SessionLocal()
    stats = BackfillStats()

    try:
        # ---- Phase A ----
        if args.phase in ("a", "both"):
            logger.info("Starting Phase A.")
            phase_a_backfill_agents(
                db,
                dry_run=args.dry_run,
                batch_size=args.batch_size,
                limit=args.limit,
                stats=stats,
            )
            logger.info("Phase A complete.")

        # ---- Inter-phase gate ----
        # Phase B reads agents.user_id which Phase A populates. If
        # Phase A residuals remain, Phase B can't safely run.
        if args.phase == "both" and not args.skip_residual_check:
            if not args.dry_run:
                a_residual, _ = count_residual_nulls(db)
                if a_residual > 0:
                    logger.warning(
                        "Phase A left %d agents.user_id residuals. "
                        "Phase B will skip memory rows whose Agent has "
                        "no user_id. Run --phase a separately to fix "
                        "before --phase b, or pass --skip-residual-check "
                        "to acknowledge the gap.",
                        a_residual,
                    )

        # ---- Phase B ----
        if args.phase in ("b", "both"):
            if args.phase == "b" and not args.skip_residual_check and not args.dry_run:
                a_residual, _ = count_residual_nulls(db)
                if a_residual > 0:
                    print(
                        f"FATAL: --phase b requested but Phase A has "
                        f"{a_residual} agents.user_id residuals. "
                        f"Run --phase a first, or pass "
                        f"--skip-residual-check to override.",
                        file=sys.stderr,
                    )
                    db.close()
                    return 2

            logger.info("Starting Phase B.")
            phase_b_backfill_memory(
                db,
                dry_run=args.dry_run,
                batch_size=args.batch_size,
                limit=args.limit,
                stats=stats,
            )
            logger.info("Phase B complete.")

        # ---- Final residual check + summary ----
        agent_residual, memory_residual = count_residual_nulls(db)
        print_summary(
            stats=stats,
            dry_run=args.dry_run,
            agent_residual=agent_residual,
            memory_residual=memory_residual,
        )

        # Exit code semantics:
        # - dry-run always returns 0 regardless of residuals (operator
        #   is iterating; residuals are informational, not failure).
        # - live run returns 0 only if both residual counts are zero
        #   (otherwise File 3.8's NOT NULL flip would fail at runtime,
        #   so we want this script's exit code to gate that step).
        if args.dry_run:
            return 0
        if agent_residual == 0 and memory_residual == 0:
            return 0
        return 1

    except Exception as exc:
        # Top-level fatal error -- DB unavailable, missing dependency,
        # etc. Distinct from per-row errors which are caught inside the
        # phases and accumulated into stats.errors.
        logger.exception("FATAL: %s", type(exc).__name__)
        print(
            f"FATAL: backfill aborted: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return 2

    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())