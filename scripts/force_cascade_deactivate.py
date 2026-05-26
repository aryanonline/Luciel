"""One-shot admin-path cascade for a given admin_id.

Designed for ECS-exec execution from inside the backend container.
Calls AdminService.deactivate_tenant_with_cascade directly. Idempotent
on re-run (children already inactive are skipped by the existing repo
cascade methods).

Use this ONLY when:
  - The Stripe webhook path did NOT fire (e.g. cancel-at-period-end
    rather than cancel-immediate), AND
  - The tenant needs to be deactivated NOW rather than at period end.

Writes a manually-triggered cascade_deactivate audit row (no paired
subscription_cancel row, because we are not the Stripe webhook). The
audit chain row_hash stays continuous because cascade_deactivate is
a first-class action.

Usage (inside container):
    python /app/scripts/force_cascade_deactivate.py co-354c5056 \\
        --reason "Step 30a.6 pre-Pass-3 retire; Stripe dashboard refund left sub on cancel-at-period-end"
"""
from __future__ import annotations

import argparse
import sys
import uuid

from sqlalchemy.orm import Session

from app.core.database import get_engine
from app.repositories.admin_audit_repository import AuditContext
# Arc 5 Path A — AgentRepository deleted at Commit A5; V2 has no Agent layer.
from app.services.admin_service import AdminService
from app.services.instance_service import InstanceService


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("admin_id")
    parser.add_argument("--reason", required=True, help="audit-row note for the manual cascade")
    parser.add_argument("--actor-label", default="ops:force_cascade_deactivate")
    args = parser.parse_args()

    engine = get_engine()
    with Session(engine) as db:
        admin = AdminService(db)
        # Arc 5 Path A — agent_repo no longer exists; the V2 cascade
        # spine is admin-id-only. This script's `--tenant-id` arg is
        # interpreted as the V2 admin_id post-Revision-B backfill.
        luciel_service = InstanceService(db, admin_service=admin)

        ctx = AuditContext(
            actor_method="admin_script",
            actor_label=args.actor_label,
            actor_key_prefix=None,
            request_id=str(uuid.uuid4()),
            user_agent="force_cascade_deactivate.py",
            ip_address=None,
        )

        try:
            result = admin.deactivate_tenant_with_cascade(
                args.admin_id,
                audit_ctx=ctx,
                luciel_instance_service=luciel_service,
                agent_repo=None,
                updated_by=args.actor_label,
                autocommit=True,
            )
        except Exception as exc:
            print(f"verdict=CASCADE_RAISED exc={type(exc).__name__} msg={exc}", file=sys.stderr)
            db.rollback()
            return 2

        if result is True:
            print(f"verdict=CASCADE_APPLIED admin_id={args.admin_id}")
            return 0
        elif result is False:
            print(f"verdict=TENANT_NOT_FOUND admin_id={args.admin_id}")
            return 1
        else:
            print(f"verdict=UNEXPECTED_RETURN result={result!r}")
            return 3


if __name__ == "__main__":
    sys.exit(main())
