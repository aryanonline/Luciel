# Arc 7 — Commit 5 RECORD (RESCOPED: leads-cap doctrine retirement)

**Date:** 2026-05-24
**Operator:** Computer (agent), partner-delegated end-to-end
**Outcome:** SUCCESS — `leads_per_month_cap` field deleted from `TierEntitlement`, frontend copy purged of monthly-lead-count claims, all doctrine comments updated to reflect retirement. No schema change this commit. No Stripe touch. No prod deploy needed yet (next prod cut bundles C5 + C6).

---

## Why this commit changed shape

The original Commit 5 plan was to **enforce** `leads_per_month_cap` (the value-lever cap: Free=100, Pro=5,000, Enterprise=50,000) and build an `enterprise_overflow_archive` table for traffic above the Enterprise 50k.

Mid-execution, partner challenged the premise: **"if we are introducing rate limits per tiers then what is need for introducing leads cap. That doesn't make sense?"**

That challenge was correct. The two caps govern different surfaces in theory (RPM = anti-burst infrastructure; lead count = monthly business value), but under Arc 7's **flat-recurring no-metering doctrine**:

- `api_rate_limit_rpm` (Commit 4 tier-aware middleware) already closes the abuse surface: Free=30 rpm, Pro=300 rpm, Enterprise=3,000 rpm. A Free admin's theoretical monthly request ceiling is ~1.3M; a Pro's is ~13M. The lead-count caps (100, 5k, 50k) sit *far* below those ceilings and never actually engage as abuse boundaries — they only engage on *legitimate* customers who convert well.
- Capping a paying customer's success ("you converted 5,001 leads this month, sorry") is anti-value and violates the doctrine that "every paying tier is flat-recurring with no metering."
- The field was metering-ghost residue from the pre-Arc-7 hybrid doctrine.

Partner chose **Option 1 — kill the field entirely.** Rate-limit alone is the abuse boundary.

---

## Files changed

### Backend (`luciel`)

| File | Change |
| --- | --- |
| `app/policy/entitlements.py` | Deleted `leads_per_month_cap: int \| None` from `TierEntitlement` dataclass. Removed the field from all three tier rows (Free=100, Pro=5,000, Enterprise=50,000 → all gone). Replaced inline "abuse cap; overflow → archive" comment on Enterprise with a doctrine note explaining retirement. Added doctrine note where the field used to live, naming `arc7-out/arc7-commit5-leads-cap-retirement-record.md` (this file) as the source. |
| `app/core/config.py` | Updated the tier-topology comment block (lines ~155–167) to point at `api_rate_limit_rpm` as the gate, not `leads_per_month_cap`. |
| `app/services/billing_service.py` | Updated the `_TIER_CADENCE_TO_PRICE_SETTING` block comment to point at `api_rate_limit_rpm` + `instance_count_cap` + `embed_key_count_cap` as gates, with retirement note for `leads_per_month_cap`. |

### Frontend (`Luciel-Website`)

| File | Change |
| --- | --- |
| `src/pages/Pricing.tsx` | Removed "100 leads/month" from Free bullets, "5,000 leads/month per instance" from Pro bullets, "50,000 leads/month" from Enterprise bullets. Updated module-level doctrine comment to explain the retirement. |
| `src/pages/Signup.tsx` | Removed "50,000 leads/month" from both SEO descriptions (annual + monthly Enterprise) and both prose paragraphs. |
| `src/pages/SignupFree.tsx` | Removed "100 leads per month" from the Free-tier prose. |

### NOT changed (deferred to Arc 8 schema sweep)

- `admin_tier_overrides.leads_per_month_override` column — still in schema, no longer read by code. Documented as deferred drop in the entitlements.py doctrine note. Keeping this commit code-only avoids bundling a schema migration with a doctrine retirement.
- `alembic/versions/arc5_a_admin_instance_additive.py` — historical revision, untouched as is doctrine.

---

## Tests

- `tests/policy/` — **38 passed** (entitlements dataclass, resolve_entitlement, tier semantics)
- `tests/security/test_rate_limit_tier_aware.py` — **17 passed** (no field reference inside; passes after deletion)
- `tests/services/` — **60 passed** (no service-layer reference to the deleted field)

Net: **98 tests green, 0 regressions.** No test asserted on `leads_per_month_cap` — clean removal.

---

## Doctrine carry-forward

Going forward, the Arc 7 tier-shape doctrine is:

- **Abuse boundary:** `api_rate_limit_rpm` (Commit 4 middleware, per-(tier, admin, instance) bucket)
- **Value gate that separates Pro from Enterprise:** `instance_count_cap` (Pro=10, Ent=unlimited), `embed_key_count_cap` (Pro=10, Ent=100), `seat_cap` (Pro=25, Ent=unlimited), `composition` (Pro depth≤2, Ent unlimited), `sso_enabled`, `widget_branding_custom`, `widget_custom_domain_cname_cap`, `audit_retention_days` (Pro 1y, Ent 7y/unlimited), `dashboard_views`, `support_sla`.
- **Retired:** `leads_per_month_cap`, `enterprise_overflow_archive`, all metering, all overage billing.

This commit closes the "metering-ghost" drift introduced by leaving the field in place after the Arc 7 doctrine pivot.

---

## Next commit

**Commit 6 — `admins.last_signup_ip` column + 1-per-IP soft gate.** Distinct abuse surface (signup fraud, not request volume). Schema change + alembic migration + tier-bypass rule for Enterprise contracts.

🟢 Arc 7 Commit 5: **CODE COMPLETE** — partner gate cleared (Option 1 retirement), commit ready to seal.
