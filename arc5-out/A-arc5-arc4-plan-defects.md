# Arc 5 — Defects in Arc 4's authored migration plan

**Status:** RECORD — defects found in `arc4-out/A-tenancy-collapse-arc-record.md` §3.1 during Arc 5 pre-flight execution attempt; sprint hard-stopped on discovery.
**Authored:** 2026-05-22 ~22:45 EDT
**Trigger:** Path 3 sprint directive ("execute Arc 5 + Arc 6 tonight, including tests") began executing Arc 4's backfill SQL into a Revision B authoring pass. Authoring failed against the AST-verified schema reality on four separate identifiers. Sprint hard-stopped before any commit landed; no prod state touched; no Revision A/B/C SQL committed.
**Discovery tool:** `arc5-out/_arc5_schema_survey.py` (AST-based; re-runnable, deterministic)
**Anchors:**
- `arc4-out/A-tenancy-collapse-arc-record.md` §3.1 (the defective SQL block)
- `arc5-out/A-arc5-preflight.md` §1.2 + §1.2.5 + §4.1 (AST-corrected schema survey + defect summary)
- `app/models/tenant.py` (TenantConfig — source of truth for D1, D3, D4)
- `app/models/luciel_instance.py` (LucielInstance — source of truth for D2, D3)
- `app/models/subscription.py` (Subscription — source of truth for D4 `tier` location)

---

## §0 — Why this document exists

Doctrine requires that when an authored plan is found defective, the defects are recorded as a first-class artifact, not silently patched. This doc is that record. Arc 4's plan was a strong skeleton — the 3-revision Alembic chain, the 7-batch code-sweep gating, the rollback-window discipline, the WU-8 Phase A schema additions are all sound — but its backfill SQL was authored against an imagined schema, not against `app/models/` reality. Surfacing the gap now (before any prod touch) is the discipline working as designed.

The defects below are the four identifier-level errors that would have failed at `alembic upgrade head` time if Revision B's SQL had been authored verbatim from Arc 4's record. The corrected SQL stanza in §3 is the proposed Arc 5 replacement; it is NOT yet authored as a migration file — Revision A/B/C migration-file authoring is gated on partner review of this doc + the corrected pre-flight.

---

## §1 — The four defects

### D1 — Table `tenants` does not exist; the table is `tenant_configs`

**Arc 4 plan (`arc4-out/A-tenancy-collapse-arc-record.md`):**

| Line | Quoted text |
|---|---|
| 238 | `# 1. Backfill admins from tenants (1:1)` |
| 242 | `    FROM tenants` |
| 243 | `    WHERE NOT EXISTS (SELECT 1 FROM admins WHERE admins.id = tenants.id)` |
| 291 | `op.drop_table("tenants")` |

**Reality:** `app/models/tenant.py:24` declares `__tablename__ = "tenant_configs"`. There has never been a table named `tenants` on this database; the model has always been `TenantConfig` with table `tenant_configs`. Verified at HEAD `d9885f0` via AST survey.

**Impact if executed verbatim:** `alembic upgrade head` would fail at Revision B step 1 with `psycopg2.errors.UndefinedTable: relation "tenants" does not exist`. Revision C's `op.drop_table("tenants")` would also fail with the same error.

### D2 — Column `luciel_instances.tenant_id` does not exist; the column is `scope_owner_tenant_id`

**Arc 4 plan:**

| Line | Quoted text |
|---|---|
| 249 | `    SELECT li.id, li.tenant_id, li.name, li.active, li.created_at, li.id` |

**Reality:** `app/models/luciel_instance.py:99` declares `scope_owner_tenant_id` (with siblings `scope_owner_domain_id` at line 109 and `scope_owner_agent_id` at line 117). The shape is intentionally a three-column scope-owner tuple, not a flat `tenant_id`. The `__table_args__` UNIQUE+CHECK constraints at lines 195–230 reference all three by their `scope_owner_*` names.

**Impact if executed verbatim:** `psycopg2.errors.UndefinedColumn: column li.tenant_id does not exist` at Revision B step 2.

### D3 — Column `name` does not exist on `tenant_configs` or `luciel_instances`; the column is `display_name`

**Arc 4 plan:**

| Line | Quoted text |
|---|---|
| 240 | `    INSERT INTO admins (id, name, tier, active, created_at, legacy_tenant_id)` |
| 241 | `    SELECT id, name, tier, active, created_at, id` |
| 249 | `    SELECT li.id, li.tenant_id, li.name, li.active, li.created_at, li.id` |

**Reality:**
- `app/models/tenant.py:34` declares `display_name: Mapped[str] = mapped_column(String(200), nullable=False)`
- `app/models/luciel_instance.py:86` declares `display_name: Mapped[str] = mapped_column(String(200), nullable=False)`

Neither table has a `name` column.

**Impact if executed verbatim:** `psycopg2.errors.UndefinedColumn: column "name" does not exist` on both backfill INSERTs in Revision B.

**Implication for Revision A's `admins` table shape:** the new column should be named `display_name`, not `name`, for symmetry with what's being replaced. Revision A's §2.1 table-shape entry needs the corresponding rename.

### D4 — Column `tier` does not exist on `tenant_configs`; `tier` lives on `subscriptions`

**Arc 4 plan:**

| Line | Quoted text |
|---|---|
| 240 | `    INSERT INTO admins (id, name, tier, active, created_at, legacy_tenant_id)` |
| 241 | `    SELECT id, name, tier, active, created_at, id` |
| 242 | `    FROM tenants` |

**Reality:** `app/models/tenant.py` full column listing (verified by `grep mapped_column app/models/tenant.py`): the columns on `tenant_configs` are `id`, `tenant_id`, `display_name`, `description`, `escalation_contact`, `allowed_domains`, `system_prompt_additions`, `chunk_size`, `chunk_overlap`, `chunk_strategy`, `active`, `deactivated_at`, `created_by`, `updated_by`, plus the TimestampMixin columns. **There is no `tier` column on `tenant_configs`.** `tier` lives on `subscriptions.tier` (`app/models/subscription.py:244`), joined to the customer via `subscriptions.tenant_id`.

**Impact if executed verbatim:** `psycopg2.errors.UndefinedColumn: column "tier" does not exist` even after D1+D3 are fixed. Naive `SELECT id, display_name, tier, active, created_at FROM tenant_configs` still fails.

**Implication:** the Admin backfill needs a JOIN to `subscriptions`. The shape becomes something like:

```sql
INSERT INTO admins (id, display_name, tier, active, created_at, legacy_tenant_id)
SELECT
    tc.id,
    tc.display_name,
    COALESCE(s.tier, 'free') AS tier,
    tc.active,
    tc.created_at,
    tc.tenant_id    -- the string natural-key, not the autoincrement int
FROM tenant_configs tc
LEFT JOIN subscriptions s
    ON s.tenant_id = tc.tenant_id
   AND s.active = TRUE
WHERE NOT EXISTS (SELECT 1 FROM admins WHERE admins.legacy_tenant_id = tc.tenant_id)
```

Which raises two **partner-review questions** (§2 below).

---

## §2 — Partner-review gates raised by these defects

### Q1 — `admins.id` shape: legacy `tenant_configs.id` (int autoincrement) or `tenant_configs.tenant_id` (string semantic key)?

`tenant_configs` has two candidate keys:
- `id` — autoincrement int, opaque, used internally
- `tenant_id` — string, semantic (e.g. `"luciel-internal"`), used by every FK on every other scoped table

Every other scope-FK column in the database (per the AST survey: 17 surviving tables) is typed as the `tenant_id` string, not the `id` int. The natural choice for `admins.id` is therefore the string — preserves the FK shape downstream, lets Revision B's code sweep flip names without re-typing columns. The Arc 4 plan's `WHERE admins.id = tenants.id` is ambiguous here (whichever `id` it meant, it picks one without doctrine justification).

**Recommendation:** `admins.id` is `VARCHAR` (or whatever `tenant_configs.tenant_id` is currently typed as — to be verified by reading the column declaration). The legacy `tenant_configs.id` autoincrement int is irrelevant to the cutover. `legacy_tenant_id` back-pointer on `admins` carries `tenant_configs.tenant_id` (string), NOT `tenant_configs.id` (int).

### Q2 — Customers with no active subscription: tier default?

The LEFT JOIN in the corrected backfill SQL produces NULL `tier` for any `tenant_configs` row that has no active `subscriptions` row. The `COALESCE(s.tier, 'free')` choice maps such customers to the new Free tier. Is that correct, or should orphan TenantConfigs be considered defective state and fail the backfill?

**Recommendation:** `'free'` default for the customer cohort; the Revision B `TIER_RENAME_APPLIED` audit row should record the source (`'from-subscriptions'` vs `'defaulted-to-free'`) so the population can be audited post-cutover. This matches the doctrine that Free is a real tier with real entitlements (not a "no subscription" sentinel).

### Q3 — Subscriptions with legacy tier strings (`individual`, `solo`, `team`, `company`): map in the JOIN or in a follow-up UPDATE?

Arc 4 §3.1 step 3 has the tier-rename UPDATE *after* the backfills. With the JOIN-based backfill, the same rename could be folded into the JOIN as a CASE expression (one less round-trip). Either order is correct; the question is which is more legible.

**Recommendation:** keep them separate (as Arc 4 has it) — the JOIN selects raw legacy tier strings, then the UPDATE rewrites them. Easier to read, easier to audit, easier to roll back step-by-step.

---

## §3 — Corrected backfill SQL (DRAFT — not yet authored as migration)

Subject to partner-review resolution of Q1, Q2, Q3 above. Authored here so the corrected shape is visible alongside the defects, not as committed migration code.

```python
# Revision B (cutover) — Admin backfill, corrected against schema reality

# 1. Backfill admins from tenant_configs (joined to subscriptions for tier)
op.execute("""
    INSERT INTO admins (id, display_name, tier, active, created_at, legacy_tenant_id)
    SELECT
        tc.tenant_id        AS id,               -- string natural key (Q1)
        tc.display_name,                          -- D3 corrected
        COALESCE(s.tier, 'free') AS tier,        -- Q2 default
        tc.active,
        tc.created_at,
        tc.tenant_id        AS legacy_tenant_id   -- back-pointer (string)
    FROM tenant_configs tc                        -- D1 corrected
    LEFT JOIN subscriptions s                     -- D4 fix (JOIN for tier)
        ON s.tenant_id = tc.tenant_id
       AND s.active = TRUE
    WHERE NOT EXISTS (
        SELECT 1 FROM admins WHERE admins.legacy_tenant_id = tc.tenant_id
    )
""")

# 2. Backfill instances from luciel_instances (corrected against scope_owner_* shape)
op.execute("""
    INSERT INTO instances (
        id, admin_id, display_name, active, created_at,
        legacy_luciel_instance_id
    )
    SELECT
        li.luciel_instance_id        AS id,
        li.scope_owner_tenant_id     AS admin_id,        -- D2 corrected
        li.display_name,                                  -- D3 corrected
        li.active,
        li.created_at,
        li.luciel_instance_id        AS legacy_luciel_instance_id
    FROM luciel_instances li
    WHERE NOT EXISTS (
        SELECT 1 FROM instances WHERE instances.legacy_luciel_instance_id = li.luciel_instance_id
    )
""")

# 3. Tier rename UPDATEs — unchanged from Arc 4 §3.1 step 3
op.execute("UPDATE admins SET tier = 'pro' WHERE tier IN ('individual', 'solo')")
op.execute("UPDATE admins SET tier = 'enterprise' WHERE tier IN ('team', 'company')")
```

**TODO before this becomes the Revision B file:**
- Verify `tenant_configs.tenant_id` and `luciel_instances.luciel_instance_id` column types (likely `VARCHAR(100)` based on `String(100)` mapped_column patterns elsewhere — confirm)
- Verify `subscriptions.active` is the right gate (vs `status='active'` — `app/models/subscription.py` has both)
- Verify `Agent` model collapse into `instances`: Arc 4 §3.1 step 2 was already silent on this (the SQL above is silent too). Open Arc 4 §3.2 question — is `agents` collapsed into `instances` as additional rows, or just dropped wholesale? The pre-flight §4.2 lists `op.drop_table("agents")` which suggests wholesale drop with no migration of Agent rows — confirm this is the intended doctrine.

---

## §4 — Doctrine correction stanza (proposed for Arc 4's arc-record)

When Arc 5 lands, Arc 4's record gets a forward-pointer correction stanza appended at the end:

```markdown
### §10 — Plan defects discovered during Arc 5 execution (RECORDED 2026-05-23)

The backfill SQL in §3.1 above was authored before the post-Arc-3 schema reality was AST-surveyed. Four identifier-level defects (D1–D4) would have caused `alembic upgrade head` failures if executed verbatim. The corrected SQL was authored in `arc5-out/A-arc5-arc4-plan-defects.md` §3 and is the doctrine-honest replacement.

- D1: table `tenants` → actually `tenant_configs`
- D2: column `li.tenant_id` → actually `scope_owner_tenant_id`
- D3: column `name` → actually `display_name` (both tables)
- D4: column `tier` not on `tenant_configs`; lives on `subscriptions.tier` (JOIN required)

The 3-revision chain shape, batch gating contract, rollback discipline, and WU-8 Phase A scope from §1–§9 above remain doctrine; only the SQL stanzas in §3.1 are superseded.
```

This stanza is NOT applied yet — it lands as part of Arc 5's execution record after Revision A is authored. Recorded here so it's visible in the pre-flight pass.

---

## §5 — How the survey caught this

Reproducer (works against any future HEAD):

```bash
cd /home/user/workspace/luciel
python arc5-out/_arc5_schema_survey.py
```

The survey:
1. Walks `app/models/*.py` with `ast.parse`
2. For each `ClassDef`, reads `__tablename__` via AST (not grep)
3. For each `ClassDef`, walks the class body (NOT the module) and lists `mapped_column(...)` / `Column(...)` assignments whose target name is in `{tenant_id, domain_id, agent_id, luciel_instance_id}`
4. Cross-references against `DROP_TABLES = {"tenants", "domains", "luciel_instances", "agents"}` to separate "drop the whole table" from "drop these columns from a surviving table"

Why grep-based survey failed: grep matches the string `tenant_id` anywhere in a file. `app/models/luciel_instance.py` contains `scope_owner_tenant_id` 30+ times; a naive grep would falsely conclude the `LucielInstance` class has a `tenant_id` column. AST walking inside `ClassDef.body` filters that out structurally.

This is now the canonical schema-survey tool for any future destructive migration. It belongs in the repo permanently.
