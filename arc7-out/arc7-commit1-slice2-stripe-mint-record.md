# Arc 7 Commit 1 Slice 2/3 — Stripe Enterprise symmetry mint record

**Date:** 2026-05-24 (mint completed at `2026-05-24T14:22:07+00:00`)
**Anchor commit (Slice 1):** `9585eda` on `main` — doctrine pivot backend code
**This commit (Slice 2/3):** `arc7(c1-s2)` — Stripe Live mutations + SSM puts + audit record
**Stripe account:** `acct_1TX2BmRytQVRVXw7` (Live)
**AWS account:** `729005488042`, region `ca-central-1`, principal `arn:aws:iam::729005488042:user/luciel-sandbox-agent`

---

## What this commit does

Records the Stripe Live + SSM mutations executed for the Arc 7
doctrine pivot — Enterprise becomes FLAT-recurring symmetric with
Pro (monthly + annual self-serve cadences, metering retired, abuse
ceilings enforced in `TIER_ENTITLEMENTS` per Slice 1).

This commit contains:

1. The executable script `scripts/_arc7_stripe_mint_enterprise_symmetry.py`
   that performed the mutations.
2. The full post-state audit JSON `arc7-out/arc7-commit1-slice2-stripe-mint-poststate.json`.
3. The Arc 7 reuse event appended to
   `ops/iam/arc6_stripe_ssm_scope_expansion.json._meta.scope_reuse_events`.
4. This record document.

**No application code changes** — Slice 1 (`9585eda`) already updated
`app/core/config.py`, `app/services/billing_service.py`,
`app/api/v1/billing.py`, `app/policy/entitlements.py`, and
`app/services/billing_webhook_service.py` to reference the new SSM slot
names. With Slice 2/3 landing, those code paths now resolve at runtime
to live Stripe Price IDs.

---

## Stripe Live mutations (idempotency-keyed)

| Slot | Price ID | Amount | Cadence | Idempotency key | Active post-mint |
|---|---|---|---|---|---|
| `enterprise_monthly` (NEW) | `price_1TacunRytQVRVXw71i6eCx1K` | $2,800.00 CAD | recurring/month | `arc7-mint-price-enterprise-monthly-2026-05-24` | ✅ true |
| `enterprise_annual` (NEW) | `price_1TacunRytQVRVXw72JTSAmmq` | $24,000.00 CAD | recurring/year | `arc7-mint-price-enterprise-annual-2026-05-24` | ✅ true |
| `enterprise_floor_annual` (ARCHIVED) | `price_1TaOmPRytQVRVXw7ozfKMFps` | $24,000.00 CAD | recurring/year | `Price.modify(active=False)` | ❌ false (superseded) |

Both new Prices are attached to the existing Enterprise Product
`prod_UZXsY0kLJsFfop` (minted at Arc 6 Commit 4). The archived
`floor_annual` Price metadata now carries `superseded_by_price_id =
price_1TacunRytQVRVXw72JTSAmmq` so Stripe Dashboard reviewers can
trace the deprecation cleanly.

### Control Prices (asserted active=True both pre and post)

- `intro_fee`: `price_1TXNmnRytQVRVXw7GGfyJiaj` (Arc 1 Pilot Intro Fee, $100 CAD one-time)
- `pro_monthly`: `price_1TaOmORytQVRVXw77yRoEC8m` ($349 CAD/mo)
- `pro_annual`: `price_1TaOmORytQVRVXw7ElbQotvK` ($2,990 CAD/yr)

All three remain unmodified by this commit. The Stripe Products
themselves (Pro `prod_UZXsUqCuumvw1v`, Enterprise `prod_UZXsY0kLJsFfop`)
are also unmodified — the doctrine pivot is a Price-level change,
not a Product-level one.

---

## SSM SecureString puts (Type=SecureString, Overwrite=True)

| SSM param name | Value | Version | KMS |
|---|---|---|---|
| `/luciel/production/stripe_price_enterprise_monthly` | `price_1TacunRytQVRVXw71i6eCx1K` | 1 | aws/ssm via `kms:ViaService` |
| `/luciel/production/stripe_price_enterprise_annual` | `price_1TacunRytQVRVXw72JTSAmmq` | 1 | aws/ssm via `kms:ViaService` |

Both Version=1 confirms these are fresh params, not overwrites. Verified
post-put via independent `ssm:GetParameter WithDecryption=True` round-trip
reads from a clean boto3 client (Gate 3) — values matched exactly.

### Existing Arc 6 SSM params NOT touched by this commit

- `/luciel/production/stripe_price_pro_monthly` (Arc 6 Commit 4, Version ≥ 1)
- `/luciel/production/stripe_price_pro_annual` (Arc 6 Commit 4)
- `/luciel/production/stripe_secret_key` (Arc 6 Commit 2, read-only access here)
- `/luciel/production/stripe_webhook_secret` (Arc 6 Commit 2)
- `/luciel/production/stripe_price_intro_fee` (Arc 6 Commit 2)
- `/luciel/production/stripe_price_enterprise_floor_annual` (Arc 6 Commit 4)
  — retirement deferred to Arc 7 Commit 2 so an in-arc rollback remains
  possible. Value is unchanged (still points at the now-archived
  `price_1TaOmPRytQVRVXw7ozfKMFps`).

---

## Credential posture

- **AWS creds:** delivered inline to the python invocation only
  (`AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... python ...`). Never
  written to `~/.aws/credentials`, never `export`ed into the shell,
  never written to any workspace file. Process-scoped only. Wiped at
  process exit. Post-run `env | grep AWS_` returned empty.
- **Stripe Live key:** loaded inside the script from SSM
  `/luciel/production/stripe_secret_key` via `LucielSandboxStripeScope`.
  Never echoed to stderr or stdout. Mode-locked to `sk_live_` before any
  Stripe API call. Never serialised into the audit JSON.
- **Idempotency:** all Stripe Price.create calls used keyed idempotency
  tokens; the archive step is a no-op on an already-inactive Price.
  Re-running the script is safe (the only assertion that would fail on
  re-run is the pre-mutation `floor_annual_pre.active` check; doctored
  as a one-line comment in the script's docstring for that case).

---

## IAM scope-expansion posture

**No new policy authored for this commit.** The Arc 6
`LucielSandboxStripeScope` customer-managed policy already grants:

- `ssm:GetParameter` / `ssm:GetParameters` on
  `parameter/luciel/production/stripe_*`
- `ssm:PutParameter` / `ssm:AddTagsToResource` on the same glob
- `kms:Decrypt` / `kms:Encrypt` / `kms:GenerateDataKey` gated by
  `kms:ViaService=ssm.ca-central-1.amazonaws.com`

The two new SSM param names (`stripe_price_enterprise_monthly`,
`stripe_price_enterprise_annual`) match the existing `stripe_*`
resource glob exactly. This is **policy re-use, not scope expansion**.
The Arc 7 reuse event is recorded under
`ops/iam/arc6_stripe_ssm_scope_expansion.json._meta.scope_reuse_events[0]`
so the audit trail stays single-source.

**5-gate execution:**

1. **Gate 1 — Attach.** Partner attached `LucielSandboxStripeScope`
   to `luciel-sandbox-agent` via IAM Console prior to script invocation.
2. **Gate 2 — Run.** Mint script executed cleanly, exit 0. Full stderr
   trace captured in `arc7-out/arc7-commit1-slice2-stripe-mint.stderr.log`
   (not committed — recreated on demand; the canonical record is this
   markdown + the poststate JSON).
3. **Gate 3 — Verify.** Independent SSM read-back via `boto3.client('ssm').get_parameter`
   confirmed both new params resolve to the minted Price IDs; independent
   `stripe.Price.retrieve` on all 3 affected Price IDs confirmed
   `active=True` for the two new and `active=False` for the archive
   target.
4. **Gate 4 — Detach.** Partner detaches `LucielSandboxStripeScope`
   immediately after this commit lands. (TODO in the close handoff —
   see `_meta.scope_reuse_events[0].detach_status`.)
5. **Gate 5 — Record.** This document + the JSON poststate + the
   `scope_reuse_events[0]` meta entry constitute the record.

---

## Drift posture changes

- `D-enterprise-metering-not-implemented-2026-05-22` (P1) → moves
  closer to closure (the Stripe-side surface for the retired metered
  shape is now archived). Final close lands at Arc 7 Commit 11
  alongside the doctrine entry in `CANONICAL_RECAP §17`.
- No new drifts opened by this commit.

---

## Rollback posture

If we ever need to revert Slice 2/3:

1. **Stripe-side:** re-activate `floor_annual` via Stripe Dashboard
   (Products → Luciel Enterprise → archived Prices → Restore). The
   two new Prices stay minted but can be archived the same way.
2. **SSM-side:** delete the two new params via `aws ssm
   delete-parameter` (requires `ssm:DeleteParameter` which is NOT in
   `LucielSandboxStripeScope`; would need a one-off scope expansion or
   partner-Console action).
3. **Code-side:** revert commit `9585eda` (Slice 1). The runtime then
   stops referencing the new SSM names and resumes using
   `stripe_price_enterprise_floor_annual`.

Rollback is reversible in either direction without data loss; existing
Subscription rows on the old `floor_annual` Price continue to bill
normally regardless of the archive flag (Stripe archive is a
new-Checkout gate, not a renewal gate).
