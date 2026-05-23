# Arc 5 — Commit 5 — Revision A Local Smoke Test Protocol

**Date opened:** 2026-05-23
**Partner-driven:** Yes (Windows + Docker box; sandbox has no Docker)
**Agent role:** Guide verification, interpret output, validate each checkpoint before next step

**Pre-conditions:**
- Local branch `main` at HEAD with Revision A migration + this protocol present
- Docker Desktop running on partner's Windows box, Linux engine, fully started
- PowerShell terminal at repo root `C:\Users\aryan\Projects\Business\Luciel\`
- Alembic head this protocol drives to: `arc5_a_admin_instance_additive`
- Prior revision: `b2e5f17a3d9c` (Arc 8 WU-6 email_suppression — already on prod)
- Standalone `luciel-postgres` container (5-week-old dev DB at `3dbbc70d0105`, 585+424+552 fixture rows) is allowed to remain running on host port **5432** — we do NOT touch it (D-dev-db-stale-5-weeks-behind-prod-2026-05-23 deferred)
- docker-compose.yml updated 2026-05-23 to use `pgvector/pgvector:pg16` (the `vector` extension is required by 9+ migrations between empty DB and `b2e5f17a3d9c`) and to bind compose db to host port **5433** (avoids the standalone container's 5432)

**Connection URL for ALL alembic + psql commands in this protocol:**
```
postgresql+psycopg://postgres:postgres@localhost:5433/luciel
```
The `5433` is critical — `5432` would hit your standalone dev container, NOT the fresh smoke-test DB.

**NO PROD TOUCHED. NO STANDALONE `luciel-postgres` TOUCHED. Smoke test runs against the compose-managed `db` service only, on host port 5433.**

---

## Goal

Verify Revision A `upgrade → downgrade -1 → upgrade` round-trips cleanly against a **fresh** pgvector/pgvector:pg16 container DB, with no leftover state between cycles. Proves:

1. `upgrade()` emits valid postgres DDL that executes without error
2. All 6 new tables + 1 column-add + 21 indexes + 8+ CHECK constraints materialize correctly
3. `downgrade()` reverses cleanly in strict FK-reverse order with no dangling objects
4. Re-applying `upgrade()` on a downgraded DB produces identical schema (idempotency of the round-trip)
5. The full 9-revision replay (empty → `b2e5f17a3d9c`) executes cleanly, including pgvector extension creation at migration `b0e003ffa07f`

If any step fails, **STOP and report the exact error to me before proceeding.** Do not improvise fixes.

---

## Step 0 — Fresh compose-managed container baseline (port 5433)

The standalone `luciel-postgres` container is allowed to keep running on 5432 — do not stop it.

```powershell
cd C:\Users\aryan\Projects\Business\Luciel
docker compose down -v
docker compose up -d db
```

`down -v` is critical — the `-v` strips the `luciel_pgdata` volume so we start from a truly empty `luciel` database in the compose-managed container. The standalone container's volume is unaffected (different project name).

Wait ~10s for the fresh pgvector image to initialize postgres (first pull may take 30-60s on initial run), then verify the compose db came up:

```powershell
docker compose ps
```

**Expected:** `db` service status `running` (or `healthy`), PORTS column shows `0.0.0.0:5433->5432/tcp`.

Then confirm it's empty AND that pgvector is available in the image:

```powershell
docker compose exec db psql -U postgres -d luciel -c "\dt"
docker compose exec db psql -U postgres -d luciel -c "SELECT * FROM pg_available_extensions WHERE name='vector';"
```

**Expected outputs:**
- First: `Did not find any relations.` (empty DB)
- Second: One row showing `name=vector`, `default_version` populated, `installed_version` NULL (available but not yet created — that happens during migration `b0e003ffa07f`)

→ Paste both outputs. If you see existing tables, the volume didn't clear — re-run `docker compose down -v` and re-create. If pg_available_extensions returns 0 rows for `vector`, the wrong image is being used — STOP.

---

## Step 1 — Apply all migrations up to Arc 8 WU-6 baseline (matches prod schema)

```powershell
$env:DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5433/luciel"
alembic upgrade b2e5f17a3d9c
```

This replays the full migration chain from empty → `b2e5f17a3d9c`, including:
- Initial schema migrations
- `b0e003ffa07f` which runs `CREATE EXTENSION IF NOT EXISTS vector` + creates `knowledge_embeddings.embedding vector(1536)` + ivfflat index — REQUIRES pgvector image
- The 8 Step-30a + Arc 8 WU-6 migrations that ship the prod schema

This run is expected to take 30-90 seconds for the full replay. Watch for any migration that errors — that's a real finding (not part of Arc 5 but blocks the smoke test).

**Expected output tail:**
```
INFO  [alembic.runtime.migration] Running upgrade a91c4d2e7f08 -> b2e5f17a3d9c, arc8 wu6 email suppression
```

Verify head:
```powershell
alembic current
```

**Expected:** `b2e5f17a3d9c (head)` — this is the pre-Arc-5 prod state.

Confirm pgvector extension actually got installed during the replay:
```powershell
docker compose exec db psql -U postgres -d luciel -c "SELECT extname, extversion FROM pg_extension WHERE extname='vector';"
```

**Expected:** one row, `vector` + some version (e.g. `0.8.2`).

Verify table count matches prod expectations:
```powershell
docker compose exec db psql -U postgres -d luciel -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';"
```

Note this number — call it **N_base**. We'll add **6** new tables in Revision A, so after `upgrade head` the count should be `N_base + 6`.

---

## Step 2 — UP: `alembic upgrade head` (apply Revision A)

```powershell
alembic upgrade head
```

**Expected output:**
```
INFO  [alembic.runtime.migration] Running upgrade b2e5f17a3d9c -> arc5_a_admin_instance_additive, arc5 Revision A additive ...
```

(The downrev → uprev arrow must show exactly that pair. If you see a different `down_revision`, STOP.)

Verify head landed:
```powershell
alembic current
```

**Expected:** `arc5_a_admin_instance_additive (head)`

---

## Step 3 — UP verification checkpoints (after first upgrade head)

### 3.1 — All 6 new tables exist

```powershell
docker compose exec db psql -U postgres -d luciel -c "\dt"
```

**Expected:** Among the existing tables, these 6 NEW tables must appear:
- `admins`
- `instances`
- `instance_composition_grants`
- `knowledge_share_grants`
- `admin_tier_overrides`
- `metering_emissions`

Table count must equal **N_base + 6**.

### 3.2 — `admins` table shape (Q1/Q2/Gap 1 verification)

```powershell
docker compose exec db psql -U postgres -d luciel -c "\d+ admins"
```

**Critical checkpoints:**
- `id` is `character varying(100)`, NOT NULL, primary key → confirms Q1 lock (semantic key)
- `tier` is `character varying(16)`, NOT NULL, default `'free'::character varying` → confirms Q2 lock
- `tier_source` is `character varying(32)`, NOT NULL, default `'default_orphan'::character varying` → confirms Q2 audit field
- `stripe_customer_id` is `character varying(64)`, **NULL allowed** → confirms Gap 1 lazy-create
- `legacy_tenant_id` is `character varying(100)`, NULL allowed
- CHECK constraints visible: `ck_admins_tier_permissive` (accepts the 7-value union), `ck_admins_tier_source` (5 values)

### 3.3 — `subscriptions.billing_model` column added + backfilled

```powershell
docker compose exec db psql -U postgres -d luciel -c "\d+ subscriptions" | Select-String -Pattern "billing_model"
```

**Expected:** `billing_model | character varying(16) |  |  |` (NULL allowed since added as NULL then backfilled)

Verify backfill ran:
```powershell
docker compose exec db psql -U postgres -d luciel -c "SELECT billing_model, count(*) FROM subscriptions GROUP BY billing_model;"
```

**Expected:** All existing subscription rows show `billing_model = 'flat'` (the in-migration UPDATE). If there are zero existing subscription rows, you'll just see "0 rows" — that's fine.

CHECK constraint visible:
```powershell
docker compose exec db psql -U postgres -d luciel -c "\d+ subscriptions" | Select-String -Pattern "ck_subscriptions_billing_model"
```

**Expected:** `"ck_subscriptions_billing_model" CHECK (billing_model = ANY (ARRAY['flat'...'hybrid'...'consumption'...]))`

### 3.4 — `admin_tier_overrides` wide-row shape (doctrine drift verification)

```powershell
docker compose exec db psql -U postgres -d luciel -c "\d+ admin_tier_overrides"
```

**Critical checkpoints:**
- `admin_id` is PK, `character varying(100)`, FK to `admins.id`
- ~22 nullable entitlement-axis columns visible (instance_count_cap, leads_per_month_cap, api_rate_limit_rpm, seat_cap, audit_retention_days, widget_custom_domain_cname_cap, stripe_customer_record_required, billing_model, support_sla, composition_enabled, sso_enabled, dashboard_views_per_month_cap, data_residency, export_enabled, uptime_sla_pct, etc.)
- 7 WU-8 Phase A columns visible: `billing_model`, `included_usage_per_period`, `overage_rate_cents`, `committed_use_discount_bps`, `period_start`, `period_end`, `metered_unit`
- 6 CHECK constraints listed:
  - `ck_admin_tier_overrides_billing_model`
  - `ck_admin_tier_overrides_metered_unit`
  - `ck_admin_tier_overrides_support_sla`
  - `ck_admin_tier_overrides_committed_use_discount_bps_range` (0–10000)
  - `ck_admin_tier_overrides_uptime_sla_pct_range` (0–100)
  - `ck_admin_tier_overrides_period_window` (period_start ≤ period_end)

If any of those 6 CHECKs is missing, STOP and report.

### 3.5 — Grant tables abuse-prevention CHECKs

```powershell
docker compose exec db psql -U postgres -d luciel -c "\d+ instance_composition_grants" | Select-String -Pattern "CHECK"
docker compose exec db psql -U postgres -d luciel -c "\d+ knowledge_share_grants" | Select-String -Pattern "CHECK"
```

**Expected:** Each table shows a no-self CHECK:
- `ck_instance_composition_grants_no_self_composition` (caller != callee)
- `ck_knowledge_share_grants_no_self_share` (source != target)
- `ck_knowledge_share_grants_scope` (scope ∈ {read_only, read_write})

### 3.6 — Partial indexes on legacy back-pointers (NULL slots don't collide)

```powershell
docker compose exec db psql -U postgres -d luciel -c "\di+" | Select-String -Pattern "WHERE|legacy|stripe_customer"
```

Want to see partial unique indexes with `WHERE … IS NOT NULL` predicates — at least:
- `uq_admins_stripe_customer_id_not_null`
- `uq_admins_legacy_tenant_id_not_null`
- `uq_instances_legacy_luciel_instance_id_not_null`
- `uq_instances_legacy_agent_id_not_null`
- `uq_instance_composition_grants_active_triple` (WHERE revoked_at IS NULL)
- `uq_knowledge_share_grants_active_triple` (WHERE revoked_at IS NULL)

### 3.7 — `metering_emissions` PK + idempotency

```powershell
docker compose exec db psql -U postgres -d luciel -c "\d+ metering_emissions"
```

**Expected:**
- Composite PK on `(admin_id, period, emission_ts)`
- UNIQUE constraint on `stripe_idempotency_key`
- CHECK `quantity_emitted >= 0`
- CHECK `period ~ '^[0-9]{4}-[0-9]{2}$'` (period format YYYY-MM)

→ Snapshot the full schema state at this point for cycle-2 comparison:

```powershell
docker compose exec db pg_dump -U postgres -d luciel -s --schema=public > schema_after_upgrade_1.sql
```

This dumps the schema-only (no data) DDL. We'll diff this against the cycle-2 result.

---

## Step 4 — DOWN: `alembic downgrade -1`

```powershell
alembic downgrade -1
```

**Expected output:**
```
INFO  [alembic.runtime.migration] Running downgrade arc5_a_admin_instance_additive -> b2e5f17a3d9c
```

Verify head reverted:
```powershell
alembic current
```

**Expected:** `b2e5f17a3d9c (head)` — back to Arc 8 WU-6 baseline.

### 4.1 — DOWN verification: all 6 new tables gone

```powershell
docker compose exec db psql -U postgres -d luciel -c "\dt" | Select-String -Pattern "admins|instances|composition_grants|knowledge_share_grants|admin_tier_overrides|metering_emissions"
```

**Expected:** NO matches. All 6 tables dropped.

Table count must equal **N_base** (the pre-Revision-A count from Step 1).

### 4.2 — DOWN verification: `subscriptions.billing_model` column gone

```powershell
docker compose exec db psql -U postgres -d luciel -c "\d+ subscriptions" | Select-String -Pattern "billing_model"
```

**Expected:** NO matches. Column dropped.

### 4.3 — DOWN verification: no orphan indexes/constraints

```powershell
docker compose exec db psql -U postgres -d luciel -c "\di" | Select-String -Pattern "ix_admins|ix_instances|uq_admins|uq_instances|uq_instance_composition|uq_knowledge_share|ix_metering|ix_subscriptions_billing_model"
```

**Expected:** NO matches. All Revision A indexes dropped. If any orphan index remains, the downgrade has a bug — STOP and report.

---

## Step 5 — UP again: `alembic upgrade head` (cycle 2)

```powershell
alembic upgrade head
```

**Expected output:** same as Step 2.

Verify head:
```powershell
alembic current
```

**Expected:** `arc5_a_admin_instance_additive (head)`

### 5.1 — Schema-equivalence diff (cycle 1 vs cycle 2)

```powershell
docker compose exec db pg_dump -U postgres -d luciel -s --schema=public > schema_after_upgrade_2.sql
```

Then diff:

```powershell
fc.exe schema_after_upgrade_1.sql schema_after_upgrade_2.sql
```

(or `Compare-Object (Get-Content schema_after_upgrade_1.sql) (Get-Content schema_after_upgrade_2.sql)`)

**Expected:** Zero meaningful differences. Postgres may reorder some constraint/index lines in `pg_dump` output but the set of objects must be identical. If you see any object appearing in one dump but not the other, STOP and report the diff.

---

## Step 6 — Teardown

```powershell
docker compose down -v
```

Leave the box clean. Nothing should persist from this test.

---

## Success criteria summary

All of the following MUST be true:

- [ ] Step 0: fresh empty DB confirmed
- [ ] Step 1: alembic at `b2e5f17a3d9c (head)` with N_base tables
- [ ] Step 2: alembic upgrade to `arc5_a_admin_instance_additive (head)` succeeds
- [ ] Step 3.1: 6 new tables exist; table count = N_base + 6
- [ ] Step 3.2: admins shape matches Q1/Q2/Gap 1 locks
- [ ] Step 3.3: subscriptions.billing_model added + backfilled to 'flat' + CHECK constraint live
- [ ] Step 3.4: admin_tier_overrides wide-row with ~22+7 columns + 6 CHECK constraints
- [ ] Step 3.5: grant tables have no-self CHECKs + scope CHECK
- [ ] Step 3.6: 6+ partial indexes with `WHERE … IS NOT NULL`
- [ ] Step 3.7: metering_emissions has composite PK + UNIQUE idempotency key + 2 CHECKs
- [ ] Step 4: downgrade -1 returns to `b2e5f17a3d9c (head)`
- [ ] Step 4.1: all 6 tables dropped
- [ ] Step 4.2: subscriptions.billing_model column dropped
- [ ] Step 4.3: no orphan indexes/constraints from Revision A
- [ ] Step 5: upgrade head succeeds cleanly on cycle 2
- [ ] Step 5.1: schema dumps cycle 1 vs cycle 2 are equivalent

If ALL pass → Commit 5 (smoke test) closes successfully. We then move to Commit 6+ (Revision B authoring) OR you may choose to first execute Revision A on prod (TODO item 11) before continuing local authoring.

If ANY fails → I diagnose from the failure output before any further commits.
```
