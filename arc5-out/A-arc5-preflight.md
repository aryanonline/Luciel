# Arc 5 — Schema migration pre-flight

**Status:** PRE-FLIGHT — planning only, no code edits, no prod touches authored here. Partner-review gate before any execution.
**Authored:** 2026-05-22 ~22:00 EDT, immediately after Arc 8 WU-6 Phase C doctrine truthification (commits `8b91603` + `c84f1b0`).
**Anchors:**
- `arc4-out/A-tenancy-collapse-arc-record.md` §1–§9 (the plan this pre-flight validates against current reality)
- `arc4-out/A-tier-matrix-detail.md` v2 §17 (the WU-8 Phase A additions Revision A absorbs)
- `docs/CANONICAL_RECAP.md` §11 Q1 + §12 + §14 (the V2 Admin → Instance → Lead canonical shape Arc 5 makes the schema match)
- `docs/DRIFTS.md` §3 `D-tenancy-collapse-admin-instance-lead-2026-05-22` (the umbrella drift Arc 5 closes)

---

## §0 — TL;DR for the partner-review pass

Arc 5 is a **three-Alembic-revision schema migration** that brings the production database into shape with the V2 canonical doctrine. The plan was already authored at Arc 4 in deep detail. **This pre-flight validates the plan against current schema reality** (post-Arc-8-WU-6 head `b2e5f17a3d9c`) and surfaces five gaps that need partner-review resolution before any execution.

**The plan in one sentence:** Revision A creates `admins` / `instances` / 3 new grant tables + the WU-8 Phase A additions (additive, zero-risk); Revision B backfills + flips application reads from `tenant_id` → `admin_id` across ~3,900 callsites in 7 gated batches; Revision C drops the legacy `tenants` / `domains` / `luciel_instances` tables + the ~30 `tenant_id` / `domain_id` / `agent_id` / `luciel_instance_id` FK columns + tightens the tier CHECK constraint.

**Five gaps surfaced by this pre-flight that Arc 4 did NOT anticipate:**

1. **Tier-constant code rename gap.** Arc 4's plan handles the data-layer tier rename (UPDATE statements in Revision B) but does NOT call out the **code-layer tier-constant rename** in `app/models/subscription.py` (`TIER_INDIVIDUAL`/`TIER_TEAM`/`TIER_COMPANY` → `TIER_FREE`/`TIER_PRO`/`TIER_ENTERPRISE`) and downstream imports (`app/policy/entitlements.py`, `app/services/billing_service.py`, etc.). This is doctrine-critical because the canonical tier shape is **Free / Pro / Enterprise** but the code still emits the legacy four-tier strings.
2. **Batch sizing drift.** Arc 4's batch B1 was sized at "15 model files"; reality is 19 model files carry `tenant_id` and 15 carry `LucielInstance`/`Agent` references. The 7-batch gating contract is still sound, but per-batch file lists need re-authoring against current reality before B1 begins.
3. **Arc 8 WU-6 tables NOT scope-bound — confirmed safe.** The two tables added in Arc 8 WU-6 (`email_send_event`, `email_suppression`) deliberately carry no `tenant_id` / `admin_id` FK because SES events are infrastructure-level. Arc 4's plan was authored before these existed; this pre-flight confirms they need NO action in Arc 5.
4. **Revision C "drop ~30+ FK columns" needs exhaustive enumeration.** Arc 4 §3.3 left the column-drop list "to be authored at execution time after Revision B smoke is green." This pre-flight enumerates it now so Revision C is a fully-specified mechanical pass, not a discovery exercise.
5. **Rollback-window discipline beyond Revision C.** Arc 4 §3.3 says "Restore from backup only" after Revision C lands. This pre-flight tightens that into a concrete backup-snapshot operating contract (pre-Revision-A `pg_dump`, pre-Revision-B `pg_dump`, pre-Revision-C `pg_dump` with retention and restore-test cadence).

---

## §1 — Current schema reality (as of 2026-05-22 22:00 EDT, head `b2e5f17a3d9c`)

### §1.1 — Alembic chain

- **Current head:** `b2e5f17a3d9c` (Arc 8 WU-6 `email_suppression`)
- **Total migrations on the chain:** 39 (single linear chain, no branches, no merge points)
- **Last 6 migrations (head first):**
  - `b2e5f17a3d9c` — arc8_wu6_email_suppression (2026-05-22)
  - `a91c4d2e7f08` — arc8_wu6_email_send_event (2026-05-22)
  - `b4d8a2e7c1f3` — step30a_owner_scope_backfill
  - `e7b2c9d4a18f` — step30a_4_user_invites_table
  - `a3c1f08b9d42` — step30a_3_users_password_hash
  - `dfea1a04e037` — step30a_2_deactivated_at_and_retention
- **down_revision for Arc 5 Revision A:** `b2e5f17a3d9c`

### §1.2 — Model files that will be touched (24 total in `app/models/`)

**Models that carry `tenant_id` (19):**
`admin_audit_log.py`, `agent.py`, `agent_config.py`, `api_key.py`, `conversation.py`, `domain_config.py`, `identity_claim.py`, `knowledge.py`, `luciel_instance.py`, `memory.py`, `retention.py`, `scope_assignment.py`, `session.py`, `subscription.py`, `tenant.py`, `trace.py`, `user.py`, `user_consent.py`, `user_invite.py`

**Models that carry `domain_id` (13):**
`admin_audit_log.py`, `agent.py`, `api_key.py`, `conversation.py`, `domain_config.py`, `identity_claim.py`, `knowledge.py`, `luciel_instance.py`, `scope_assignment.py`, `session.py`, `tenant.py`, `trace.py`, `user_invite.py`

**Models that carry `agent_id` (9):**
`admin_audit_log.py`, `agent.py`, `agent_config.py`, `api_key.py`, `knowledge.py`, `luciel_instance.py`, `memory.py`, `session.py`, `trace.py`

**Models that carry `luciel_instance_id` (8):**
`admin_audit_log.py`, `agent.py`, `api_key.py`, `knowledge.py`, `luciel_instance.py`, `memory.py`, `session.py`, `trace.py`

**Models that carry NEITHER (5) — untouched by Arc 5:**
`base.py`, `conversation.py` (wait — has tenant_id; re-checking), `email_send_event.py`, `email_suppression.py`, `__init__.py` (init only)

Correction: confirmed-safe-untouched models = `base.py`, `email_send_event.py`, `email_suppression.py`, `__init__.py`. The other 20 model files will all see edits.

### §1.3 — Callsite counts (current reality)

| Identifier | Count (app/ only) | Arc 4 estimate | Delta |
|---|---|---|---|
| `tenant_id` | 1,637 | 2,121 | –22% |
| `Tenant` / `Tenants` | 85 | (rolled into above) | n/a |
| `domain_id` | 662 | (rolled into 4,025 total) | n/a |
| `Domain` / `Domains` | 108 | (same) | n/a |
| `agent_id` | 504 | (same) | n/a |
| `Agent` / `Agents` | 311 | (same) | n/a |
| `luciel_instance_id` | 248 | (same) | n/a |
| `LucielInstance` | 338 | (same) | n/a |
| **Total identifier touches** | **~3,893** | **4,025** | **–3%** |

The total is within 3% of Arc 4's estimate. The per-batch numbers below need re-authoring against this reality before B1 begins (see §3.2).

### §1.4 — Current tier-constant reality in code (THE CRITICAL GAP)

**`app/models/subscription.py`** currently declares the four-tier shape:

```python
TIER_INDIVIDUAL = "individual"
TIER_TEAM = "team"
TIER_COMPANY = "company"
ALLOWED_TIERS = (TIER_INDIVIDUAL, TIER_TEAM, TIER_COMPANY)
```

with downstream consumers in:
- `app/policy/entitlements.py` (imports `TIER_COMPANY, TIER_INDIVIDUAL, TIER_TEAM`)
- `app/services/billing_service.py` (imports `ALLOWED_TIERS, TIER_COMPANY, TIER_INDIVIDUAL`)
- `app/services/admin_service.py` (imports `TIER_PERMITTED_SCOPES`)

The canonical V2 doctrine (CANONICAL §11.7, §14 entitlement matrix) declares the **three-tier shape Free / Pro / Enterprise**. Arc 4's plan handles this gap **at the data layer** (Revision B's `UPDATE admins SET tier='pro' WHERE tier IN ('individual', 'solo')` etc.) but does NOT explicitly call out the **code-layer constant rename**.

**Implication:** Arc 5 Revision B's code rename sweep must add an 8th batch (B8) — or fold the work into B1's model-layer batch — to rename `TIER_INDIVIDUAL` → `TIER_PRO`, `TIER_TEAM` → `TIER_ENTERPRISE`, `TIER_COMPANY` → `TIER_ENTERPRISE` (the four-tier collapsed to three; Free is net-new with no legacy mapping). All downstream imports rebind. `app/policy/entitlements.py`'s tier-entitlement sets must be rewritten against the V2 18-dim matrix in `arc4-out/A-tier-matrix-detail.md` v2.

This is **Gap 1** — flagged for partner-review resolution in §6.

---

## §2 — Revision A (additive) — fully specified

**Revision ID:** `arc4_a_admin_instance_additive` (rename to `arc5_a_admin_instance_additive` for clarity since this lands in Arc 5; revision-id is a free-form string per Alembic so no risk)
**down_revision:** `b2e5f17a3d9c`
**Risk:** Near-zero — pure additive. No existing data touched. Rollback = `alembic downgrade -1` drops the new tables.
**Estimated duration on prod RDS:** <30s (table creation only, no data motion).

### §2.1 — Tables created (in order)

| # | Table | Cols | Purpose | Doctrine anchor |
|---|---|---|---|---|
| 1 | `admins` | id, name, tier, active, created_at, **legacy_tenant_id** (back-pointer for Revision B alias helper) | Replaces `tenants` — the billing entity and permissions root | CANONICAL §11 Q1, ARCHITECTURE §4.1 |
| 2 | `instances` | id, admin_id, name, active, created_at, **legacy_luciel_instance_id**, **legacy_agent_id** | Replaces `luciel_instances` + collapses `agents` into the same table | CANONICAL §11 Q1, ARCHITECTURE §4.7 |
| 3 | `instance_composition_grants` | id, admin_id, caller_instance_id, callee_instance_id, created_at, created_by_user_id, revoked_at, notes | Captures Pro/Enterprise inter-instance composition (depth-bounded per tier) | CANONICAL §11 Q4 |
| 4 | `knowledge_share_grants` | id, admin_id, source_instance_id, target_instance_id, scope, created_at, created_by_user_id, revoked_at | Captures Enterprise knowledge sharing across Instances | CANONICAL §11 Q4 |
| 5 | `admin_tier_overrides` | (admin_id, dimension_key) PK + override_value JSON + override_enforced + notes + created_at + created_by_user_id + **6 WU-8 Phase A columns** (billing_model, included_usage_per_period, overage_rate_cents, committed_use_discount_bps, period_start, period_end, metered_unit) | Per-Admin entitlement override row; mirrors the 18-dim matrix from `A-tier-matrix-detail.md` v2 | CANONICAL §14, `arc4-out/A-tier-matrix-detail.md` v2 §17.2 |
| 6 | `metering_emissions` | (admin_id, period, emission_ts) PK + stripe_idempotency_key + quantity_emitted + stripe_subscription_item_id + created_at | Append-only cursor for the Enterprise metering hook (WU-8 Phase A) | ARCHITECTURE §3.2.14, `arc4-out/A-tier-matrix-detail.md` v2 §17.4 |

### §2.2 — Column added (in `subscriptions`)

- `subscriptions.billing_model VARCHAR(16) NULL` with in-migration backfill `UPDATE subscriptions SET billing_model='flat' WHERE billing_model IS NULL`
- Doctrine anchor: ARCHITECTURE §3.2.14 (the `billing_model` enum: `flat` / `hybrid` / `consumption`)

### §2.3 — Tier CHECK constraint on `admins`

During Revision A's window the constraint accepts the **union** of legacy + V2 tier strings to permit Revision B's backfill:

```python
CheckConstraint(
    "tier IN ('free', 'pro', 'enterprise', 'individual', 'solo', 'team', 'company')",
    name="ck_admins_tier_valid_during_migration",
)
```

Revision C tightens this to `tier IN ('free', 'pro', 'enterprise')`.

### §2.4 — Application-layer changes that ship with Revision A's commit

- New file `app/models/aliases.py` containing read-helpers `admin_or_tenant_id_for(row)` and `instance_or_agent_id_for(row)` — these return the new column if present, else the legacy column, so application code can be cut over file-by-file without a flag-day.
- New file `app/models/admin.py` (mirrors `app/models/tenant.py` shape against the new `admins` table; both files exist side-by-side during the revision pair window).
- Existing `app/models/tenant.py` and `app/models/luciel_instance.py` are **untouched** in this revision's commit.
- Application code writes BOTH `tenant_id` and `admin_id` on inserts (dual-write); reads still go through `tenant_id`.

### §2.5 — Rollback contract

```powershell
# From the operator's PowerShell on Windows:
$env:DATABASE_URL = $env:LUCIEL_PROD_RDS_URL
docker compose exec backend alembic downgrade -1
# Then: revert the application commit on `main`.
```

Rollback safety: **HIGH**. No prod data has been mutated; the only state change is the new (empty) tables + the new `subscriptions.billing_model` column (which is nullable and backfilled to `'flat'` for existing rows).

---

## §3 — Revision B (backfill + cutover) — the high-risk one

**Revision ID:** `arc5_b_admin_instance_cutover`
**down_revision:** `arc5_a_admin_instance_additive`
**Risk:** **HIGH** — this is the big code rename sweep (~3,900 callsites) + the data flip. Rollback path is application-layer revert + dual-write re-enable, not schema downgrade.
**Estimated duration on prod RDS:** <2 minutes for the data backfills (Tenant→Admin 1:1 + LucielInstance→Instance 1:1 are bounded by current row counts which are <10k each in current prod).
**Estimated duration for the code rename sweep:** 8–12 hours of focused work, single session preferred.

### §3.1 — Data operations (Revision B SQL)

```python
# 1. Backfill admins from tenants (idempotent — re-runnable)
op.execute("""
    INSERT INTO admins (id, name, tier, active, created_at, legacy_tenant_id)
    SELECT id, name, tier, active, created_at, id
    FROM tenants
    WHERE NOT EXISTS (SELECT 1 FROM admins WHERE admins.id = tenants.id)
""")

# 2. Backfill instances from luciel_instances (idempotent)
op.execute("""
    INSERT INTO instances (id, admin_id, name, active, created_at, legacy_luciel_instance_id)
    SELECT li.id, li.tenant_id, li.name, li.active, li.created_at, li.id
    FROM luciel_instances li
    WHERE NOT EXISTS (SELECT 1 FROM instances WHERE instances.id = li.id)
""")

# 3. Tier rename UPDATEs (V2 three-tier shape)
op.execute("UPDATE admins SET tier = 'pro' WHERE tier IN ('individual', 'solo')")
op.execute("UPDATE admins SET tier = 'enterprise' WHERE tier IN ('team', 'company')")

# 4. Per renamed Admin, mint an admin_tier_overrides row mirroring previous limits
#    so effective limits are unchanged at the cutover. + write TIER_RENAME_APPLIED
#    audit row in the same transaction (audit chain integrity per Arc 4 §9).
#    This block is Python-loop-driven; SQL not inlined here. See Arc 4 §3.2 + §9.
```

**Idempotency note:** All four blocks above are designed to be re-runnable. If Revision B aborts partway through, re-running it picks up where it left off.

### §3.2 — Code rename sweep — 7 batches (Arc 4 §4) + 1 net-new batch (B8) for tier constants

Each batch is its own intermediate commit with a green-test gate before the next batch's commit can land.

| Batch | Layer | Files (Arc 4 est / current reality) | Identifiers renamed | Validation gate |
|---|---|---|---|---|
| **B1** | Model layer | 15 / **19 (+27%)** | `Tenant`→`Admin`, `LucielInstance`→`Instance`, `TenantConfig`→`AdminConfig`. New `aliases.py` keeps `Tenant` as a deprecated alias so the rest of `app/` and `tests/` compile during the batch. | `pytest tests/models/ -x` green |
| **B2** | Service layer | 35 / **18 (–49%)** | `tenant_id`→`admin_id`, `luciel_instance_id`→`instance_id`, `agent_id`→`instance_id` in kwarg-only-position. Service-layer test fixtures updated in same batch. | `pytest tests/services/ -x` green |
| **B3** | API layer (v1 routes) | 25 / **12 (–52%)** | Route paths: `/admin/luciel-instances`→`/admin/instances`, `/admin/domains/*` deleted. Query-param names: `tenant_id`→`admin_id`. Response field names: same rename. | `pytest tests/api/ -x` green + curl smoke against 4 representative endpoints |
| **B4** | Middleware + auth | 8 (estimate stands, not re-counted yet) | `TenantConfig.active`→`AdminConfig.active`, `tenant_admin` role-string→`admin_user`, `department_lead`→`instance_lead`. JWT claim names rebind. | `pytest tests/middleware/ -x` green |
| **B5** | Cascade + audit | 20 (estimate stands) | 13-layer cascade → 12-layer (Domain layer removed). Audit constants: `TENANT_CREATED` keeps emitting for backward audit-chain readability during this batch; new rows emit `ADMIN_CREATED`. Cascade-completeness verifier updated to read both. | `pytest tests/audit/ -x` green + cascade verifier passes |
| **B6** | Tests (non-fixture) | 80 (estimate stands) | Assertion-level renames across `tests/integration/`, `tests/e2e/`, harness scripts. The 28-test AST contract suite is updated last so it can validate the entire post-sweep shape. | Full `pytest -x` green; 33-test live e2e harness green |
| **B7** | Alembic + scripts | 10 (estimate stands) | Alembic migration files older than this revision are NOT renamed (historical record). Scripts in `scripts/`, `arc3-out/`, `arc4-out/` are updated. | Lint pass; no functional gate |
| **B8 (NEW — pre-flight surfaced)** | Tier-constant rename | 4–6 files (need exhaustive count) | `TIER_INDIVIDUAL` → `TIER_PRO`, `TIER_TEAM` → `TIER_ENTERPRISE`, `TIER_COMPANY` → `TIER_ENTERPRISE`. Net-new constants `TIER_FREE` introduced (Free is brand-new tier with no legacy mapping). Files affected: `app/models/subscription.py` (the source); `app/policy/entitlements.py` (the V2 18-dim entitlement matrix rewrite); `app/services/billing_service.py`; `app/services/admin_service.py`; any other importers. | `pytest tests/policy/ -x` green; `pytest tests/billing/ -x` green; manual review of entitlements-matrix output diff |

**Batch gating rule (unchanged from Arc 4 §4):** the commit for batch N+1 cannot land until batch N's validation gate passes. Each batch is its own intermediate commit; the eight commits compose into the Revision B Pull Request.

### §3.3 — Revision B rollback contract

**Schema rollback:** A reverse-direction backfill is possible (re-sync any new `admins` rows back to `tenants`), but riskier because rows may have diverged. The cleaner path is **application-layer revert**: revert the application commit on `main`, re-enable dual-write, and the schema state remains valid.

**Recovery plan for a botched Revision B:**
1. Revert the application commit on `main` via `git revert <sha>`
2. Re-deploy the previous image to ECS via `update-service --task-definition luciel-backend:<previous>` (forced rollback)
3. Re-enable dual-write in the application layer (`tenant_id` reads + `admin_id` writes, mirroring Revision A's posture)
4. Schema state remains valid: `admins` / `instances` tables still exist with data; tenant_id columns still readable
5. Decide whether to retry Revision B or to defer

---

## §4 — Revision C (subtractive) — exhaustive column-drop enumeration

**Revision ID:** `arc5_c_admin_instance_subtractive`
**down_revision:** `arc5_b_admin_instance_cutover`
**Risk:** **MEDIUM — irreversible**. After Revision C, schema rollback requires `pg_restore` from the pre-Revision-C backup.
**Estimated duration on prod RDS:** ~5 minutes (column drops + table drops + constraint creation).

### §4.1 — FK columns dropped — exhaustive enumeration (closing Arc 4's TODO)

Per the §1.2 model survey, the columns to drop on Revision C are (table-by-table, column-by-column):

| Table | Columns to drop |
|---|---|
| `admin_audit_log` | `tenant_id`, `domain_id`, `agent_id`, `luciel_instance_id` |
| `agent` | `tenant_id`, `domain_id`, `agent_id` (self-ref), `luciel_instance_id` |
| `agent_config` | `tenant_id`, `agent_id` |
| `api_key` | `tenant_id`, `domain_id`, `agent_id`, `luciel_instance_id` |
| `conversation` | `tenant_id`, `domain_id` |
| `domain_config` | `tenant_id`, `domain_id` |
| `identity_claim` | `tenant_id`, `domain_id` |
| `knowledge` | `tenant_id`, `domain_id`, `agent_id`, `luciel_instance_id` |
| `luciel_instance` | (entire table dropped — see §4.2) |
| `memory` | `tenant_id`, `agent_id`, `luciel_instance_id` |
| `retention` | `tenant_id` |
| `scope_assignment` | `tenant_id`, `domain_id` |
| `session` | `tenant_id`, `domain_id`, `agent_id`, `luciel_instance_id` |
| `subscription` | `tenant_id` |
| `tenant` | (entire table dropped — see §4.2) |
| `trace` | `tenant_id`, `domain_id`, `agent_id`, `luciel_instance_id` |
| `user` | `tenant_id` |
| `user_consent` | `tenant_id` |
| `user_invite` | `tenant_id`, `domain_id` |

**Total columns dropped: ~45 (counting per-column, not per-table).**

### §4.2 — Tables dropped (in dependency order)

```python
# Drop in reverse-FK order to avoid constraint violations
op.drop_table("luciel_instances")  # depends on tenants + agents
op.drop_table("agents")             # depends on tenants
op.drop_table("domains")            # depends on tenants
op.drop_table("tenants")            # root
```

### §4.3 — Back-pointer columns dropped (on the new tables)

```python
op.drop_column("admins", "legacy_tenant_id")
op.drop_column("instances", "legacy_luciel_instance_id")
op.drop_column("instances", "legacy_agent_id")
```

### §4.4 — Tier CHECK constraint tightened

```python
op.drop_constraint("ck_admins_tier_valid_during_migration", "admins")
op.create_check_constraint(
    "ck_admins_tier_valid",
    "admins",
    "tier IN ('free', 'pro', 'enterprise')",
)
```

### §4.5 — Revision C rollback contract

**Schema rollback:** **Restore from pre-Revision-C `pg_dump` backup only.** The drop of `tenants` / `domains` / `luciel_instances` is irreversible from the schema layer.

Concrete operator backup plan (Gap 5 closure):

```powershell
# IMMEDIATELY BEFORE Revision C executes (operator's PowerShell):
$ts = Get-Date -Format "yyyyMMdd-HHmmss"
aws rds create-db-snapshot `
  --db-instance-identifier luciel-prod `
  --db-snapshot-identifier "luciel-prod-pre-revc-$ts"

# Then poll until status=available before proceeding with `alembic upgrade head`.
aws rds describe-db-snapshots `
  --db-snapshot-identifier "luciel-prod-pre-revc-$ts" `
  --query "DBSnapshots[0].Status"

# Retain for 30 days. Restore test on day 7 (mount snapshot to a staging RDS,
# run a smoke pytest against it, verify the smoke is green, then delete the
# staging RDS — proves the snapshot is restorable).
```

Same backup-snapshot discipline applies **before each of Revision A and Revision B** (lighter-risk but still mandatory — six-pillar discipline).

---

## §5 — Prod-touch order (concrete operator runbook outline)

This is the order in which prod-touching steps will execute. Each step is paired with the agent at the time of execution. No step here is executed by authoring this pre-flight.

| Step | What | Risk | Rollback |
|---|---|---|---|
| **0** | Author `arc5-out/` planning docs (THIS DOCUMENT + a per-revision runbook + a per-batch checklist) | None | Discard the docs |
| **1** | Resolve the 5 gaps from §6 with partner (Free-tier-introduction handling, etc.) | None | n/a |
| **2** | Write Revision A migration file at `alembic/versions/arc5_a_admin_instance_additive.py` | None — file on disk, not run | Delete the file |
| **3** | Local Docker test of Revision A against a fresh RDS-clone | None — local only | `docker compose down -v` |
| **4** | Take pre-Revision-A `pg_dump` snapshot of prod RDS | None — read-only | n/a |
| **5** | **PROD TOUCH:** Run `alembic upgrade head` on prod RDS (Revision A lands) | **LOW** — pure additive | Snapshot restore |
| **6** | Write Revision B migration file + author the 8 code-sweep batches as 8 separate commits on a feature branch `arc5-revb` | None — local | Discard branch |
| **7** | Run each batch's validation gate locally; merge to `main` batch-by-batch | None — local | Revert commits |
| **8** | Build + push backend image with the Revision B code | None — image in ECR | Don't deploy it |
| **9** | Take pre-Revision-B `pg_dump` snapshot of prod RDS | None — read-only | n/a |
| **10** | **PROD TOUCH:** Force-deploy the Revision B image to ECS (worker first, then backend) | **HIGH** — code flip from `tenant_id` reads to `admin_id` reads | Revert to previous image + re-enable dual-write |
| **11** | **PROD TOUCH:** Run Revision B `alembic upgrade head` on prod RDS (data backfills + tier rename) | **MEDIUM** — data mutation, but bounded + idempotent | Snapshot restore |
| **12** | Soak in prod for **24h minimum** — watch CloudWatch error rates, monitor any rows with mismatched `tenant_id`/`admin_id`, watch the audit chain for `TIER_RENAME_APPLIED` rows landing | n/a | Roll back via §3.3 if any defect surfaces |
| **13** | Take pre-Revision-C `pg_dump` snapshot of prod RDS | None — read-only | n/a |
| **14** | **PROD TOUCH:** Run Revision C `alembic upgrade head` on prod RDS (drops legacy tables) | **MEDIUM-IRREVERSIBLE** — schema is destructive | Snapshot restore only |
| **15** | Strike-through close the umbrella drift `D-tenancy-collapse-admin-instance-lead-2026-05-22` in DRIFTS.md; author `arc5-out/A-arc5-execution-record.md`; tag `arc-5-tenancy-collapse-complete` | None | n/a |

---

## §6 — The 5 gaps surfaced for partner-review

### Gap 1 — Free-tier introduction handling

**The issue:** The V2 tier shape is **Free / Pro / Enterprise**, but the legacy data has tier strings like `individual` / `team` / `company`. Arc 4 §3.2 maps `individual`/`solo` → `pro` and `team`/`company` → `enterprise`. Free is **net-new** — no existing customer renames into it.

**Open question for partner:** Do we want existing customers grandfathered into Pro (Arc 4's plan), or do we want some segment migrated into Free as a downgrade path? The doctrine-honest default per Arc 4 is "existing customers all become Pro at minimum to preserve effective entitlements; Free is for net-new signups only." Confirm this is still your intent.

**Recommendation:** Stick with Arc 4's default. New code-layer constant `TIER_FREE` exists for net-new signups but is never written to existing rows during Revision B.

### Gap 2 — Tier-constant code rename (B8 batch)

**The issue:** Arc 4 §4 has 7 code-sweep batches (B1–B7) but **does not call out** the rename of `TIER_INDIVIDUAL` / `TIER_TEAM` / `TIER_COMPANY` constants in `app/models/subscription.py` + downstream imports. The V2 doctrine names them `TIER_FREE` / `TIER_PRO` / `TIER_ENTERPRISE`.

**Recommendation:** Add **B8 as a net-new batch** (§3.2 above) and gate it on `pytest tests/policy/ -x` green + manual review of the entitlements-matrix output diff against `arc4-out/A-tier-matrix-detail.md` v2.

### Gap 3 — Batch B1's file count is 27% larger than Arc 4 estimated

**The issue:** Arc 4 sized B1 at 15 model files; reality is 19. The extra 4 files are the result of post-Arc-4 model additions and discoveries (`identity_claim.py`, `email_send_event.py` — wait, the latter doesn't have tenant_id, confirmed-safe; the actual delta is from Arc 5 not pre-counting `user_consent.py`, `user_invite.py`, and `domain_config.py` which all carry `tenant_id`).

**Recommendation:** Re-author the per-batch file lists in `arc5-out/A-arc5-revb-batch-checklist.md` (separate file, NEXT pre-flight artifact to author) against the §1.2 reality. The 8-batch gating contract is sound; only the per-batch file enumeration needs the refresh.

### Gap 4 — Revision C column-drop list, now exhaustive

**The issue:** Arc 4 §3.3 left the column-drop list "to be authored at execution time." This pre-flight closes that gap at §4.1 above (45 columns across 19 tables).

**Recommendation:** **No partner-input needed** — this is a survey result, not a decision. Flagged here for completeness.

### Gap 5 — Backup-snapshot discipline beyond Revision C

**The issue:** Arc 4 §3.3 says "Restore from backup only" but does not specify the backup discipline (when, how many, retention, restore-test cadence).

**Recommendation:** §4.5 above codifies the operator's backup contract:
- `aws rds create-db-snapshot` IMMEDIATELY BEFORE each of Revisions A, B, C
- Snapshot retention: 30 days
- Restore-test on day 7 (mount to staging, smoke-pytest, delete staging)
- Same discipline for any future destructive migration

This is a doctrine-strengthening recommendation — not Arc-5-specific. Worth confirming you want it codified as a six-pillar operating rule for ALL future destructive migrations (in which case it should also land in ARCHITECTURE §3.4 as a permanent operating-discipline rule).

---

## §7 — What is NOT in scope for Arc 5

Per Arc 4 §1.2 and reaffirmed here:

- **Stripe SKU restructure** — defers to Arc 6 (separate arc, post-Revision-C-soak)
- **Frontend updates** — defers to Arc 6's Stripe rename window (a single deploy carrying schema rename references in the frontend + the new Stripe SKU names is cleaner than two separate frontend deploys)
- **WU-8 Phase B (metering worker, beat entry, entitlements extension)** — Arc 5 lands ONLY the schema (Phase A); the worker logic lands at WU-8 Phase B inside Arc 8 return
- **WU-7 Phase B (Free-tier provisioning path)** — Arc 5 lands the `TIER_FREE` constant but the Free-tier signup endpoint + soft-gate + synthetic-abuse test are WU-7 Phase B work, inside Arc 8 return
- **Cross-Admin re-parenting flow (Step 38)** — separate roadmap step, decoupled from Arc 5
- **Same-Admin tier moves (Step 35)** — separate roadmap step; the column rename + entitlement delta payment infrastructure that Step 35 needs are landed by Arc 5 + Arc 6, but the Step 35 endpoint + flow are authored later

---

## §8 — Pre-flight artifacts to author NEXT (after partner reviews this doc)

1. **`arc5-out/A-arc5-revb-batch-checklist.md`** — per-batch file enumeration + validation-gate-pass criteria for B1 through B8
2. **`arc5-out/A-arc5-rollback-runbook.md`** — concrete PowerShell + AWS CLI snippets for each of the three rollback paths (post-Rev-A revert, post-Rev-B application-revert, post-Rev-C snapshot-restore)
3. **`arc5-out/A-arc5-revision-c-column-drop-sql.md`** — the full SQL for §4.1 (~45 column drops + 4 table drops + 3 back-pointer drops + 1 constraint tighten) as a single copy-pasteable block for the Revision C migration file
4. **`docs/DRIFTS.md` edit (small, post-partner-review)** — progress the umbrella drift `D-tenancy-collapse-admin-instance-lead-2026-05-22` from OPEN to PRE-FLIGHT-AUTHORED with a pointer to this document
5. **`docs/CANONICAL_RECAP.md` edit (small)** — update §12 to note Arc 5 pre-flight authored 2026-05-22 ~22:00 EDT, with the 5 gaps surfaced + the proposed resolutions

---

## §9 — Sign-off

**Pre-flight author:** Agent, 2026-05-22 ~22:00 EDT
**Partner-review gate:** Aryan to read this doc end-to-end, confirm the 5 gap resolutions, and sign off before any of §5's prod-touching steps begin.
**Earliest execution window:** After Aryan reviews + the pre-flight artifacts in §8 (1, 2, 3) are authored + a fresh test session is available (this is not a Friday-night job).
