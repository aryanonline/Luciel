"""Step 30a owner-scope backfill: mint owner ScopeAssignments for existing self-serve buyers.

Revision ID: b4d8a2e7c1f3
Revises: e7b2c9d4a18f
Create Date: 2026-05-17

Why this migration exists
-------------------------

Drift D-step-30a-owner-scopeassignment-missing-self-serve-checkout-2026-05-17.

Step 30a's `TierProvisioningService.premint_for_tier` minted `User`,
`LucielInstance(s)`, and a primary `Agent` for every self-serve buyer
post-Stripe-checkout. It did NOT mint a `ScopeAssignment` binding that
User to (tenant, default-domain). The data model since Step 24.5b has
treated `scope_assignments` as the canonical "user X holds role R in
scope (tenant, domain)" record -- every cookied admin route's auth-gate
resolves the actor's tenant by looking up an active assignment on
`scope_assignments`. Without an assignment row, the buyer (the owner of
their own tenant) hits 403 "Cookied user has no active scope assignment"
on every cookied admin call -- caught on /app/team's first real
"Send invite" click 2026-05-17.

The forward-fix is in `app/services/tier_provisioning_service.py`
(commit D1, same drift): every new self-serve buyer from this deploy
onward gets an owner-role scope row inside the same transaction as
their primary Agent, audited the normal way via AdminAuditRepository.
This migration is the BACKFILL leg -- it mints the missing scope rows
for every buyer who already exists pre-deploy.

Forward-fix and backfill are split into two commits because the
forward-fix is code (catches every new signup the instant the new
image rolls out) and the backfill is data (writes one row per
existing owner, idempotently, in a single transaction). Splitting
them lets us validate each surface independently.

Why no admin_audit_logs row from this migration
-----------------------------------------------

`admin_audit_logs.row_hash` and `prev_row_hash` are NOT NULL post
Step 29.y Cluster 3 (D-8). They are populated by a SQLAlchemy session
event registered in `app.repositories.audit_chain` -- NOT by a DB
trigger. A raw `sa.text("INSERT INTO admin_audit_logs ...")` from
Alembic bypasses the session event entirely, which means either:
  (a) the INSERT fails on NOT NULL row_hash / prev_row_hash, OR
  (b) we hand-roll the hash chain in this migration -- which means
      duplicating the canonical_content serialiser, the sha256
      computation, the prev-row lookup, and the prev_row_hash chain
      math, in raw SQL. That's a security-critical path; reimplementing
      it in a migration is the exact kind of "bypass the session event
      so the chain has a hole in it" pattern that Pillar 23 exists to
      prevent.

The forensic trail for THIS data write is therefore captured by:
  * This migration file in git (durable, signed by commit hash).
  * The alembic_version row stamping the revision id.
  * The commit message on commit D2 referencing the drift label.
  * The drift entry D-step-30a-owner-scopeassignment-missing-self-
    serve-checkout-2026-05-17 in DRIFTS.md (filed under §3 at the
    time of plan-formation, mirrored to §5 at closure).

This is stronger than an `admin_audit_logs` row would be: a migration
sitting in git is cryptographically tied to every subsequent commit;
an audit row is just a database row. Future signups (forward-fix D1)
DO get admin_audit_logs rows via AdminAuditRepository the normal way
-- that path is untouched and remains Pillar 23 compliant.

What this migration writes
--------------------------

For every active `Agent` row that:
  1. Has a non-NULL `user_id` (i.e. is an actual buyer-anchored agent;
     pre-Step-24.5b synthetic agents with NULL user_id are excluded --
     they have no platform-user to bind a scope row to);
  2. Does NOT already have an active `ScopeAssignment` row pointing
     at the same `(user_id, tenant_id)` pair (idempotency: a teammate
     who already has a scope assignment under this tenant is left
     untouched; a redeliver of this migration on a tenant that was
     created post-fix sees no rows to mint).

...we INSERT one `scope_assignments` row with:
  * `user_id`     = `agents.user_id`
  * `tenant_id`   = `agents.tenant_id`
  * `domain_id`   = `agents.domain_id` (NOT a hardcoded constant -- we
                     follow the Agent's actual domain so this matches
                     the forward-fix's contract where the scope row
                     binds to the same domain the primary Agent lives
                     in; for every self-serve tenant minted via
                     OnboardingService this is "general", but the
                     migration is robust to operator-provisioned
                     tenants in non-default domains).
  * `role`        = 'owner' (the new role string; sibling to v1's
                     'teammate' and Step-30a.5's 'department_lead').
  * `active`      = true.

`started_at` is left implicit so the column's server_default of `now()`
fires; this matches every other ScopeAssignment row in the table and
avoids a clock-skew gotcha (the app server's `now()` vs the DB's
`now()` -- the column default uses the DB clock, which is the right
answer for an audit-grade timestamp).

Idempotency / safety
--------------------

The `NOT EXISTS` clause is the idempotency contract. Re-running this
migration on a database that already has owner-scope rows for every
buyer is a no-op -- zero rows inserted.

The migration runs inside Alembic's transactional DDL block (set by
`alembic env`). A failure on any row rolls back the entire backfill
(no partial state).

Downgrade
---------

The downgrade is intentionally a no-op. Once the forward-fix is in
place, removing the owner-scope rows would re-break every cookied
admin route. If you need to back out the change for some other
reason, revert commit D1 in `app/services/tier_provisioning_service.py`
first (so new signups don't keep adding owner-scope rows), then
write a separate cleanup migration with full operator review.

This is the same pattern as the Step 24.5b user-table migration's
downgrade comment ("destructive downgrade requires explicit
operator step").
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b4d8a2e7c1f3"
down_revision = "e7b2c9d4a18f"
branch_labels = None
depends_on = None


# Role string introduced by this migration. Mirrors the constant in
# app/services/tier_provisioning_service.py (_OWNER_ROLE = "owner");
# a shape test in tests/api/test_step30a_1_tiered_self_serve_shape.py
# pins the runtime constant, this migration pins the data.
_OWNER_ROLE = "owner"


def upgrade() -> None:
    """Mint owner-role ScopeAssignments for every existing buyer-anchored Agent.

    Single INSERT ... SELECT, idempotent via NOT EXISTS. See module
    docstring for the why-no-audit-row rationale.
    """
    bind = op.get_bind()

    bind.execute(
        sa.text(
            """
            INSERT INTO scope_assignments (
                user_id, tenant_id, domain_id, role, active
            )
            SELECT DISTINCT
                a.user_id,
                a.tenant_id,
                a.domain_id,
                :owner_role,
                true
            FROM agents a
            WHERE a.user_id IS NOT NULL
              AND a.active = true
              AND NOT EXISTS (
                SELECT 1
                FROM scope_assignments s
                WHERE s.user_id = a.user_id
                  AND s.tenant_id = a.tenant_id
                  AND s.active = true
                  AND s.ended_at IS NULL
              )
            """
        ),
        {"owner_role": _OWNER_ROLE},
    )


def downgrade() -> None:
    """No-op. See module docstring for the rationale.

    Removing the owner-scope rows post-fix would re-break every cookied
    admin route. A genuine rollback requires reverting commit D1 in
    `app/services/tier_provisioning_service.py` first, then writing a
    separate cleanup migration -- not this downgrade.
    """
    pass
