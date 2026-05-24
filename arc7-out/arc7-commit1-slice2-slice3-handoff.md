# Arc 7 Commit 1 Slice 2 + Slice 3 — Partner-gated handoff

**Anchor:** Arc 7 doctrine pivot (commit `9585eda` on `main`, 2026-05-24).
**Purpose:** Mint two new Stripe Prices for the Enterprise FLAT-recurring
symmetric tier shape, archive the prior `enterprise_floor_annual` Price,
and write the two new Price IDs to SSM SecureString under
`/luciel/production/stripe_price_enterprise_{monthly,annual}`.

---

## Critical scope-expansion finding (5-gate protocol)

Re-reading `ops/iam/arc6_stripe_ssm_scope_expansion.json` confirms that
the existing customer-managed policy
**`LucielSandboxStripeScope`**
(ARN `arn:aws:iam::729005488042:policy/LucielSandboxStripeScope`)
already grants:

- `ssm:GetParameter` / `ssm:GetParameters` on
  `arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/production/stripe_*`
- `ssm:PutParameter` / `ssm:AddTagsToResource` on the same resource glob
- `kms:Decrypt` / `kms:Encrypt` / `kms:GenerateDataKey` gated by
  `kms:ViaService=ssm.ca-central-1.amazonaws.com`

Our two new SSM param names match the existing `stripe_*` resource glob:

- `/luciel/production/stripe_price_enterprise_monthly`
- `/luciel/production/stripe_price_enterprise_annual`

**Implication:** Slice 3 does **NOT** require a new IAM policy authoring
step. The Arc 6 scope-expansion policy already covers the blast radius
exactly. We re-use it under the 5-gate protocol — same policy ARN, same
two-Sid SSM block, same KMS-via-SSM condition. Blast-radius analysis
from `ops/iam/arc6_stripe_ssm_scope_expansion.json._meta` is unchanged:
the agent gains read+write on the 9-key `stripe_*` namespace only;
cannot touch database, JWT keys, magic-link secret, or any non-Stripe
SSM path.

This is a clean re-use, not a scope expansion. The 5-gate cadence
still applies (attach → run → verify → detach → record).

---

## Slice 2 — Stripe Live mutation (mint + archive)

**Script:** `scripts/_arc7_stripe_mint_enterprise_symmetry.py`

**What it does (Stripe Live, account `acct_1TX2BmRytQVRVXw7`):**

1. **Mint** `enterprise_monthly` Price on the existing Enterprise
   Product `prod_UZXsY0kLJsFfop` — `$2,800.00 CAD/mo` recurring.
   Idempotency key: `arc7-mint-price-enterprise-monthly-2026-05-24`.
2. **Mint** `enterprise_annual` Price on the same Product —
   `$24,000.00 CAD/yr` recurring. Idempotency key:
   `arc7-mint-price-enterprise-annual-2026-05-24`.
3. **Archive** the prior `enterprise_floor_annual` Price
   `price_1TaOmPRytQVRVXw7ozfKMFps` (active=False) with metadata
   noting `superseded_by_price_id` = the new annual Price ID.

**Pre/post controls (must all pass):**

- Pre: intro_fee Price + Product still active, Pro/Enterprise
  Products still active, pro_monthly + pro_annual + floor_annual still
  active. Any failure aborts before any mutation.
- Post: intro_fee + pro_monthly + pro_annual still active, new
  enterprise_monthly + enterprise_annual active, floor_annual archived
  (active=False).

**Re-run posture:** Stripe idempotency keys make the two mints safe to
re-run; the archive step is a no-op on an already-inactive Price. Only
the pre-mutation assertion on `floor_annual_pre.active` may need to be
relaxed on the second run (commented out) per the script's docstring.

---

## Slice 3 — SSM SecureString puts

Wrapped into the same script (Step 4) so the four operations land as a
single atomic-ish unit. Two SSM puts:

- `/luciel/production/stripe_price_enterprise_monthly` = (new monthly Price ID)
- `/luciel/production/stripe_price_enterprise_annual` = (new annual Price ID)

Both `Type=SecureString`, `Overwrite=True`, description tags the source.

**The prior `/luciel/production/stripe_price_enterprise_floor_annual`
SSM param is intentionally NOT deleted in this commit.** Its retirement
is bundled into Arc 7 Commit 2 (alongside the
`subscriptions.billing_model` column drop) so any in-arc rollback
remains feasible. After Commit 2 lands, partner removes it via the
Console (or it gets removed under a follow-up SSM scope-attach).

---

## 5-gate protocol — partner steps

The luciel-sandbox-agent IAM principal has the
`LucielSandboxStripeScope` managed policy currently **detached** (this
is the steady-state posture between credential-required commits per
Arc 6 close discipline). Partner steps:

### Gate 1 — Attach (Console)

1. Open IAM → Users → `luciel-sandbox-agent` → Permissions tab.
2. Add permissions → Attach policies directly.
3. Search: `LucielSandboxStripeScope`.
4. Tick the box, click Next, Add permissions.
5. Confirm the Permissions tab now lists `LucielSandboxStripeScope`
   under "Permissions policies".

### Gate 2 — Run the script

Set three environment variables (do NOT write any to disk, do NOT echo
them in shell history; prepend the command with a leading space if your
shell has `HISTCONTROL=ignorespace`):

```bash
export ARC7_STRIPE_SECRET_KEY=sk_live_...          # the existing Live key
export ARC7_STRIPE_PRICE_INTRO_FEE=price_1TXNmnRytQVRVXw7GGfyJiaj
export AWS_ACCESS_KEY_ID=...                       # luciel-sandbox-agent
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=ca-central-1
```

Then run from `/home/user/workspace/luciel/`:

```bash
mkdir -p arc7-out
python scripts/_arc7_stripe_mint_enterprise_symmetry.py \
  > arc7-out/arc7-commit1-slice2-stripe-mint-poststate.json \
  2> arc7-out/arc7-commit1-slice2-stripe-mint.stderr.log
```

The audit JSON ends up in `arc7-out/`; the progress log in the stderr
file (capture both so we have the full trace for the commit record).

### Gate 3 — Verify

```bash
# Read back the two new SSM params (decrypted) and confirm format
aws ssm get-parameter \
  --name /luciel/production/stripe_price_enterprise_monthly \
  --with-decryption --region ca-central-1 \
  --query 'Parameter.[Name,Value,Version]'

aws ssm get-parameter \
  --name /luciel/production/stripe_price_enterprise_annual \
  --with-decryption --region ca-central-1 \
  --query 'Parameter.[Name,Value,Version]'

# Both Values must start with "price_" and be 30 chars long.
# Versions are likely 1 (fresh param) but could be higher if a prior
# re-run already wrote them.

# Confirm the old floor_annual Price is archived
python -c "import stripe, os; \
  stripe.api_key=os.environ['ARC7_STRIPE_SECRET_KEY']; \
  p=stripe.Price.retrieve('price_1TaOmPRytQVRVXw7ozfKMFps'); \
  print('floor_annual active=', p.active, ' (expected False)')"
```

### Gate 4 — Detach (Console)

Symmetric reverse of Gate 1: IAM → `luciel-sandbox-agent` → Permissions
tab → click the X on `LucielSandboxStripeScope` → confirm. The agent
returns to the baseline posture (no SSM write access on Stripe
namespace).

### Gate 5 — Record + commit

Once Gates 1-4 are complete, agent will:

1. Append a section to `ops/iam/arc6_stripe_ssm_scope_expansion.json._meta.scope_reuse_events`
   noting the Arc 7 reuse (attach at <timestamp>, detach at <timestamp>,
   SSM operations performed, no policy change required).
2. Stage the script + audit JSON + meta update + handoff doc.
3. Commit as `arc7(c1-s2): mint enterprise monthly/annual prices and
   archive floor_annual` and push.
4. Mark Slice 2 + Slice 3 complete and move to Commit 2 (Alembic).

---

## Rollback posture

If Gates 1-3 succeed but Gate 4 (detach) is forgotten, the agent
retains SSM write access on the Stripe namespace until detached. This
is detectable by re-running `aws iam list-attached-user-policies` and
spotting `LucielSandboxStripeScope` still in the list.

If the Stripe mint succeeds but SSM put fails, the new Prices exist in
Stripe but the application's PRICE_ID_KEY lookup in `billing_service.py`
will raise `KeyError` until the SSM values are populated. The Arc 6
intro-fee path remains unaffected (different SSM key). Partner can
either re-run the script (idempotent) or set the SSM params manually
via the Console.

If the Stripe mint succeeds but the archive step fails, the floor_annual
Price stays minted-and-active alongside the new annual Price. The
application code (post-Slice 1) no longer references floor_annual, so
no user can land on it via Checkout. Partner can run the archive
manually via Stripe Dashboard (Products → Luciel Enterprise → click
the floor_annual Price → Archive).

If any control assertion fires after the mint+archive, the partial
state is recoverable: new Prices can be manually archived in Stripe
Dashboard, SSM params can be deleted via Console, and the codebase
post-Slice-1 still references the OLD slot names that we removed —
which means a rollback would need to revert commit `9585eda` to
restore consistency. Practical posture: if Gate 3 verification fails,
do NOT detach; ping the agent for triage.
