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
4. Cross-references against `DROP_TABLES = {"tenant_configs", "domains", "luciel_instances", "agents"}` to separate "drop the whole table" from "drop these columns from a surviving table"

Why grep-based survey failed: grep matches the string `tenant_id` anywhere in a file. `app/models/luciel_instance.py` contains `scope_owner_tenant_id` 30+ times; a naive grep would falsely conclude the `LucielInstance` class has a `tenant_id` column. AST walking inside `ClassDef.body` filters that out structurally.

This is now the canonical schema-survey tool for any future destructive migration. It belongs in the repo permanently.

---

## §6 — Partner resolutions (RECORDED 2026-05-23 9:44am EDT)

All four open questions (Q1, Q2, Q3, and Gap 1 from `A-arc5-preflight.md` §6) resolved during the morning re-anchor pass. Partner directive: "yes this looks good partner. Go ahead and proceed" with the entitlement table as the buyer-facing surface + agent's recommended schema/Stripe shape.

### §6.1 — Q1: `admins.id` shape — LOCKED as string (semantic key)

**Decision:** `admins.id VARCHAR(100) PRIMARY KEY` populated from `tenant_configs.tenant_id` (e.g. `"luciel-internal"`), NOT from `tenant_configs.id` (autoincrement int).

**Rationale:**
1. **FK symmetry** — all 17 surviving scoped tables already type their scope FK as the string `tenant_id` (AST-verified). Choosing the string means Revision B is a column rename, not a re-type + re-backfill. Risk reduction.
2. **Operator-readability** — `SELECT * FROM admin_audit_logs WHERE admin_id = 'luciel-internal'` reads better than `WHERE admin_id = 42` in incident response.
3. **URL routability** — `/admin/luciel-internal/instances/...` is shareable; opaque ints are not.
4. **Stripe symmetry** — `stripe_customer_id` is a string; admin_id being a string keeps the join shape consistent for Arc 6.

**Cost accepted:** legacy `tenant_configs.id` (autoincrement int) is orphaned. Preserved in `admins.legacy_tenant_id` for historical reconstruction; nothing downstream depends on the int.

**Schema column shape (locks in Revision A):**
```
admins.id VARCHAR(100) PRIMARY KEY
admins.legacy_tenant_id VARCHAR(100) NULL  -- back-pointer to tenant_configs.tenant_id; same string
```

### §6.2 — Q2: orphan-customer tier default — LOCKED as `'free'` with audit trail

**Decision:** Customers in `tenant_configs` with no active `subscriptions` row default to `tier = 'free'` on backfill.

**Audit-trail discipline:** Revision B emits an `ADMIN_BACKFILLED` audit row per admin with `source` field carrying one of:
- `'from-subscriptions'` — the customer had an active subscription; tier copied (and renamed in step 3)
- `'defaulted-to-free'` — the customer had no active subscription; tier set to `'free'`

This lets us audit the orphan cohort post-cutover (count, who they are, what they were doing). The `source` field is recorded on the audit row, NOT on the `admins` table itself — audit data belongs in the audit chain, not on the row it audits.

### §6.3 — Q3: tier-string renames — LOCKED as lowercase + separate UPDATE step

**Decision:** Tier vocabulary is `'free'` / `'pro'` / `'enterprise'` (all lowercase). Renames happen as separate `UPDATE` statements AFTER the backfill INSERT, NOT folded into the JOIN's CASE.

**Storage convention:** lowercase strings (matches existing DB tier-string convention: `individual`, `solo`, `team`, `company` are all lowercase). UI presentation capitalizes at render time ("Pro" / "Enterprise" / "Free").

**Rename map:**
```sql
UPDATE admins SET tier = 'pro'        WHERE tier IN ('individual', 'solo');
UPDATE admins SET tier = 'enterprise' WHERE tier IN ('team', 'company');
-- 'free' rows stay 'free' (no rename needed; 'free' is net-new at this revision)
```

**CHECK constraint** (tightens in Revision C, NOT Revision A — Revision A keeps the constraint permissive during the in-flight rename window):
```sql
ALTER TABLE admins ADD CONSTRAINT admins_tier_check
  CHECK (tier IN ('free', 'pro', 'enterprise'));
```

### §6.4 — Gap 1: Free tier behaviour — LOCKED as real tier, no-Stripe-on-Free, 1-instance cap

**Decision:** Free is a real, first-class tier with real entitlements and real audit logging. It is NOT a "no-subscription" sentinel. Free admins get a real `admins` row, a real `instances` row (capped at 1), no Stripe customer record (lazy-created on first upgrade), and the entitlement vector specified in §6.5 below.

**Rationale:**
1. **Acquisition funnel** — Free is the onboarding ramp. Sentinel-shape would force every Free → Pro upgrade to be a state migration; real-tier-shape makes upgrade a simple `subscriptions` row append.
2. **Operational symmetry** — cascade verifiers, audit chains, retention policies, deletion logs all key on `admin_id`. Branchless shape across all tiers = lower maintenance tax forever.
3. **Six pillars** — sentinel-shape violates scalability + maintainability via the branch tax.

**Stripe shape:** `admins.stripe_customer_id VARCHAR(100) NULL`. Free admins carry NULL. Upgrading to Pro/Enterprise creates the Stripe customer record (~200ms latency on upgrade click, pre-warmable at upgrade-form-load). Belt-and-suspenders CHECK constraint at the DB layer (lands in Revision C):
```sql
ALTER TABLE admins ADD CONSTRAINT admins_stripe_customer_id_required_on_paid_tier
  CHECK (tier = 'free' OR stripe_customer_id IS NOT NULL);
```

### §6.5 — Final entitlement table — LOCKED 2026-05-23 9:51am EDT

The canonical buyer-facing entitlement vector for Arc 5 + Arc 6 + all downstream surfaces. This table **supersedes Arc 4 tier-matrix-v2's §2.1 + §3.1 + §4.1 + §6.1 + §7.1 + §8.1 + §9.1 numeric cells where they conflict**; Arc 4 §17 Enterprise hybrid-billing axis is unchanged.

| Entitlement | Free | Pro | Enterprise |
|---|---|---|---|
| Max instances | 1 | 10 | unlimited (sales-negotiated) |
| Max admin-team seats (dashboard logins) | 1 | 25 | unlimited |
| Max leads per month | 100 | 5,000 | unlimited (metered usage above floor) |
| API rate limit (req/min) | 30 | 300 | 3,000 |
| Audit log retention | 30 days | 1 year | 7 years (or contract) |
| Widget custom-domain CNAME | No | Yes (1) | Yes (unlimited) |
| Stripe customer record | No (lazy on upgrade) | Yes | Yes |
| Support SLA | Community | 48h email | 24h email + dedicated CSM |

**Notes on row semantics (so we don't re-litigate later):**

1. **"Max admin-team seats (dashboard logins)"** — these are humans who help the Admin operate the dashboard (assistants, junior teammates, bookkeepers). Scoped to the **Admin account**, NOT per-instance — a Pro Admin with 10 instances still has 25 total seats shared across all instances. Roles within seats: `admin_owner`, `instance_lead`, `member`, `delegated_admin` (per Arc 4 §7.2).

2. **"Leads"** are end-user conversations (the homebuyer/seller chatting with a Luciel). NOT capped per-instance; capped per-month-per-Admin. Metering basis for Enterprise.

3. **"Widget custom-domain CNAME"** — Meaning A confirmed by partner 2026-05-23: customer points `chat.theircompany.com` at our widget endpoint. This is NOT the legacy `domains` table from the four-layer hierarchy (that table is dropped wholesale in Revision C). It's a new lightweight CNAME-mapping artifact that lands at Arc 6 alongside the Stripe wiring; v1 schema is a `admin_widget_domains(admin_id, cname, verified_at, created_at)` table authored at Arc 6.

4. **"Stripe customer record: No (lazy on upgrade)"** — means `admins.stripe_customer_id IS NULL` while tier='free'. Upgrade transition creates the Stripe customer + subscription in a single coherent operation. See §6.4 rationale + the CHECK constraint above.

5. **"Support SLA: Community"** — means GitHub Discussions / community Slack / docs-search; no email ticket. "48h email" = ticket response within 48 business hours (M–F 9am–6pm EDT). "24h + CSM" = 24-hour email response PLUS a dedicated Customer Success Manager for the contract term.

### §6.6 — Doctrine deltas this resolution creates

The entitlement table above **diverges from Arc 4 tier-matrix-v2** in 7 of 8 rows (only "Free instance cap = 1" + "Free audit retention = 30 days" + "Free seat cap = 1" match). Specifically:

| Dim | Arc 4 v2 | This resolution | Direction |
|---|---|---|---|
| Free leads/month | 10 | **100** | More generous (10×) |
| Free API | Disabled (0 rpm) | **Enabled at 30 rpm** | Net-new on Free — raises captcha drift to P1 |
| Pro instance cap | 3 | **10** | More generous (3.3×) |
| Pro seat cap | 5 | **25** | More generous (5×) |
| Pro leads/month | 2,000 | **5,000** | More generous (2.5×) |
| Pro API rpm | 60 | **300** | More generous (5×) |
| Enterprise API rpm | 1,000 default | **3,000 default** | Header value raised |

**Sibling-doc rewrites required (executed in next pass):**
- `arc4-out/A-tier-matrix-detail.md` §2.1, §3.1, §4.1, §6.1, §7.1, §8.1, §9.1 — in-place rewrite of the Numeric values cells with the new defaults; §10–§16 rows that aren't in the new table (composition depth, knowledge-share grants, SSO, etc.) keep Arc 4's existing values
- `docs/CANONICAL_RECAP.md` §12 — add 2026-05-23 revision stanza recording this decision + reason ("founder business-shape call, Option A locked")
- `docs/CANONICAL_RECAP.md` §14 — update the entitlement-matrix-v2 reference to point at §6.5 here as the source of truth (the tier-matrix-detail.md doc gets a forward-pointer in its header to this resolution)
- `app/policy/entitlements.py` — rewrite `TIER_ENTITLEMENTS` dict with the new defaults; lands as part of Revision A's authoring batch (NOT in the migration file itself — the values live in code, the DB constraint only enforces the tier-name vocabulary)
- `docs/DRIFTS.md` §3 — (a) raise `D-free-tier-captcha-missing-2026-05-22` from P2 to **P1** (Free + API = abuse vector); (b) **open new drift** `D-pro-tier-rate-limit-abuse-surface-2026-05-23` (Pro at 300rpm × 25 seats × 10 instances = theoretical single-admin saturation; need per-instance + per-key rate-limiting before Pro launches at scale)

These rewrites land in the NEXT commit after this resolution doc is committed, as one cohesive doctrine-revision pass.
