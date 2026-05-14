# Step 30a.2 — Design Plan
**Authored:** 2026-05-14 09:50 EDT, this session
**Status:** DRAFT — awaiting Aryan's explicit approval before any `app/` edits
**Bundled with:** Step 30a.1 closing slice (Stripe live activation + Phase 1+2+5+6)

---

## 0. Inputs locked this morning

| Gate | Decision | Source |
|---|---|---|
| Trial mechanics | All 6 (tier × cadence) flows get a **$100 paid 3-month intro**, first-time only, then auto-converts to plan rate | Aryan, 09:42 EDT |
| Retention window | **90 days uniform** after paid period end | Aryan, 09:42 EDT |
| Retention worker host | Agent's judgment → **existing `luciel-worker` Celery container, new beat schedule** | Justified §3 |
| Deploy strategy | **30a.2 code first, then Stripe activation, then Phase 1+2+5+6** | Aryan, 09:42 EDT |

---

## 1. Trial mechanics — $100 paid 3-month intro (Stripe object model)

### Recommended approach: **Option A — separate one-time intro_fee Price + `trial_period_days=90` on recurring**

**At Checkout-session creation:**
```python
stripe.checkout.Session.create(
    mode="subscription",
    line_items=[
        # The recurring plan (sub line item) — $0 for 90 days, then plan rate
        {"price": resolve_price_id(tier, cadence), "quantity": 1},
    ],
    subscription_data={
        "trial_period_days": 90 if is_first_time(tenant_id) else 0,
    },
    # The $100 intro fee is added as a one-time line item IF first-time
    discounts=[],  # no discounts used
    # ^ if first_time:
    #     line_items.append({"price": intro_fee_price_id, "quantity": 1})
    ...
)
```

Wait — that has a problem: Checkout in `mode=subscription` only accepts ONE recurring line item plus zero-or-more one-time items. Re-checking Stripe docs.

**Confirmed pattern (verified via Stripe docs section "Combine one-time and recurring prices"):**
- `mode="subscription"`
- `line_items` array can mix one-time + recurring Prices
- One-time price → invoiced immediately at checkout completion
- Recurring price → starts billing per `subscription_data.trial_period_days`

So the customer sees: **"Pay $100 today, then $X/mo (or $X/yr) starting in 90 days."**

### "First-time" gate

Stripe doesn't know "first time." We enforce in `BillingService.create_checkout_session`:

```python
def is_first_time_customer(db: Session, tenant_id: str) -> bool:
    """True if this tenant has never had a subscriptions row.

    Note: we check tenant_id, not stripe_customer_id, because the
    customer object may be created lazily at checkout. tenant_id is
    our stable identifier.
    """
    return db.query(Subscription).filter_by(tenant_id=tenant_id).count() == 0
```

**Edge cases I want your explicit ack on before coding:**
- (E1) **Trial-period cancellation:** Customer pays $100, cancels in week 6. Today: subscription stays active until trial end (90d), then would auto-charge. With cancel-and-purge (1b), we need to honor "access until paid period ends" — and the $100 covers exactly 90 days, so paid period = 90 days from checkout. After 90d, cascade-deactivate, 90d retention, then purge. **Total exposure to "their data still exists" from cancellation date: up to 90 + 90 = 180 days.** Acceptable?
- (E2) **What if they cancel before $100 charges?** Stripe Checkout charges $100 the instant they submit card. There's no "cancel before charge." Refund is operator-only (you, manually).
- (E3) **What if they try to checkout a SECOND time after cancelling?** They'd no longer be "first-time." They'd skip the $100 intro and go straight to plan rate. **Is that what we want?** Or do we want to enforce "one intro per tenant, ever" (current logic) vs "one intro per fresh start"?

### New objects required in Stripe live mode

| Object | Type | Count |
|---|---|---|
| `intro_fee_100` | One-time Price ($100 CAD) | **1 new** |
| `stripe_price_individual` | Recurring Monthly | unchanged |
| `stripe_price_individual_annual` | Recurring Annual | unchanged |
| `stripe_price_team_monthly` | Recurring Monthly | unchanged |
| `stripe_price_team_annual` | Recurring Annual | unchanged |
| `stripe_price_company_monthly` | Recurring Monthly | unchanged |
| `stripe_price_company_annual` | Recurring Annual | unchanged |

**SSM keys: now 7, not 6:**
- New: `/luciel/prod/stripe/price_id/stripe_price_intro_fee`
- All 6 existing keys unchanged

### Code changes in `billing_service.py`

1. `TRIAL_DAYS` dict → **delete it entirely**, replace with single constant `INTRO_TRIAL_DAYS = 90`
2. `resolve_trial_days(tier, cadence)` → simplify to `resolve_trial_days(*, is_first_time: bool) -> int`
3. Add `INTRO_FEE_PRICE_KEY = "stripe_price_intro_fee"` + `resolve_intro_fee_price_id()`
4. Add `is_first_time_customer(tenant_id) -> bool`
5. Modify `create_checkout_session` to append intro_fee line_item when first-time
6. Update `app/core/config.py` to add `stripe_price_intro_fee: str = ""` setting

### Tests to update

- `tests/api/test_step30a_1_tiered_self_serve_shape.py` lines 236-241 → assert new behavior (all return 90 if first-time, 0 if not)
- New test: `test_first_time_gate.py` — covers E1, E2, E3 above

---

## 2. Cascade extension — conversations + identity_claims (revised after schema audit)

### Schema reality (verified 09:55 EDT against actual models)

```
tenant_configs (active, NEW: deactivated_at) ← the "tenant" identity row
   ├── conversations    (tenant_id FK→tenant_configs.tenant_id, active, NEW: deactivated_at)
   │      └── sessions  (conversation_id FK→conversations.id ON DELETE SET NULL, NO active)
   │             └── messages (session_id FK→sessions.id ON DELETE CASCADE, NO active)
   ├── identity_claims  (tenant_id FK→tenant_configs.tenant_id, active, NEW: deactivated_at)
   └── [existing 7 cascade layers below tenant_config]
```

Key corrections to plan v1:
- There is **no `tenants` table**. The top-level row is `tenant_configs` (which the cascade already flips at step 7). The new `deactivated_at` belongs there.
- `messages` has **no `active` column** and no soft-delete shape. Adding one would be architectural drift no read path honors.
- `sessions` has **no `active` column** and no FK to `tenant_configs`. Its `tenant_id` is a plain String.

### Decisions locked 09:55 EDT (agent judgment, delegated by Aryan)

| Layer | Treatment |
|---|---|
| `conversations` | **Add to soft-delete cascade.** New `deactivated_at` column. Step 1 of extended cascade. |
| `identity_claims` | **Add to soft-delete cascade.** New `deactivated_at` column. Step 2 of extended cascade. |
| `sessions` | **NOT in soft-delete cascade.** Hard-purged at retention time via direct DELETE. Implicit deadness during the 90d retention window is acceptable — reads filter at the conversation layer. |
| `messages` | **No column changes.** Lives and dies with sessions via SQL FK CASCADE. Hard-purged automatically when sessions are deleted at retention time. |
| `tenant_configs` | **Add `deactivated_at`** + index `(active, deactivated_at)`. Step 7 of cascade (existing step) now also stamps `deactivated_at=now()`. This is the column the retention worker scans. |

### New cascade order (9 layers total, was 7)

```
 1. conversations      [NEW soft-delete] tenant_id=:tid, active=true
 2. identity_claims    [NEW soft-delete] tenant_id=:tid, active=true
 3. memory_items       [existing]
 4. api_keys           [existing]
 5. luciel_instances   [existing]
 6. agents             [existing]
 7. agent_configs      [existing]
 8. domain_configs     [existing]
 9. tenant_config      [existing, EXTENDED to also stamp deactivated_at]
```

All in single transaction. `autocommit=False` parameter unchanged; webhook callsite unchanged.

### Schema migration (final scope)

`alembic/versions/<new_rev>_step30a_2_deactivated_at_and_retention.py`:

**upgrade():**
- ADD COLUMN `tenant_configs.deactivated_at TIMESTAMP WITH TIME ZONE NULL`
- ADD COLUMN `conversations.deactivated_at TIMESTAMP WITH TIME ZONE NULL`
- ADD COLUMN `identity_claims.deactivated_at TIMESTAMP WITH TIME ZONE NULL`
- CREATE INDEX `ix_tenant_configs_active_deactivated_at` ON `tenant_configs(active, deactivated_at)`

**downgrade():** reverse order — drop index, drop 3 columns.

**Alembic head AFTER:** to be minted (let's preview-name it; will set the actual hex slug when file is written)
**Alembic head BEFORE:** `c2a1b9f30e15`

### Webhook callsite (`billing_webhook_service.py:478-548`)

`_on_subscription_deleted` already calls `deactivate_tenant_with_cascade(autocommit=False)`. **No change to webhook signature needed.** The extended cascade is transparent. Single `db.commit()` at end wraps all 9 layers atomically.

---

## 3. Retention worker — Celery beat in existing luciel-worker

### Why this host (my judgment per Aryan's delegation)

**For:**
- Zero new infra (no EventBridge rule, no new ECS task-def, no new IAM role)
- Beat schedule lives in `celery_app.conf.beat_schedule` — code-only change
- Reuses existing CloudWatch log group `/ecs/luciel-worker`
- Reuses existing RDS connection pooling
- Reuses existing audit-chain installation (cascade-purge writes AdminAuditLog rows; need hash chain in same process)

**Against:**
- Couples retention lifecycle to worker deploys (acceptable — both move together)
- One worker container, no redundancy (if it's down at 03:00 ET, we miss that night; acceptable for a once-daily job — next run catches up)

### Beat schedule

```python
celery_app.conf.beat_schedule = {
    "retention-cascade-purge-nightly": {
        "task": "app.worker.tasks.retention.run_retention_purge",
        "schedule": crontab(hour=7, minute=0),  # 03:00 ET = 07:00 UTC (EDT) / 08:00 UTC (EST)
        # ^ wait — DST. Use 07:00 UTC year-round, which is 03:00 EDT / 02:00 EST.
        # Or use 08:00 UTC = 04:00 EDT / 03:00 EST. Pick one; both are off-peak.
        # Recommendation: 08:00 UTC (3am Markham in winter, 4am in summer).
    },
}
```

### New worker task: `app/worker/tasks/retention.py`

```python
@shared_task(bind=True, name="app.worker.tasks.retention.run_retention_purge")
def run_retention_purge(self):
    """Nightly: hard-delete tenants deactivated >90 days ago.

    Scans tenant_configs WHERE active=false AND deactivated_at < now() - INTERVAL '90 days'.
    For each tenant_id, calls hard_delete_tenant_after_retention.

    Logs to AdminAuditLog. Emits structured CloudWatch line per tenant purged.
    """
```

### New admin_service method: `hard_delete_tenant_after_retention(tenant_id)`

**This is the ACTUAL purge** (the cascade only deactivates). Order (leaf-first hard DELETE):
1. Idempotency guard: re-verify `tenant_configs.active=false AND deactivated_at < now() - 90d`
2. DELETE `messages WHERE session_id IN (SELECT id FROM sessions WHERE tenant_id=:tid)` — OR rely on SQL CASCADE when sessions go (cleaner; let FK do the work)
3. DELETE `sessions WHERE tenant_id=:tid` — cascades to messages via FK
4. DELETE `conversations WHERE tenant_id=:tid` — sessions already gone, no orphan risk
5. DELETE `identity_claims WHERE tenant_id=:tid`
6. DELETE `memory_items WHERE tenant_id=:tid`
7. DELETE `api_keys WHERE tenant_id=:tid`
8. DELETE `luciel_instances WHERE tenant_id=:tid`
9. DELETE `agents WHERE tenant_id=:tid`
10. DELETE `agent_configs WHERE tenant_id=:tid`
11. DELETE `domain_configs WHERE tenant_id=:tid`
12. DELETE `tenant_configs WHERE tenant_id=:tid` — the row itself
13. Write AdminAuditLog row (action=`tenant_hard_purged`) with row-count map per table
14. Single transaction; any error rolls back the entire purge

**FK note:** `conversations.tenant_id` has `ON DELETE RESTRICT` to `tenant_configs.tenant_id`. Since we hard-delete `conversations` BEFORE `tenant_configs` (step 4 before step 12), this is fine. Same applies to `identity_claims`. The RESTRICT is a safety net against accidental tenant deletion with live children — we satisfy it by deleting children first.

**Audit log retention:** AdminAuditLog rows for the deleted tenant are intentionally NOT purged. They are the legal record that the purge happened. They contain `tenant_id` as a string reference, not an FK, so they survive.

### Beat runner startup

Worker container today runs `celery -A app.worker.celery_app worker ...`. Beat is a **separate process**. Two options:
- (a) Run `celery -A app.worker.celery_app worker --beat` in the same container (beat embedded in worker, requires `-l info` flag and works with single-replica setups — we are single-replica)
- (b) Add a separate beat sidecar container in the task-def

**Recommendation: (a) embedded beat.** Simpler, no new container, fits single-replica reality. Trade-off: if we ever scale worker to 2+ replicas, we MUST switch to (b) or use a beat lock (celery-redbeat). I'll add a CANONICAL_RECAP note flagging this future-debt.

### Dockerfile / task-def change

`Dockerfile.worker` (or wherever the CMD is set) → add `--beat` flag.
Task-def `luciel-worker` revision bump (23, up from 22).

---

## 4. Annual trial change

**This is now subsumed by Section 1.** All cadences get the same 3-month intro mechanism. The "annual trial" question dissolves — there's no separate annual trial; everyone gets the $100/3mo intro.

`TRIAL_DAYS` dict is deleted. Annual subscribers no longer rely on "17% prepay incentive alone" — they get the same intro. The 17% annual discount can stay as a separate signal (it's encoded in the annual Price vs monthly Price × 12 ratio; we set this when we create the 6 Stripe Prices tomorrow).

---

## 5. Doc-truthing lockstep map

### DRIFTS to open / close (single commit)

| Drift ID | Action |
|---|---|
| `D-cancellation-cascade-incomplete-conversations-claims-2026-05-14` | OPEN + CLOSE in same commit (gap discovered + filled this session) |
| `D-trial-policy-mixed-per-tier-2026-05-14` | OPEN + CLOSE (gap: trial varied by tier; closed by uniform 3mo $100 intro) |
| `D-no-retention-worker-pipeda-principle-5-2026-05-14` | OPEN + CLOSE (gap: PIPEDA P5 required, never built; closed by retention worker) |
| `D-vantagemind-apex-www-split-2026-05-14` | OPEN only (deferred) |
| `D-stripe-live-account-not-yet-activated-2026-05-13` | CLOSE (when activation approved) |
| `D-tier-name-team-vs-department-2026-05-14` | OPEN only (drift between code & marketing; defer fix) |
| `D-canonical-recap-section-12-table-overflow-2026-05-14` | OPEN + CLOSE (closed by format migration) |
| `D-celery-beat-single-replica-coupling-2026-05-14` | OPEN only (future-debt: must use redbeat or sidecar if we scale worker beyond 1 replica) |

### CANONICAL_RECAP §12

- Add row for Step 30a.2 (new tag: `step-30a-2-trial-and-purge-complete`)
- Update Step 30a.1 row's "Notes" to reference 30a.2 closure
- §14: append timestamp + summary
- §12 format migration: convert the whole §12 table to heading-per-step format (per side-observation #6); preserves data, fixes overflow

### ARCHITECTURE.md

- §3.2.13 — extend cascade layer-count from 7 → 10
- §3.2.X (new) — retention worker (Celery beat schedule, 03/04am Markham, calls hard_delete)
- §4.4 — billing flow: add intro_fee_100 Price + first_time gate
- §4.9 — webhook event handling: no change to `_on_subscription_deleted` (transparent)

---

## 6. Atomic deploy gates (with explicit your-approval markers)

Each ✋ is a checkpoint where I stop and wait for your "go".

1. ✋ **Approve this design plan** (you, now)
2. Write Alembic migration (deactivated_at on 4 tables)
3. ✋ Show you the migration file before I add code
4. Extend `deactivate_tenant_with_cascade` (3 new layers)
5. Write retention worker task + hard_delete method
6. Wire beat schedule in `celery_app.py`
7. Update `billing_service.py` (TRIAL_DAYS → INTRO_TRIAL_DAYS, intro_fee logic, first_time gate)
8. Update `app/core/config.py` (new setting)
9. Update Dockerfile.worker (add --beat)
10. Update tests
11. ✋ Show you the full diff before commit
12. Local: `alembic upgrade head` on a scratch DB to verify migration
13. Local: pytest full suite
14. ✋ Approve to commit + push
15. Build new images (backend + worker), push to ECR
16. Register new task-defs (backend:44, worker:23)
17. ✋ Approve prod deploy from your laptop
18. Run migration on prod RDS (via one-shot ECS task or you-from-bastion)
19. Update services to new revisions (backend + worker)
20. Smoke: tenant deactivation in dev tenant, verify 10 layers all flip
21. ✋ Smoke green? Approve Stripe activation
22. **You submit Stripe activation form** (from laptop, using v2 scaffold)
23. Wait for approval (could be minutes, could be hours — we're paused on Stripe-side)
24. After approval: create 7 Prices (6 recurring + 1 intro_fee) in Stripe live mode
25. 7 SSM puts to `/luciel/prod/stripe/price_id/*`
26. Force service redeploy (backend) to pick up SSM values
27. Smoke: 3 checkout flows (one per primitive: ind/team/company), each with first-time + repeat-customer path
28. CloudFront `/*` invalidation
29. ✋ All smoke green? Approve doc-truthing commit
30. Doc-truthing commit: §12 rows, §14, §12 format migration, §3.2.13 + §3.2.X + §4.4 + §4.9, DRIFTS opens/closures
31. Push, re-cut tag forward (`step-30a-2-trial-and-purge-complete`)
32. ✋ Confirm tag clean on origin + your laptop

**Estimated wall-clock:** 8–10 hours from now to tag-cut, assuming Stripe activation lands within 2 hours of submission. Worst case (Stripe takes overnight): we land 30a.2 code + tag today and Phase 1+2+5+6 first thing tomorrow.

---

## 7. Open questions — RESOLVED 2026-05-14 09:55 EDT

| Q | Resolution |
|---|---|
| E1 (180d total data exposure post-cancel) | **Acceptable** — Aryan, 09:55 |
| E3 (first-time semantics) | **Once per tenant ever.** Cancel + rejoin → no second intro, straight to plan rate. — Aryan, 09:55 |
| Q2 (beat timing) | **08:00 UTC year-round** (04:00 EDT summer, 03:00 EST winter). Off-peak in both, UTC anchor matches infra convention. — Agent judgment, delegated. |
| Q3 (currency) | **CAD only** for today. Canadian sole-prop, Stripe-CA activation account, one intro_fee Price. Multi-currency deferred. — Agent judgment, delegated. |

### Implications now baked into the plan
- `is_first_time_customer` checks `subscriptions.tenant_id` count == 0; cancelled rows still count, so rejoin = not-first-time. **No code special-case needed.**
- Stripe Price `intro_fee_100` created as `currency: cad`, `unit_amount: 10000` (cents). One SSM key.
- Beat schedule: `crontab(hour=8, minute=0)` UTC (no `tz` param needed — Celery `enable_utc=True` is already on in `celery_app.py:200`).

---

## 8. Next action

Step 2 from §6: writing the Alembic migration now. Will show you the file before any service code is touched.

**Approval status:** Design plan APPROVED via Q1/E3/Q2/Q3 resolution 2026-05-14 09:55 EDT.

---

**End of design plan v1.**
