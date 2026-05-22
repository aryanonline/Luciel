# Arc 4 Deliverable #3 — Tenancy collapse execution arc record

**Status:** DESIGN — execution plan only, no code lands until the plan is reviewed, approved, and staged into Arc 5 (schema migration) and Arc 6 (code rename sweep + Stripe SKU restructure + customer-comms).

**Authored:** 2026-05-22 at the close of Arc 4 doctrine integration, alongside Deliverable #1 (commit `66f6528`) and Deliverable #2 (commit `8c3e0b7`).

**Audience:** Engineer-facing. This is the operational runbook that turns the Arc 4 doctrine (3-doc canonical edits) and tier matrix spec (Deliverable #2) into a sequenced, reversible, audit-trailed code migration.

**Cross-refs:**
- CANONICAL_RECAP.md §14 — buyer-facing doctrine and entitlement matrix
- ARCHITECTURE.md §4.1 — three-axis isolation contract (Admin↔Admin / Instance↔Instance / Lead↔Lead)
- DRIFTS.md §3 `D-tenancy-collapse-admin-instance-lead-2026-05-22` — umbrella drift this arc resolves
- arc4-out/A-tier-matrix-detail.md — engineer-facing tier matrix spec (this arc executes that spec)

---

## §1 — Scope and non-scope

### §1.1 — In scope (the Arc 5 + Arc 6 commits land these)

1. Alembic schema migration: rename `tenants → admins`, drop `domains` table and all FK columns referencing it, rename `agents → instances` (drop `luciel_instances` table; `instances` becomes the canonical name).
2. Code rename sweep: ~4,025 callsites across 227 files in `app/`, `tests/`, `alembic/`. Identifier renames are mechanical (`tenant_id → admin_id`, `agent_id → instance_id`, `LucielInstance → Instance`, etc.); prose-string renames are case-by-case (logs, audit constants, error messages).
3. Entitlements module rewrite: `app/policy/entitlements.py` to the 21-dimension × 4-tier shape from Deliverable #2 §8 (adds `TIER_SOLO` / `TIER_ENTERPRISE`, drops `domains_cap`, adds the four Arc 4 dimensions, adds `admin_tier_overrides` lookup path).
4. New tables: `instance_composition_grants`, `knowledge_share_grants`, `admin_tier_overrides`.
5. Cascade chain reduction: 13-layer cascade → 12-layer cascade (Domain layer removed).
6. Stripe SKU rename: Individual → Solo product label in Stripe dashboard.
7. Customer-comms SES email: one-time best-effort send to existing Individual-tier customers confirming the rename.
8. Marketing-site (Luciel-Website repo) `/pricing` page tier labels: Individual → Solo, add Enterprise card.
9. Test suite update: every assertion on old noun set rebound to new noun set.
10. Closing-tag stamping: `arc-4-tenancy-collapse-admin-instance-lead` lands on the last commit of Arc 6.

### §1.2 — Out of scope (explicitly deferred)

1. **Composition runtime enforcement** — the `instance_composition_grants` and `knowledge_share_grants` tables exist after Arc 5 but `composition_enabled` and `knowledge_share_grants_enabled` remain `enforced=False` in the entitlements module. The chat-service routing layer that *consumes* these grants lands in a separate future arc (§10's `_STEP_ARC_5_COMPOSITION` is misnamed — should be a later arc; will be corrected at execution time).
2. **Per-Instance model-tier routing** — the `model_tier_per_instance` dimension is declared in the entitlements module but `enforced=False`. The `chat_service.py` per-Instance model-class router lands in a separate future arc.
3. **Enterprise tier customer onboarding** — no SKU exists in Stripe yet; the Enterprise tier is a 📋 Planned 4th column in the matrix and the override table exists empty. The first Enterprise customer triggers SKU creation + override row seeding as a separate work-unit.
4. **Channel adapters** — voice / SMS / email remain Roadmap across all tiers. No Step 34a work in this arc.
5. **Backwards-compat for external API consumers** — current customer count is 0 live tenants on the wire (the last live tenant `co-354c5056` was retired at Step 30a.2-pilot). External API stability is not a constraint; we do not preserve old route names or column aliases beyond the immediate migration window.

---

## §2 — Six open questions resolved (from Deliverable #2 §10)

These are commitments now. The reasoning is short but locked.

### §2.1 — Migration ordering: **staged sequence**, not single revision

**Decision:** Three Alembic revisions in sequence, not one atomic revision. Reason: a single 4,025-callsite revision is impractical to review, impossible to rollback cleanly, and creates a >24h merge-conflict window during the sweep. The staged sequence:

1. **Revision A — additive only.** Add new columns alongside old (`admins.id`, `instances.id`, `admin_id` FK columns) and triple-write at the application layer: old code writes `tenant_id`, new code writes both `tenant_id` and `admin_id`. Both columns reference the same value; the model_aliases helper in `app/models/aliases.py` (new file) bridges. This revision creates `instance_composition_grants`, `knowledge_share_grants`, `admin_tier_overrides` empty.
2. **Revision B — backfill + cutover.** Backfill `admin_id` from `tenant_id` (1:1 mapping); flip the application reads from `tenant_id` to `admin_id`; drop the `tenant_id` write half of the dual-write. Old code paths still work via the alias helper.
3. **Revision C — subtractive.** Drop `tenants`, `domains`, `luciel_instances` tables; drop `tenant_id`, `domain_id`, `agent_id` FK columns; delete `app/models/aliases.py`. This revision is the point-of-no-return.

Each revision is independently reversible. Revision A is fully reversible (drop the new columns). Revision B is reversible by re-enabling the `tenant_id` write half. Revision C is reversible only via restore-from-backup; the window for rollback closes at this revision.

### §2.2 — Backwards-compat window: **none for external API; one-revision-pair internal**

**Decision:** No external API backwards-compat (zero live customers, no contract to preserve). Internal backwards-compat lasts exactly the Revision A → Revision B window: dual-write + alias helper. After Revision C, old noun set is dead.

### §2.3 — Stripe SKU rename timing: **after schema migration**

**Decision:** Stripe rename lands in Arc 6, after all three Alembic revisions complete and pass smoke. Reason: Stripe rename is cosmetic and reversible (a dashboard label change); doing it last means the schema is stable when customers see the new label. The reverse ordering (Stripe first, schema second) creates a Stripe-says-Solo-but-DB-says-Individual gap that lasts the entire Arc 5 window — bad audit signal.

### §2.4 — `admin_tier_overrides` seeding: **empty at creation**

**Decision:** Revision A creates the table empty. Currently zero Enterprise customers exist; pre-populating with placeholder rows for Company customers (in case they ever upgrade) creates audit noise without operational benefit. The first Enterprise customer's override row is created at their signup.

### §2.5 — Customer-comms SES email timing: **at Stripe rename**

**Decision:** The customer-comms SES email fires at the same Arc 6 commit that flips the Stripe SKU label. Customers don't see the database; they see the Stripe receipt and the Pricing page label. The email is paired with the visible event:

```
Subject: Your VantageMind Individual plan is now called Solo

Hi {first_name},

We've renamed the Individual tier to Solo. Nothing about your plan has
changed -- same $30/month, same 3-Instance cap, same features. The only
difference is the label.

Why the rename? As we've grown VantageMind, the "Individual" label
stopped reflecting what the tier actually is -- a single-Admin plan that
fits anyone working on their own, whether they're a solo professional,
a freelancer, or a one-person team. "Solo" says that more plainly.

Nothing for you to do. Your next Stripe receipt will say "Solo" instead
of "Individual"; your dashboard already shows the new label.

If you have any questions, hit reply.

-- The VantageMind team
```

Fired post-commit so a SES failure cannot roll back the rename cascade. Failure path writes a `tier_rename_email_send_failed` audit row, same pattern as the pilot-refund courtesy email from Step 30a.2-pilot Commit 3j.

### §2.6 — `max_composition_depth` safety ceiling: **implementation-layer cap of 10**

**Decision:** The entitlement contract for Company / Enterprise remains `None` (unbounded per the doctrine). The chat-service composition router applies an implementation-layer ceiling of **10** as a runaway-prevention safety net. If a request hits depth 10, the router refuses with a structured `composition_depth_safety_ceiling_exceeded` error and emits an audit row. The ceiling is operator-tunable via SSM parameter `composition_depth_safety_ceiling`; the default `10` is a belt-and-suspenders value, not a contract.

This satisfies the partner-locked operational rule "we are designing a business we can[not] get lazy and just ignore things" — the contract says "unbounded" because *the buyer-facing promise is unbounded*, but the implementation guards against pathological cases without changing the contract.

---

## §3 — Migration ordering — Alembic revision detail

**Current truth (Arc 4 tier-shape revision 2026-05-22-late):** Three Alembic revisions A/B/C land the migration. The tier surface targeted by the migration is **Free / Pro / Enterprise** (not the v1 four-tier Solo / Team / Company / Enterprise framing). Concretely:

- **Revision A (additive)** — in addition to the new-table set authored below (`admins`, `instances`, `instance_composition_grants`, `knowledge_share_grants`, `admin_tier_overrides`), Revision A also lands: (a) `subscriptions.billing_model VARCHAR(16) NULL` column with in-migration backfill `UPDATE subscriptions SET billing_model='flat' WHERE billing_model IS NULL`; (b) `admin_tier_overrides` extended with six columns carrying the Enterprise hybrid-billing axis — `billing_model VARCHAR(16) NULL`, `included_usage_per_period INTEGER NULL`, `overage_rate_cents INTEGER NULL`, `committed_use_discount_bps INTEGER NULL`, `period_start DATE NULL`, `period_end DATE NULL`, `metered_unit VARCHAR(16) NULL` (all nullable, defaults per `A-tier-matrix-detail.md` v2 §17.2); (c) new table `metering_emissions` as an append-only cursor for the Enterprise metering hook — primary key `(admin_id, period, emission_ts)`, plus `stripe_idempotency_key VARCHAR(128) NOT NULL`, `quantity_emitted INTEGER NOT NULL`, `stripe_subscription_item_id VARCHAR(64) NOT NULL`, `created_at TIMESTAMP NOT NULL`.
- **Tier CHECK constraint (current truth):** the `admins.tier` CHECK constraint during the migration window allows the **union of legacy + current tier strings** — `tier IN ('free', 'pro', 'enterprise', 'individual', 'solo', 'team', 'company')` — to permit the live data to be backfilled. Revision C's subtractive pass tightens it to `tier IN ('free', 'pro', 'enterprise')` once the rename `UPDATE`s complete.
- **Tier rename UPDATEs (current truth):** Revision B's tier rename writes `UPDATE admins SET tier='pro' WHERE tier IN ('individual', 'solo')`, then `UPDATE admins SET tier='enterprise' WHERE tier IN ('team', 'company')` with an `admin_tier_overrides` row minted per renamed Admin to mirror the customer's previous tier limits as override values so effective limits do not change at the cutover. Free is net-new — no existing customer renames to Free.
- **New audit-action constants (Revision A):** `TIER_RENAME_APPLIED`, `BILLING_MODEL_ENUM_ADDED`, `METERING_INFRASTRUCTURE_ADDED`, `METERING_USAGE_EMITTED`. Revision B writes a `TIER_RENAME_APPLIED` row per existing customer inside the same transaction as the tier rename `UPDATE` (field set `{old_tier, new_tier, admin_tier_overrides_minted}`).

The v1 SQL blocks in §3.1/§3.2/§3.3 below were authored against the v1 four-tier framing. Where the v1 SQL contradicts the current truth above, the v1 line is struck through in place and the current-truth replacement is shown alongside. The v1 SQL is preserved verbatim in git history at commit `5c80c15`.

### §3.1 — Revision A (additive)

**Revision ID:** `arc4_a_admin_instance_additive`
**down_revision:** current head (whatever lands last before Arc 5 starts)

**SQL operations (in order):**

```python
# 1. Create admins table (parallel to tenants)
op.create_table(
    "admins",
    sa.Column("id", sa.UUID, primary_key=True),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("tier", sa.String(32), nullable=False),
    sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("legacy_tenant_id", sa.UUID, nullable=True),  # back-pointer for alias helper
    sa.CheckConstraint(
        # ~~v1: "tier IN ('individual', 'solo', 'team', 'company', 'enterprise')"~~
        # Current truth (Arc 4 tier-shape revision 2026-05-22-late):
        "tier IN ('free', 'pro', 'enterprise', 'individual', 'solo', 'team', 'company')",
        name="ck_admins_tier_valid_during_migration",
    ),
)

# 2. Create instances table (parallel to luciel_instances)
op.create_table(
    "instances",
    sa.Column("id", sa.UUID, primary_key=True),
    sa.Column("admin_id", sa.UUID, sa.ForeignKey("admins.id"), nullable=False),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("legacy_luciel_instance_id", sa.UUID, nullable=True),
    sa.Column("legacy_agent_id", sa.UUID, nullable=True),
)

# 3. Create leads table (parallel to where leads currently live)
# -- NOTE: leads do not yet have a dedicated table; they live in conversations + sessions
# under the existing FK chain. Revision A introduces a `leads` table only if the audit
# of the current data model shows leads are first-class. If they are not, this step is
# a no-op and `lead_id` FKs are postponed to a later arc.

# 4. Composition grants
op.create_table(
    "instance_composition_grants",
    sa.Column("id", sa.UUID, primary_key=True),
    sa.Column("admin_id", sa.UUID, sa.ForeignKey("admins.id"), nullable=False),
    sa.Column("caller_instance_id", sa.UUID, sa.ForeignKey("instances.id"), nullable=False),
    sa.Column("callee_instance_id", sa.UUID, sa.ForeignKey("instances.id"), nullable=False),
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("created_by_user_id", sa.UUID, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
    sa.Column("notes", sa.Text, nullable=True),
    sa.CheckConstraint("caller_instance_id != callee_instance_id", name="ck_composition_grant_distinct_instances"),
)

# 5. Knowledge share grants
op.create_table(
    "knowledge_share_grants",
    sa.Column("id", sa.UUID, primary_key=True),
    sa.Column("admin_id", sa.UUID, sa.ForeignKey("admins.id"), nullable=False),
    sa.Column("source_instance_id", sa.UUID, sa.ForeignKey("instances.id"), nullable=False),
    sa.Column("target_instance_id", sa.UUID, sa.ForeignKey("instances.id"), nullable=False),
    sa.Column("scope", sa.String(32), nullable=False),
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("created_by_user_id", sa.UUID, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
    sa.CheckConstraint("scope IN ('memories', 'leads', 'embeddings', 'all')", name="ck_knowledge_share_scope_valid"),
)

# 6. Admin tier overrides
op.create_table(
    "admin_tier_overrides",
    sa.Column("admin_id", sa.UUID, sa.ForeignKey("admins.id"), nullable=False),
    sa.Column("dimension_key", sa.String(64), nullable=False),
    sa.Column("override_value", sa.JSON, nullable=False),
    sa.Column("override_enforced", sa.Boolean, nullable=False),
    sa.Column("notes", sa.Text, nullable=True),
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("created_by_user_id", sa.UUID, sa.ForeignKey("users.id"), nullable=False),
    sa.PrimaryKeyConstraint("admin_id", "dimension_key"),
)
```

**Application-layer changes in this revision's commit:**
- New file `app/models/aliases.py` containing `admin_or_tenant_id_for(row)` and `instance_or_agent_id_for(row)` helpers.
- New file `app/models/admin.py` (renamed from `app/models/tenant.py`, kept side-by-side during the revision pair).
- Existing `app/models/tenant.py`, `app/models/luciel_instance.py` left untouched.
- Application writes both `tenant_id` and `admin_id` on inserts; reads still go through `tenant_id`.

**Rollback:** `alembic downgrade -1` drops the four new tables. Application-layer commit is reverted.

### §3.2 — Revision B (backfill + cutover)

**Revision ID:** `arc4_b_admin_instance_cutover`
**down_revision:** `arc4_a_admin_instance_additive`

**SQL operations:**

```python
# 1. Backfill admins from tenants (1:1)
op.execute("""
    INSERT INTO admins (id, name, tier, active, created_at, legacy_tenant_id)
    SELECT id, name, tier, active, created_at, id
    FROM tenants
    WHERE NOT EXISTS (SELECT 1 FROM admins WHERE admins.id = tenants.id)
""")

# 2. Backfill instances from luciel_instances (1:1)
op.execute("""
    INSERT INTO instances (id, admin_id, name, active, created_at, legacy_luciel_instance_id)
    SELECT li.id, li.tenant_id, li.name, li.active, li.created_at, li.id
    FROM luciel_instances li
    WHERE NOT EXISTS (SELECT 1 FROM instances WHERE instances.id = li.id)
""")

# 3. Update tier labels to the current three-tier shape (Free / Pro / Enterprise).
# ~~v1 (struck through 2026-05-22-late): 'individual' → 'solo'~~
# ~~op.execute("UPDATE admins SET tier = 'solo' WHERE tier = 'individual'")~~
# Current truth (Arc 4 tier-shape revision 2026-05-22-late):
op.execute("UPDATE admins SET tier = 'pro' WHERE tier IN ('individual', 'solo')")
op.execute("UPDATE admins SET tier = 'enterprise' WHERE tier IN ('team', 'company')")
# Per renamed Admin, mint an admin_tier_overrides row mirroring previous limits as overrides
# (so effective limits at the cutover are unchanged) and write a TIER_RENAME_APPLIED audit row
# in the same transaction. See §9 for the audit-action contract.
```

**Application-layer changes in this revision's commit:**
- Application reads flip from `tenant_id` to `admin_id` (the big rename sweep — ~2,121 callsites for `tenant_id`).
- Application writes flip from dual-write to admin-only write.
- The Code Rename Sweep §4 below specifies the file-by-file batch ordering for this commit.

**Rollback:** A reverse-direction backfill is possible (re-sync any new admins rows back to tenants), but riskier. Application-layer commit revert is the cleaner path. Recovery from a botched Revision B is "revert the application commit and re-enable dual-write." The schema state remains valid.

### §3.3 — Revision C (subtractive)

**Revision ID:** `arc4_c_admin_instance_subtractive`
**down_revision:** `arc4_b_admin_instance_cutover`

**SQL operations:**

```python
# 1. Drop FK columns from every scoped table
# (full list authored at execution time after Revision B smoke is green)
op.drop_column("conversations", "tenant_id")
op.drop_column("conversations", "domain_id")
op.drop_column("conversations", "agent_id")
op.drop_column("sessions", "tenant_id")
# ... etc for ~30+ tables

# 2. Drop the legacy tables
op.drop_table("luciel_instances")
op.drop_table("domains")
op.drop_table("tenants")

# 3. Drop the back-pointer columns on the new tables
op.drop_column("admins", "legacy_tenant_id")
op.drop_column("instances", "legacy_luciel_instance_id")
op.drop_column("instances", "legacy_agent_id")

# 4. Tighten the tier CHECK constraint (drop the migration-window labels)
op.drop_constraint("ck_admins_tier_valid_during_migration", "admins")
op.create_check_constraint(
    "ck_admins_tier_valid",
    "admins",
    # ~~v1 (struck through 2026-05-22-late): "tier IN ('solo', 'team', 'company', 'enterprise')"~~
    # Current truth (Arc 4 tier-shape revision 2026-05-22-late):
    "tier IN ('free', 'pro', 'enterprise')",
)
```

**Application-layer changes:**
- Delete `app/models/tenant.py`, `app/models/luciel_instance.py`, `app/models/aliases.py`.
- Delete every `Tenant` / `LucielInstance` / `Domain` import.
- Audit constants: `TENANT_CREATED` → archived to `app/audit/legacy_constants.py` (read-only), new constants `ADMIN_CREATED` / `INSTANCE_CREATED` become canonical.

**Rollback:** Restore from backup only. The window for clean rollback closes here.

---

## §4 — Code rename sweep — file-by-file batch order (Revision B commit)

The 4,025-callsite sweep is staged into seven batches inside the Revision B commit. Each batch is a self-contained mechanical rename + test pass before moving to the next.

| Batch | Layer | Files (approx) | Identifiers renamed | Validation gate before next batch |
|---|---|---|---|---|
| **B1** | Model layer | 15 | `Tenant`→`Admin`, `LucielInstance`→`Instance`, `TenantConfig`→`AdminConfig`. New aliases.py preserves `Tenant` as a deprecated alias pointing at `Admin` so the rest of `app/` and `tests/` compile during the batch. | `pytest tests/models/ -x` green |
| **B2** | Service layer | 35 | `tenant_id`→`admin_id`, `luciel_instance_id`→`instance_id`, `agent_id`→`instance_id` in kwarg-only-position. Service-layer test fixtures updated in same batch. | `pytest tests/services/ -x` green |
| **B3** | API layer (v1 routes) | 25 | Route paths: `/admin/luciel-instances`→`/admin/instances`, `/admin/domains/*` deleted. Query-param names: `tenant_id`→`admin_id`. Response field names: same rename. | `pytest tests/api/ -x` green + curl smoke against 4 representative endpoints |
| **B4** | Middleware + auth | 8 | `TenantConfig.active`→`AdminConfig.active`, `tenant_admin` role-string→`admin_user`, `department_lead`→`instance_lead`. JWT claim names rebind. | `pytest tests/middleware/ -x` green |
| **B5** | Cascade + audit | 20 | 13-layer cascade → 12-layer (Domain layer removed). Audit constants: `TENANT_CREATED` keeps emitting for backward audit-chain readability during this batch; new rows emit `ADMIN_CREATED`. The cascade-completeness verifier is updated to read both. | `pytest tests/audit/ -x` green + cascade verifier passes |
| **B6** | Tests (non-fixture) | 80 | Assertion-level renames across `tests/integration/`, `tests/e2e/`, harness scripts. The 28-test AST contract suite (the harness-shape pin) is updated last so it can validate the entire post-sweep shape. | Full `pytest -x` green; 33-test live e2e harness green |
| **B7** | Alembic + scripts | 10 | Alembic migration files older than this revision are NOT renamed (historical record; they reference real past schema state). New migrations starting from `arc4_b_admin_instance_cutover` use the new noun set. Scripts in `scripts/` and `arc3-out/`, `arc4-out/` are updated. | Lint pass; no functional gate |

**Batch gating rule:** the commit for batch N+1 cannot land until batch N's validation gate passes. Each batch is its own intermediate commit; the seven commits compose into the Revision B Pull Request.

**Estimated sweep duration:** 8–12 hours of focused paired work, plus 2–3 hours of test recovery. Single session preferred to avoid mid-sweep merge conflicts; if interrupted, the partial state is stable (each batch leaves a green test suite).

---

## §5 — Stripe SKU restructure plan (Arc 6 commit)

### §5.1 — Sequencing

Triggered after Arc 5 Revision C lands green. Single Arc 6 commit:

1. **Stripe dashboard rename:** `Individual` product → `Solo` product. Same price, same metadata, same SKU IDs (Stripe preserves IDs across label changes). Done via Stripe Dashboard or `stripe products update prod_XXX -d name=Solo`.
2. **Create Enterprise product placeholder:** `Enterprise` product with no SKU, no price, marked inactive. Stripe Dashboard. The placeholder exists so future Enterprise SKU creation is one-step.
3. **Webhook regression test:** fire a synthetic `customer.subscription.updated` webhook from Stripe CLI and confirm the local backend's webhook handler reads the new product label correctly.
4. **Marketing-site update:** Luciel-Website `/pricing` page. Individual tier card → Solo tier card. Add Enterprise card with "Contact us" CTA. Deploy to Amplify.
5. **Customer-comms SES email:** fire to every existing Individual-tier customer (queried from `admins` table where `tier='solo'` AND `legacy_tenant_id IS NOT NULL`). Best-effort; failure path writes audit row.

### §5.2 — Stripe SKU rename rollback

Stripe product rename is reversible via `stripe products update prod_XXX -d name=Individual`. The marketing site Amplify deploy is reversible via Amplify console revert. The SES email is irreversible (already sent); if a rollback is needed, a follow-up SES email apologizing for the confusion is the path.

The full Arc 6 rollback path is therefore: revert Stripe label → revert marketing-site Amplify deploy → send follow-up SES. The Arc 5 schema is unaffected by Arc 6 rollback.

---

## §6 — Customer-comms SES email — full draft

**Current truth (Arc 4 tier-shape revision 2026-05-22-late):** The customer-comms SES email below is the **canonical draft** that fires at the Arc 6 cutover. It announces a tier-shape revision — not just a label rename:

- Existing **Solo customers → Pro** (capability identical: 3 Instances cap unchanged; the 2000 leads/month cap is new but exceeds all current Solo usage by an order of magnitude).
- Existing **Company customers → Enterprise**, with an `admin_tier_overrides` row mirroring their Company-tier limits as override values so effective limits do not change at the cutover.
- Existing **Team customers** — none on the live wire at Arc 4 — backfill to Pro if any exist at cutover time.
- **Free is net-new** with no migration impact.

The original four-tier label-only draft (Solo / Team / Company / Enterprise) is preserved at the bottom of this section as struck-through audit material; the canonical send is the revised draft immediately below.

**Canonical draft (lands at Arc 6 commit):**

```
Subject: Your VantageMind plan name is changing (no price or feature changes)

Hi [first_name],

We're tidying up our plan names to better match how customers actually use VantageMind. Starting [cutover_date], your current plan will be renamed:

  * Solo → Pro      (same price, same features)
  * Company → Enterprise  (same price, same features, with your negotiated limits preserved)

We're also introducing a new Free tier for evaluators — it doesn't affect your account.

What's NOT changing:
  * Your monthly invoice amount
  * Your Instance count cap
  * Your data, your team, your widget keys, your dashboards
  * Your contract terms (Enterprise customers)

What IS changing:
  * The label you see on your invoice and in your account dashboard
  * Pro customers now have an explicit 2,000 leads/month cap on paper (this exceeds 100% of current Solo customers' actual usage, so you will not notice)
  * Enterprise customers gain access to new optional capabilities: hybrid billing with included usage + overage (opt-in via account team); committed-use discounts on annual renewal; custom fine-tunes (sales-team coordinated)

If you have any questions, reply to this email and our team will respond within one business day.

Thanks for being a VantageMind customer.

— The VantageMind team
```

SES delivery cohorts (one campaign per cohort, sent same-day to minimize confusion): (1) live Solo customers → Pro rename note; (2) live Company customers → Enterprise rename + override-preservation note (above text customized with their negotiated values inline); (3) no Team customers exist; (4) Free tier launch is announced via a separate marketing campaign, not this cutover email. Suppression-list discipline (§DRIFTS `D-ses-suppression-app-layer-not-implemented-2026-05-22`) is honored for all three cohorts.

---

~~**Original draft (v1 four-tier label-only doctrine, preserved for audit — retired 2026-05-22-late by the tier-shape revision; canonical draft above supersedes):**~~

~~(Authoring here so Arc 6 can pick this up verbatim.)~~

~~**To:** every billing-contact email in `admins WHERE tier = 'solo' AND legacy_tenant_id IS NOT NULL` (i.e. every Admin who was on the Individual tier before the rename)~~
~~**From:** `noreply@vantagemind.ai` (the existing SES verified sender)~~
~~**Subject:** Your VantageMind Individual plan is now called Solo~~
~~**Reply-To:** `support@vantagemind.ai`~~
~~**Tracking:** SES `MessageId` written to a `tier_rename_email_sent` audit row per recipient.~~
~~**Failure handling:** SES throw → catch → write `tier_rename_email_send_failed` audit row with the recipient + exception class. The rename commit does not roll back on SES failure.~~

~~**Body (plaintext):**~~

~~```~~
~~Hi {first_name},~~
~~~~
~~We've renamed the Individual tier to Solo. Nothing about your plan has~~
~~changed -- same $30/month, same 3-Instance cap, same features. The only~~
~~difference is the label.~~
~~~~
~~Why the rename? As we've grown VantageMind, the "Individual" label~~
~~stopped reflecting what the tier actually is -- a single-Admin plan that~~
~~fits anyone working on their own, whether they're a solo professional,~~
~~a freelancer, or a one-person team. "Solo" says that more plainly.~~
~~~~
~~You may also notice some other small terminology changes in your~~
~~dashboard over the next few days: what we used to call your "Tenant" is~~
~~now called your "Admin scope", and what we used to call an "Agent" is~~
~~now called an "Instance". Same things, plainer names.~~
~~~~
~~Nothing for you to do. Your next Stripe receipt will say "Solo" instead~~
~~of "Individual"; your dashboard already shows the new labels.~~
~~~~
~~If you have any questions, hit reply.~~
~~~~
~~-- The VantageMind team~~
~~```~~

~~**Body (HTML):** authored at Arc 6 commit time as a sibling template to the pilot-refund-courtesy email template (existing pattern at `app/email/templates/pilot_refund.html`). New template path: `app/email/templates/tier_rename.html`. Variables: `{{first_name}}`, `{{old_tier_label}}`, `{{new_tier_label}}`.~~

~~**Send pacing:** SES quota is 200 emails/second (post-sandbox-exit). Current Individual-tier customer count: 0 live (post Step 30a.2-pilot retirement). At first signup under the new doctrine, this email will not fire (the recipient is already on Solo). The email path exists for the case where customers sign up *during* the Revision A/B window before Arc 6 lands and end up tagged `solo` with `legacy_tenant_id IS NOT NULL`.~~

---

## §7 — Marketing-site (Luciel-Website) changes

**Current truth (Arc 4 tier-shape revision 2026-05-22-late):** The marketing site (`aryanonline/Luciel-Website`) contracts from a four-card pricing surface (Solo / Team / Company / Enterprise) to a **three-card pricing surface — Free / Pro / Enterprise**. Per-tile CTA shape:

- **Free tile** — no Stripe Checkout button. CTA is `[Get started free](/signup-free)` pointing to the new CAPTCHA-gated signup form (per DRIFTS `D-free-tier-captcha-missing-2026-05-22`).
- **Pro tile** — retains the direct Stripe Checkout button with monthly / annual toggle. Prices read from `[PRO_MONTHLY]` / `[PRO_ANNUAL]` placeholders until Arc 6 pins them to live Stripe SKU metadata.
- **Enterprise tile** — "starting at `$[ENTERPRISE_FLOOR]/year`" framing with a `[Talk to sales](mailto:enterprise@vantagemind.ai)` CTA in place of a direct-Checkout-button.

Marketing-site code-surface impact: (a) `src/pages/Pricing.tsx` swaps from a 4-card grid to a 3-card grid with the new copy from CANONICAL_RECAP §11.7; (b) `src/components/PricingCard.tsx` gains a `variant` prop (`'free' | 'pro' | 'enterprise'`) so each tile renders the right CTA; (c) `src/pages/SignupFree.tsx` is net-new — carries the hCaptcha widget, posts to `POST /api/v1/billing/signup-free`, redirects to `/dashboard` on success; (d) `src/pages/ContactSales.tsx` already exists and is wired for the Enterprise CTA target. Rollback plan is `git revert` of the marketing-site commit + manual cache invalidation on the Amplify distribution.

Outside the Luciel backend repo; tracked here because Arc 6 must coordinate.

**Files to edit in `aryanonline/Luciel-Website`:**

| File | Change |
|---|---|
| `src/pages/Pricing.tsx` | ~~Individual tier card: rename header "Individual" → "Solo"; update copy. Add Enterprise tier card with "Contact us" CTA (mailto: `enterprise@vantagemind.ai` or similar).~~ **Current truth (2026-05-22-late):** Swap the 4-card grid for a 3-card grid — Free / Pro / Enterprise. Free card renders `[Get started free](/signup-free)`; Pro card renders Stripe Checkout button with monthly / annual toggle; Enterprise card renders `[Talk to sales](mailto:enterprise@vantagemind.ai)` with "starting at $[ENTERPRISE_FLOOR]/year" framing. Per-card copy lifts from CANONICAL_RECAP §11.7. |
| `src/pages/Pricing.tsx` (matrix) | ~~The entitlement matrix on Pricing.tsx is the buyer-facing surface of CANONICAL §14. Regenerate from CANONICAL §14 post-Arc-4 — 4 columns (Solo / Team / Company / Enterprise), 22 dimensions grouped under six axes.~~ **Current truth (2026-05-22-late):** Regenerate the matrix from CANONICAL_RECAP §14 v2 — **3 columns (Free / Pro / Enterprise), 18 dimensions plus the new Enterprise hybrid-billing axis (Axis 7)**. Per Step 30a.6 closure, this is a manual sync rather than a generated artifact; the regenerate command is to re-author the JSX literal from the canonical Markdown table. |
| `src/components/nav/MainNav.tsx` | No change — nav already points to `/pricing`, `/login`, `/dashboard`. |
| `src/i18n/en.json` (if exists) | ~~Tier label strings: rename `tier.individual.*` → `tier.solo.*`. Add `tier.enterprise.*`.~~ **Current truth (2026-05-22-late):** Tier label strings collapse to `tier.free.*`, `tier.pro.*`, `tier.enterprise.*`. Retire `tier.individual.*`, `tier.solo.*`, `tier.team.*`, `tier.company.*`. |
| `public/pricing.json` (if exists) | ~~Same renames.~~ **Current truth:** Mirror the three-tier shape — Free / Pro / Enterprise keys only. |
| `src/copy/landing.tsx` | ~~Any inline "Individual" mentions → "Solo".~~ **Current truth:** Any inline tier-name mentions ("Individual", "Solo", "Team", "Company") collapse to "Free" / "Pro" / "Enterprise" per the §11.7 positioning copy. |
| `src/pages/SignupFree.tsx` (NEW) | Net-new page carrying the hCaptcha widget, posting `{email, display_name, captcha_token}` to `POST /api/v1/billing/signup-free`, redirecting to `/dashboard` on success. |

**Deploy:** Amplify auto-deploys from `main`. The Amplify Hooks domain pattern from Step 30a.1 holds.

**CloudFront invalidation:** required post-deploy per `D-vantagemind-dns-cloudfront-mismatch-2026-05-13`. Manual step.

---

## §8 — Test suite update — assertion-level rename inventory

Authored at Revision B Batch B6. Key assertion patterns:

| Test file | Assertion pattern | Post-Arc-4 form |
|---|---|---|
| `tests/api/test_admin.py` | `assert response.json()["tenant_id"] == ...` | `assert response.json()["admin_id"] == ...` |
| `tests/services/test_tier_provisioning.py` | `tier_provisioning_service.premint_for_tier(tier=TIER_INDIVIDUAL, ...)` | `tier_provisioning_service.premint_for_admin(tier=TIER_SOLO, ...)` |
| `tests/audit/test_cascade_completeness.py` | 13-layer expected cascade | 12-layer expected cascade |
| `tests/api/test_step31_validation_gate_shape.py` (19 contract tests) | scope-bound dashboard view kwargs `tenant_id` / `domain_id` / `agent_id` | `admin_id` / `instance_group_id` / `instance_id` |
| `tests/integration/test_billing_webhook.py` | webhook handler asserts on `Tenant` | `Admin` |
| `tests/e2e/step_30a_live_e2e.py` | full live signup harness — every step references old nouns | full rebind |

**Special case — historical e2e harnesses:** harnesses in `tests/e2e/historical/` (if the directory exists) that document past pre-Step-30a.6 state should NOT be renamed; they are audit-chain records of past behavior. A header comment at the top of each historical harness file states "Frozen at pre-Arc-4 state; do not rename nouns".

**Special case — Alembic migration tests:** any test in `tests/alembic/` that exercises a specific historical revision must continue to reference the noun set valid at that revision. Only the post-`arc4_c_admin_instance_subtractive` tests use the new noun set.

---

## §9 — Audit chain integrity through the migration

**Current truth (Arc 4 tier-shape revision 2026-05-22-late):** The audit-chain integrity discipline is unchanged in shape — hash-chained advance, no recomputation, verifier passes at every revision boundary. The tier-shape revision adds **four new audit actions** that emit at the Arc 5 + Arc 6 cutover:

- `TIER_RENAME_APPLIED` — one row per existing customer at Revision B's tier-rename `UPDATE`, field set `{old_tier, new_tier, admin_tier_overrides_minted: bool}`.
- `BILLING_MODEL_ENUM_ADDED` — one row at Revision A close, system-actor `system:migration`, tied to the schema change.
- `METERING_INFRASTRUCTURE_ADDED` — two rows at Revision A close (one for `admin_tier_overrides` extended columns, one for the `metering_emissions` table).
- `METERING_USAGE_EMITTED` — one row per successful Stripe usage emission; lands at Arc 6 and continues ongoing in production.

The hash-chain advance test (`scripts/verify-audit-chain.ps1`) runs across the Revision A → B → C boundary and against a freshly-emitted `METERING_USAGE_EMITTED` row to confirm no chain break.

**Audit-chain hash refresh:** the v2 `A-tier-matrix-detail.md` rewrite committed in the doc-only commit immediately preceding Arc 5 is itself an audit-chain event — not at the `admin_audit_log` layer (which is for runtime mutations only), but at the documentation audit chain (DRIFTS §3 umbrella drift cross-refs + this arc-record's git history). Commit-hash record below will be filled at commit time:

- Pre-revision baseline hash: `5c80c15` (Arc 4 Deliverable #3 commit before tier-shape revision)
- Doc-only revision commit hash: (filled at commit)
- Arc 5 Revision A commit hash: (filled at Arc 5 commit)
- Arc 5 Revision B commit hash: (filled at Arc 5 commit)
- Arc 5 Revision C commit hash: (filled at Arc 5 commit)
- Arc 6 Stripe SKU + metering worker commit hash: (filled at Arc 6 commit)
- Arc 6 closing-tag commit hash: (filled at Arc 6 commit, stamps `arc-4-tenancy-collapse-admin-instance-lead`)

The hash-chained `AdminAuditLog` must survive the migration with no chain break.

### §9.1 — Action string handling

| Action | Pre-Arc-4 | During Revision A (dual-write) | During Revision B (cutover) | Post-Revision C |
|---|---|---|---|---|
| Tenant created | `TENANT_CREATED` | `TENANT_CREATED` (still emitted) | `ADMIN_CREATED` (new rows); `TENANT_CREATED` still readable on old rows | `ADMIN_CREATED` only |
| Domain created | `DOMAIN_CREATED` | `DOMAIN_CREATED` (still emitted from any code path that still mints Domains — Team's path doesn't, Company's does) | No new emissions (Domain layer being removed); old rows readable | Read-only legacy constant; no new emissions |
| Agent / Instance created | `AGENT_CREATED` | `AGENT_CREATED` + `INSTANCE_CREATED` dual-emit | `INSTANCE_CREATED` only on new rows | `INSTANCE_CREATED` only |

The verify-chain script (`scripts/verify_audit_chain.py` or wherever it lives) reads ALL historical action strings — `TENANT_CREATED`, `DOMAIN_CREATED`, `AGENT_CREATED`, `ADMIN_CREATED`, `INSTANCE_CREATED` — as valid actions. The chain hash is computed over the verbatim row content, so a row with `TENANT_CREATED` from 2026-05-13 verifies identically before and after the migration.

### §9.2 — Hash chain — no recomputation

The hash chain is **never recomputed**. Even though we rename the FK columns (`tenant_id` → `admin_id`) on rows we *keep*, we do NOT rewrite historical `AdminAuditLog` rows. Audit rows are immutable by contract; the migration must respect that. The view-layer that renders audit logs to the operator does the FK-name translation at read time, not at write time.

### §9.3 — Verifier passes at every revision boundary

- After Revision A: chain verifies (no changes to existing rows).
- After Revision B backfill: chain verifies (backfill is INSERT-only, no UPDATE on existing rows; the new `admins` rows have their own audit row `ADMIN_BACKFILLED_FROM_TENANT` emitted at backfill time).
- After Revision C: chain verifies (DROP COLUMN doesn't touch row content; the rows we keep still hash-verify).

---

## §10 — Sub-drift activation map

The umbrella `D-tenancy-collapse-admin-instance-lead-2026-05-22` will spawn five sub-drifts at execution time. Each sub-drift activates when its execution boundary is crossed:

| Sub-drift (activates at) | Activation event | Closure event |
|---|---|---|
| `D-arc4-revision-a-additive-schema-2026-05-XX` | Revision A commit | Revision A smoke passes on prod RDS |
| `D-arc4-revision-b-cutover-2026-05-XX` | Revision B commit | Revision B smoke passes on prod RDS + 7-batch rename sweep complete |
| `D-arc4-revision-c-subtractive-2026-05-XX` | Revision C commit | Revision C smoke passes; tenants/domains/luciel_instances tables confirmed dropped |
| `D-arc4-stripe-sku-rename-2026-05-XX` | Arc 6 commit | Stripe dashboard confirms "Solo" label live; webhook regression test green |
| `D-arc4-customer-comms-ses-rename-2026-05-XX` | Customer-comms email batch fired | Audit rows confirm send-success ≥ 95%; failures categorized |

Each sub-drift is born OPEN with a clear closure path. None of them activate today (Deliverable #3 is a plan, not an execution).

---

## §11 — Closing-tag plan

The closing tag `arc-4-tenancy-collapse-admin-instance-lead` is stamped on the **final commit of Arc 6**. The final commit is the customer-comms SES batch fire (or the Arc 6 doc-truthing commit that records the SES batch result, whichever lands last).

The tag is **not** stamped at:
- Arc 5 schema migration completion (the rename is incomplete without Stripe + customer comms)
- Stripe SKU rename (the SES batch hasn't fired yet)
- Marketing-site deploy (it's part of Arc 6 but not the closing event)

Closing-tag earned when:
- All three Alembic revisions are green on prod RDS
- All seven rename-sweep batches are green
- Stripe SKU label is "Solo" (verified via Stripe CLI)
- Marketing-site `/pricing` page renders the four-tier Solo / Team / Company / Enterprise shape
- Customer-comms SES batch has fired (success rate logged)
- DRIFTS §3 has all five sub-drifts marked RESOLVED
- The umbrella `D-tenancy-collapse-admin-instance-lead-2026-05-22` is marked RESOLVED with strikethrough
- A final doc-truth commit stamps the closing tag on CANONICAL §17 and ARCHITECTURE §4.1

---

## §12 — Risk register

**Arc-execution sequence (current truth, Arc 4 tier-shape revision 2026-05-22-late):** **Arc 8 (security hardening) precedes Arc 5 (schema migration), which precedes Arc 6 (Stripe SKU restructure + metering worker).** An earlier draft of this section sequenced Arc 5 before Arc 8; that ordering is incorrect and is struck through in the risk-register table below for traceability.

**Why Arc 8 must run first:** Arc 8 (security hardening) touches load-bearing infrastructure files that Arc 5 (schema migration) will rename. Specifically: Arc 8 modifies `app/middleware/auth.py`, `app/middleware/session_cookie_auth.py`, `app/middleware/rate_limit.py`, `app/repositories/audit_chain.py`, `app/main.py`, the Dockerfile, and `cfn/luciel-prod-ecs.yaml`; Arc 5 renames `app/middleware/*.py` callsites that reference `tenant_id` → `admin_id` and renames every `Tenant` / `Agent` / `LucielInstance` model reference repo-wide (227 files, ~4,025 callsites per the Arc 4 Deliverable #2 code-surface impact assessment). Running Arc 5 first would force every Arc 8 commit to either (a) edit the post-rename file shape (impossible — Arc 8 work was authored against pre-rename references) or (b) carry merge conflicts on every Arc 8 commit against the just-renamed files. Running Arc 8 first lets each Arc 8 commit land cleanly against the existing noun set; Arc 5's seven-batch rename sweep then mechanically updates the Arc 8 commits' callsites alongside the rest of the codebase.

**Why this wasn't caught earlier:** The Arc 4 design pass was authored before the Arc 8 work-unit list was sized (the Arc 3 deploy ceremony surfaced the seven Arc 8 items on 2026-05-22 ≈ 02:36 EDT, materially after the initial Arc 4 design pass at 2026-05-22 ~10:00 EDT). The sequence as originally written assumed Arc 5 was the next arc; the Arc 8 work-unit was a parallel discovery, not a planned predecessor. The doctrine-pivot annotation pass landed the late discovery into the live plan.

**Corrected sequence in this arc record:**

1. **Arc 8 (security hardening)** lands first — worker-not-root, version endpoint, health endpoint, SES sandbox exit + feedback + suppression, free-tier-CAPTCHA infrastructure (the `signup-free` route, the hCaptcha SSM key, the `admins.last_signup_ip` column added as a standalone migration ahead of the full rename sweep).
2. **Arc 5 (schema migration)** lands second — the three-revision A/B/C dance described in §3 above (now extended with the tier-shape-revision additions: `billing_model` enum, `admin_tier_overrides` extended columns, `metering_emissions` table). The seven-batch rename sweep mechanically updates Arc 8's callsites alongside everything else.
3. **Arc 6 (Stripe SKU restructure + metering worker)** lands third — the three-product restructure (archive Solo/Team/Company; create Free $0 + rename Solo→Pro + create Enterprise hybrid pair) + the Celery-beat metering emitter + the customer-comms SES email (§6 above). The `arc-4-tenancy-collapse-admin-instance-lead` closing tag stamps on the final Arc 6 commit.

The original risk-register table below remains the canonical risk-by-risk enumeration; the **single row whose ordering was wrong** is corrected here at the top of the section so a reader does not have to scan the table to find the correction.

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Revision A → B window stays open too long, rotation/refund/cron worker hits dual-write inconsistency | Low (current customer count is 0 live) | Medium | Time-bound the window to ≤ 7 days; ship Revision B within one week of Revision A. |
| ~~Mid-sweep merge conflict if Arc 8 (security hardening, separate arc) lands concurrently~~ | ~~Medium~~ | ~~High (Arc 8 touches some of the same files: middleware, audit, model layer)~~ | ~~Sequence Arc 5 before Arc 8 on the roadmap. Arc 8 starts only after Revision C lands.~~ Corrected 2026-05-22-late: sequence is **Arc 8 before Arc 5** — see the truth statement at the top of this section. |
| Stripe webhook handler regression after SKU rename — webhook payload field shape unchanged but our code paths assume the old product label | Low | Medium | Pre-Arc-6 webhook regression test using Stripe CLI's `stripe trigger customer.subscription.updated` fires a synthetic webhook against the renamed product. Green before live rename. |
| Customer-comms SES email bounces against still-sandbox SES (if SES sandbox exit `D-ses-sandbox-exit-pending-2026-05-22` not yet closed) | Medium (depends on Arc 8 SES sandbox exit timing) | Low (zero current customers means the email queue is near-empty) | Sequence: close SES sandbox exit (Arc 8 work) before firing the customer-comms batch. If sandbox exit is still pending at Arc 6 time, defer the SES batch until sandbox exit closes; the rename itself proceeds. |
| `Agent` class-name collision — `Agent` is used both as the AI-worker entity (the noun being renamed to `Instance`) and possibly in third-party libraries (e.g. `requests.Session.headers["User-Agent"]` is unrelated but a grep will surface it) | Medium | Low (grep noise, not real conflicts) | Use scoped grep with import-context filtering during the rename sweep. The B1 batch validates this — model-layer renames first, surface any unintended collisions before the API-layer sweep. |
| `tenant_id` appears in JWT claims minted before the migration — old JWTs reference the old claim name | High (every active session pre-Arc-5) | Medium (session expiry breaks gracefully) | Revision A's alias helper handles both claim names. JWTs minted post-Revision-B carry only `admin_id`. Existing JWTs expire naturally (24h session window); no force-logout needed. Document this in the Revision B commit notes. |
| Pre-existing drift `TIER_PERMITTED_SCOPES[TIER_TEAM] = ("agent", "domain")` vs `DOMAIN_COUNT_CAP_BY_TIER[TIER_TEAM] = 0` (line 101 vs line 147 of `subscription.py`) — Team's scope-level map still claims Domain even though Step 30a.6 set the cap to 0. The two have been inconsistent since 2026-05-20. | Confirmed (read at this Deliverable's grounding pass) | Low (Arc 4 closes the inconsistency by deleting the map) | Recorded here; closed at Revision B Batch B1 when `TIER_PERMITTED_SCOPES` is deleted. |

---

## §13 — Closing

This is the operational runbook. It compiles the Arc 4 doctrine (Deliverable #1), the tier matrix spec (Deliverable #2), and the staged migration plan (this Deliverable #3) into a single sequenced execution path: **three Alembic revisions → seven-batch rename sweep → Stripe SKU rename → customer-comms SES → closing tag**.

The plan is conservative on scope (no composition runtime, no model-tier routing, no channel adapters — those are explicit out-of-scope) and aggressive on doctrinal correctness (every noun rebind, every audit-chain preservation, every rollback path explicit). The Pattern E declared at the umbrella drift — full rename + selective subtraction of the Domain layer — holds throughout.

The next action is **partner review of this plan**, then Arc 5 kickoff (Revision A).

**Closing tag (this deliverable):** `arc-4-deliverable-3-execution-arc-record` — earned when this file commits and the umbrella drift's Cross-refs line adds a pointer to it.
