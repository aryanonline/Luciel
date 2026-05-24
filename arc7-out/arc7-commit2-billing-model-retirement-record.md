# Arc 7 — Commit 2: `billing_model` column retirement

**Date:** 2026-05-24
**Branch:** `main`
**Parent commit:** `a01eedb` (Slice 2/3 — Stripe mint + SSM put)
**Doctrine anchor:** Arc 7 doctrine pivot — every paying tier is FLAT-recurring symmetric self-serve; metering RETIRED.

---

## Summary

Drops the `billing_model` enum-column scaffolding that Arc 5 Revision A
added to back the Enterprise hybrid-billing shape. Under the Arc 7
doctrine pivot (Commit 1, 2026-05-24) every paying tier is now
flat-recurring, so the column carries zero information. Path A doctrine
("whatever we ship out in our code and prod and schema must be aligned
with this vision") forbids keeping the legacy `'hybrid'` /
`'consumption'` literals reachable.

---

## Schema changes (Alembic)

**Migration:** `alembic/versions/arc7_a_retire_billing_model.py`

- **revision:** `arc7_a_retire_billing_model`
- **down_revision:** `arc6_c_pending_downgrade_columns`
- **head check:** `python -m alembic heads` returns
  `arc7_a_retire_billing_model (head)` post-author.

### `upgrade()` (forward)

1. `DROP CHECK CONSTRAINT ck_subscriptions_billing_model_valid` on
   `subscriptions`.
2. `DROP INDEX ix_subscriptions_billing_model` on `subscriptions`.
3. `DROP COLUMN subscriptions.billing_model`.
4. `DROP CHECK CONSTRAINT ck_admin_tier_overrides_billing_model_valid`
   on `admin_tier_overrides`.
5. `DROP COLUMN admin_tier_overrides.billing_model`.

**Scope discipline:** the `admin_tier_overrides` table itself is NOT
dropped — it remains forward-architecture for Enterprise contract
overrides per ARCHITECTURE §3.2.14. Only the orphan `billing_model`
column inside it goes.

### `downgrade()` (reverse)

Re-adds both columns with the original Arc 5 Revision A shape (nullable
VARCHAR(16), CHECK on `('flat','hybrid','consumption')`, index on the
`subscriptions` copy only). Backfills `subscriptions.billing_model` to
`'flat'` for every existing row (matches Arc 5 Revision A's original
backfill). Does NOT attempt to reconstruct `'hybrid'` on any row — the
doctrine pivot retired that shape and no on-disk record exists of
which rows would have been hybrid.

### Data salvage analysis

* `subscriptions.billing_model` — every prod row was backfilled to
  `'flat'` at Arc 5 Revision A creation time (line 856 of
  `arc5_a_admin_instance_additive.py`). No code path has flipped any
  row since. Dropping the column loses **zero** customer-facing
  information: the canonical buyer-facing shape is reconstructable from
  `admins.tier` (every paid admin is flat by definition under Arc 7).
* `admin_tier_overrides.billing_model` — table has **zero rows** in
  prod (no app-code path writes to the table; it is
  forward-architecture). Column drop is a data no-op.

---

## Code changes

**File:** `app/policy/entitlements.py`

Path A aggressive removal — drops every now-unreachable identifier
alongside the schema retirement:

1. **Removed** the `BILLING_MODEL_FLAT`, `BILLING_MODEL_HYBRID`,
   `BILLING_MODEL_CONSUMPTION` module-level constants and the
   `ALL_BILLING_MODELS` tuple. Replaced with a retirement comment
   block pointing to this migration.
2. **Removed** the `billing_model: str` field on the
   `@dataclass TierEntitlement` definition (Axis 16). Replaced with a
   retirement comment.
3. **Removed** the `billing_model=BILLING_MODEL_FLAT` row in each of
   the three `TIER_ENTITLEMENTS` entries (Free, Pro, Enterprise).

**Consumer audit performed:** `grep -rn "\.billing_model\|TIER_ENTITLEMENTS\["` over `app/` confirmed no consumer outside `entitlements.py` ever read `.billing_model` from `TierEntitlement`. No tests reference it. Live source is now clean.

---

## Verification (5 gates equivalent)

1. **Migration module parses:** `python -c "import importlib.util; ..."` → `revision='arc7_a_retire_billing_model'`, both `upgrade` + `downgrade` callable.
2. **Alembic head check:** `python -m alembic heads` → `arc7_a_retire_billing_model (head)`.
3. **Entitlements import:** `python -c "from app.policy.entitlements import TIER_ENTITLEMENTS, TIER_ENTERPRISE; e=TIER_ENTITLEMENTS[TIER_ENTERPRISE]; print(hasattr(e,'billing_model'))"` → `False`. Enterprise caps intact: `leads=50000`, `rpm=3000`.
4. **Live source scan:** `grep -rn "billing_model\|BILLING_MODEL_*" app/ tests/` → all hits are inside comments explaining the retirement.
5. **Pytest sweep:** `tests/api/test_arc6_upgrade.py + tests/policy/ + -k "entitlement or tier or billing"` → **70 passed, 2 skipped, 0 failed**. One failure (`test_step31_2_cookie_bridge_shape.py::test_cookie_auth_paths_excludes_billing`) confirmed pre-existing via `git stash` (Settings `database_url` env-gap, not a regression).

---

## Stripe + SSM impact

**No Stripe Live mutations in this commit** (schema-and-code only).

**SSM state observed (read-only probe at commit time):**

```
/luciel/production/stripe_price_enterprise_floor_annual: EXISTS (Version 1)  ← orphan
/luciel/production/stripe_price_enterprise_monthly:      EXISTS (Version 1)  ← Arc 7 active
/luciel/production/stripe_price_enterprise_annual:       EXISTS (Version 1)  ← Arc 7 active
/luciel/production/stripe_price_pro_monthly:             EXISTS (Version 1)
/luciel/production/stripe_price_pro_annual:              EXISTS (Version 1)
/luciel/production/stripe_price_intro_fee:               EXISTS (Version 2)
```

**`stripe_price_enterprise_floor_annual` is now an orphan param** —
* it points to archived Stripe Price `price_1TaOmPRytQVRVXw7ozfKMFps` (active=False),
* the corresponding `app/core/config.py` setting field was removed in Slice 1,
* no live code path reads it.

### Orphan SSM deletion: deferred to partner Console action

`LucielSandboxStripeScope` policy grants `ssm:GetParameter` + `ssm:PutParameter` on the `stripe_*` glob but NOT `ssm:DeleteParameter`. Two options:

1. **Partner Console deletion** (recommended): partner deletes via AWS Console after this commit lands. No IAM widening needed. Single click. Single artifact removed.
2. **Add `ssm:DeleteParameter` to the policy** under the 5-gate protocol. Larger blast radius for a single one-time deletion — not justified.

**Action requested:** partner please delete `/luciel/production/stripe_price_enterprise_floor_annual` via AWS Console (Systems Manager → Parameter Store → select → Delete) at next break. This will be logged as the final step of Arc 7 Commit 2 in CANONICAL_RECAP §17 at Commit 11.

---

## Drifts closed / opened

* **Closes (advances toward retirement at Commit 11):**
  `D-enterprise-metering-not-implemented-2026-05-22` (P1) — the hybrid/metered Enterprise shape will never ship; column drop makes this irreversible.
* **Opens:**
  `D-arc7-ssm-orphan-floor-annual-pending-console-delete-2026-05-24` — closed when partner deletes the orphan SSM param via Console. Logged for completeness; no production impact (param is unreferenced).

---

## Files changed

- `alembic/versions/arc7_a_retire_billing_model.py` (NEW, +171 lines)
- `app/policy/entitlements.py` (modified, -28 / +14)
- `arc7-out/arc7-commit2-billing-model-retirement-record.md` (NEW, this file)
