# Step 30a.1 — Production deploy (Stripe Prices first, then schema, then code)

**Scope:** Land the Step 30a.1 tiered-self-serve work onto production in
one rollout, closing
`D-billing-team-company-not-self-serve-2026-05-13` end-to-end.

Three sequential phases, in this order:

1. **Stripe Prices** — operator creates 5 new Stripe Prices in the live
   Stripe dashboard (Individual annual; Team monthly + annual; Company
   monthly + annual). The pre-existing Individual monthly price stays
   put. Each price-id lands in SSM Parameter Store under the matching
   `STRIPE_PRICE_*` key. **No code on prod yet — this phase only
   populates secrets the new image will read.**
2. **Alembic migration `c2a1b9f30e15`** — advances production RDS from
   `b8e74a3c1d52` (Step 30a head) to `c2a1b9f30e15` (Step 30a.1 head),
   adding `subscriptions.billing_cadence` and
   `subscriptions.instance_count_cap`. Additive only. Default values
   backfill cleanly (`billing_cadence='monthly'`,
   `instance_count_cap=3`) so the existing Individual-monthly customer
   row is consistent without touching it.
3. **Application image roll** — new task-definition revisions
   `luciel-backend:43` and `luciel-worker:21` roll in. The new image
   carries (a) `BillingService.resolve_price_id(tier, cadence)`, (b)
   `TierProvisioningService.pre_mint_for_tier(tenant_id, tier)`, (c)
   `AdminService._enforce_tier_scope` 402 guard, (d) the
   teammate-invite path on `/admin/luciel-instances` POST. The website
   is already on `main` via Luciel-Website PR #3 — Amplify build is
   green, the apex domain CloudFront cache is stale per
   `D-vantagemind-dns-cloudfront-mismatch-2026-05-13` (operator-side
   manual invalidation step is in Phase 5).

**Pre-deploy state (presumed, verified at Step 1):**

- Code on `main` HEAD: the merge commit of PR #49 (Step 30a.1
  backend) on `aryanonline/Luciel`. The closing tag
  `step-30a-1-tiered-self-serve-complete` will be cut on the
  doc-truthing commit and point one or two forward from the merge.
- Website on `main` HEAD: the merge commit of Luciel-Website PR #3 on
  `aryanonline/Luciel-Website`. Amplify build green; deploy lands on
  the Amplify-issued URL immediately, apex domain catches up after
  manual CloudFront invalidation (Phase 5).
- Last production-deployed application image: `luciel-backend:42`,
  ECR digest
  `sha256:8fbc267d4126095ea10fd11a9022c2d28fea69143b3c04dd3d85696116776209`,
  built from `main` post Step 31.2 + Step 32.
- Presumed production RDS Alembic revision: `b8e74a3c1d52` (Step 30a
  head). If prod RDS is still at `3dbbc70d0105` (Step 24.5c head),
  Alembic walks `3dbbc70d0105 → b8e74a3c1d52 → c2a1b9f30e15` in one
  invocation. **Step 1b is the load-bearing check.**
- ECR repo:
  `729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend`
- Cluster: `luciel-cluster`. Services: `luciel-backend-service`,
  `luciel-worker-service`. Region: `ca-central-1`.
- ALB direct URL (documented smoke-test target per
  `D-vantagemind-dns-cloudfront-mismatch-2026-05-13`):
  `https://luciel-alb-1617994381.ca-central-1.elb.amazonaws.com`

**Default-safety claim (why this rollout is safe):**

The Step 30a.1 migration is **additive only** — two new nullable
columns on `subscriptions` with server-side defaults that hold the v1
single-SKU values (`billing_cadence='monthly'`,
`instance_count_cap=3`). The migration backfills the existing rows
with those defaults, so the post-migration state for the one existing
Individual-monthly customer is byte-identical at read-time to the
pre-migration state.

**Why Stripe Prices must go first:** the new image reads the price-id
config keys at boot. If the new image rolls before the keys are
populated in SSM, the `/billing/checkout` endpoint will 500 on the
first Team or Company customer attempt with
`stripe_price_id_not_configured`. The Individual monthly path keeps
working (its price-id is the pre-existing key), so the failure window
is narrow but real. Populating SSM first avoids it.

**Why schema must go before code:** the new image's webhook handler
writes `billing_cadence` and `instance_count_cap` on every
`checkout.session.completed`. If the new image rolls before the
migration applies, the first new customer's webhook fails with
`psycopg.errors.UndefinedColumn`. The Stripe redelivery loop will
retry; our subscription state diverges from Stripe's until we catch
up. The migration must complete and be verified before ECS task-def
update.

**Why this isn't a maintenance window:** the migration is additive
only and the old code path doesn't write the new columns. The period
between migration-complete and task-def update operates correctly on
both code paths — old tasks keep serving traffic without touching the
new columns, new tasks roll in with the columns now present. No
write-side or read-side conflict exists. ECS rolling deploy with
circuit breaker remains the rollout mechanism; zero expected
downtime.

---

## Step 0 — Sanity check from operator workstation

```powershell
cd C:\Users\aryan\Projects\Business\Luciel

# Confirm you are on the right commit
git checkout main
git pull --ff-only origin main
git log --oneline -1
# expect: the merge commit of PR #49 (Step 30a.1 backend)

# Confirm the closing tag is reachable (only after the doc-truthing commit lands)
git show --no-patch step-30a-1-tiered-self-serve-complete | head -3
# expect: tag pointing at the doc-truthing commit on main

# Confirm AWS profile (per operator-patterns.md Hazard 2)
aws configure list-profiles
# expect exactly: default

# Confirm Stripe CLI authenticated to the live mode account
stripe config --list | Select-String -Pattern 'live_mode|account_id'
# expect: live_mode = true, account_id = acct_... (the Luciel production Stripe account)
```

## Step 1 — Verify presumed production state

Three independent checks. All three must match presumption before any
forward action. If any does not match, **stop and revise the runbook**
before continuing.

### Step 1a — Verify production ECS task-def revisions

```powershell
aws ecs describe-services `
  --cluster luciel-cluster `
  --services luciel-backend-service luciel-worker-service `
  --region ca-central-1 `
  --query 'services[].{name:serviceName,taskDef:taskDefinition,desired:desiredCount,running:runningCount}' `
  --output table
# expect: luciel-backend:42 desired=running, luciel-worker:20 desired=running
```

### Step 1b — Verify production RDS Alembic revision (load-bearing)

From inside a one-shot Fargate task with Alembic and the RDS
credentials (the operator-patterns.md "bastion task" pattern):

```powershell
aws ecs run-task `
  --cluster luciel-cluster `
  --task-definition luciel-backend:42 `
  --launch-type FARGATE `
  --network-configuration "awsvpcConfiguration={subnets=[subnet-0e54df62d1a4463bc,subnet-0e95d953fd553cbd1],securityGroups=[sg-0f2e317f987925601],assignPublicIp=DISABLED}" `
  --overrides '{\"containerOverrides\":[{\"name\":\"luciel-backend\",\"command\":[\"alembic\",\"current\"]}]}' `
  --region ca-central-1 `
  --query 'tasks[0].taskArn' --output text
# Tail the task's stdout via CloudWatch:
# expect: b8e74a3c1d52 (head)
```

If the output is `3dbbc70d0105`, the prior runbook
`step-30a-and-31-2-prod-deploy.md` did not land its migration —
**stop and run that runbook first**.

### Step 1c — Verify production Stripe Customer Portal configured

```powershell
stripe billing_portal configurations list --limit 1
# expect: at least one configuration with active=true and the price IDs we use
```

If the portal is not configured, none of the Step 30a.1 plan-change
flows will work even after this rollout. Configure it in the Stripe
dashboard first.

---

## Phase 1 — Create 5 new Stripe Prices

For each price below, run in PowerShell with the Stripe CLI in live
mode. Each command prints the new `price_id`; capture all 5 before
moving on. The pre-existing Individual-monthly price stays as it is
and is not re-created.

```powershell
# (1) Individual annual — $300 CAD/yr — 14-day trial — cap 3
stripe prices create `
  --currency cad `
  --unit-amount 30000 `
  --recurring "interval=year" `
  --product prod_XXX_individual `
  --tax-behavior inclusive `
  --metadata "tier=individual" `
  --metadata "cadence=annual" `
  --metadata "instance_count_cap=3"

# (2) Team monthly — $300 CAD/mo — 7-day trial — cap 10
stripe prices create `
  --currency cad `
  --unit-amount 30000 `
  --recurring "interval=month" `
  --product prod_XXX_team `
  --tax-behavior inclusive `
  --metadata "tier=team" `
  --metadata "cadence=monthly" `
  --metadata "instance_count_cap=10"

# (3) Team annual — $3,000 CAD/yr — 7-day trial — cap 10
stripe prices create `
  --currency cad `
  --unit-amount 300000 `
  --recurring "interval=year" `
  --product prod_XXX_team `
  --tax-behavior inclusive `
  --metadata "tier=team" `
  --metadata "cadence=annual" `
  --metadata "instance_count_cap=10"

# (4) Company monthly — $2,000 CAD/mo — 7-day trial — cap 50
stripe prices create `
  --currency cad `
  --unit-amount 200000 `
  --recurring "interval=month" `
  --product prod_XXX_company `
  --tax-behavior inclusive `
  --metadata "tier=company" `
  --metadata "cadence=monthly" `
  --metadata "instance_count_cap=50"

# (5) Company annual — $20,000 CAD/yr — 7-day trial — cap 50
stripe prices create `
  --currency cad `
  --unit-amount 2000000 `
  --recurring "interval=year" `
  --product prod_XXX_company `
  --tax-behavior inclusive `
  --metadata "tier=company" `
  --metadata "cadence=annual" `
  --metadata "instance_count_cap=50"
```

If the `prod_XXX_individual`, `prod_XXX_team`, `prod_XXX_company`
Stripe Product objects do not yet exist, create them first via
`stripe products create --name "Luciel — Individual"` (and Team /
Company), then re-run the price-create commands with the resulting
`prod_id`.

**Capture the 5 new `price_id` values into a local note. They are the
input to Phase 2.**

## Phase 2 — Populate SSM Parameter Store

For each captured price-id, write the corresponding SSM parameter.
Use `SecureString` to match the existing Stripe-secret discipline
(Pattern E for rotation; the secret-rotation runbook covers
re-issuance if a key leaks).

```powershell
# Individual annual
aws ssm put-parameter `
  --name "/luciel/prod/stripe_price_individual_annual" `
  --value "price_..." `
  --type SecureString `
  --overwrite `
  --region ca-central-1

# Team monthly
aws ssm put-parameter `
  --name "/luciel/prod/stripe_price_team_monthly" `
  --value "price_..." `
  --type SecureString `
  --overwrite `
  --region ca-central-1

# Team annual
aws ssm put-parameter `
  --name "/luciel/prod/stripe_price_team_annual" `
  --value "price_..." `
  --type SecureString `
  --overwrite `
  --region ca-central-1

# Company monthly
aws ssm put-parameter `
  --name "/luciel/prod/stripe_price_company_monthly" `
  --value "price_..." `
  --type SecureString `
  --overwrite `
  --region ca-central-1

# Company annual
aws ssm put-parameter `
  --name "/luciel/prod/stripe_price_company_annual" `
  --value "price_..." `
  --type SecureString `
  --overwrite `
  --region ca-central-1
```

Verify all 5 land:

```powershell
aws ssm get-parameters-by-path `
  --path "/luciel/prod/" `
  --recursive `
  --region ca-central-1 `
  --query "Parameters[?contains(Name, 'stripe_price')].Name" `
  --output table
# expect 6 names total (including the pre-existing stripe_price_individual_monthly)
```

The new task-def revision in Phase 4 will reference these parameters
via the `secrets` block. The currently-running `:42` image does not
read them and will not be disturbed.

## Phase 3 — Run Alembic migration `c2a1b9f30e15`

The same one-shot Fargate-task pattern as Step 1b, but with
`alembic upgrade head` as the command. Use the `:42` task definition
because it has the right image; Alembic reads
`alembic/versions/c2a1b9f30e15_*.py` from the application code
mounted in the container.

**Wait** — the `:42` image was built before commit A landed.
`c2a1b9f30e15` is not in its filesystem. Use the new image instead;
build it first if it isn't in ECR yet:

```powershell
# Build the new image from main HEAD
docker build -t luciel-backend:step-30a-1 .
docker tag luciel-backend:step-30a-1 729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend:step-30a-1
aws ecr get-login-password --region ca-central-1 | docker login --username AWS --password-stdin 729005488042.dkr.ecr.ca-central-1.amazonaws.com
docker push 729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend:step-30a-1

# Capture the new image digest
$IMAGE_DIGEST = aws ecr describe-images `
  --repository-name luciel-backend `
  --image-ids imageTag=step-30a-1 `
  --region ca-central-1 `
  --query 'imageDetails[0].imageDigest' --output text
Write-Host "New image digest: $IMAGE_DIGEST"
# Save this digest — Phase 4 task-def uses it
```

Run the migration with the new image but the OLD task-def's `secrets`
block (the new SSM keys are already in place from Phase 2; we just
need the existing DB creds):

```powershell
# Build a throwaway one-shot task-def revision pointing at the new image
# (Use td-backend-rev43-migration.json — clone of rev42 with the new image digest swapped in
# and the command overridden to alembic upgrade head)

aws ecs run-task `
  --cluster luciel-cluster `
  --task-definition luciel-backend-migration:1 `
  --launch-type FARGATE `
  --network-configuration "awsvpcConfiguration={subnets=[subnet-0e54df62d1a4463bc,subnet-0e95d953fd553cbd1],securityGroups=[sg-0f2e317f987925601],assignPublicIp=DISABLED}" `
  --overrides '{\"containerOverrides\":[{\"name\":\"luciel-backend\",\"command\":[\"alembic\",\"upgrade\",\"head\"]}]}' `
  --region ca-central-1 `
  --query 'tasks[0].taskArn' --output text
```

Tail the task's CloudWatch log group until you see
`INFO  [alembic.runtime.migration] Running upgrade b8e74a3c1d52 -> c2a1b9f30e15`
followed by clean exit code 0.

Verify the migration head advanced:

```powershell
# Same one-shot pattern but with alembic current
aws ecs run-task `
  --cluster luciel-cluster `
  --task-definition luciel-backend-migration:1 `
  --launch-type FARGATE `
  --network-configuration "awsvpcConfiguration={subnets=[subnet-0e54df62d1a4463bc,subnet-0e95d953fd553cbd1],securityGroups=[sg-0f2e317f987925601],assignPublicIp=DISABLED}" `
  --overrides '{\"containerOverrides\":[{\"name\":\"luciel-backend\",\"command\":[\"alembic\",\"current\"]}]}' `
  --region ca-central-1 `
  --query 'tasks[0].taskArn' --output text
# expect: c2a1b9f30e15 (head)
```

If migration fails, the transactional DDL rolls back to the pre-state
cleanly. Do not proceed to Phase 4. Investigate the failure, fix
forward, re-run. **Never** issue a manual `ALTER TABLE`; a future
`alembic upgrade head` won't know the column exists and will fail
non-deterministically.

## Phase 4 — Roll new task-def revisions

Author `td-backend-rev43.json` and `td-worker-rev21.json` from the
current `:42` / `:20` JSON dumps, swapping the image digest to the
Phase 3 digest and appending the 5 new SSM keys to the `secrets`
block:

```json
{
  "name": "STRIPE_PRICE_INDIVIDUAL_ANNUAL",
  "valueFrom": "arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/prod/stripe_price_individual_annual"
},
{
  "name": "STRIPE_PRICE_TEAM_MONTHLY",
  "valueFrom": "arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/prod/stripe_price_team_monthly"
},
{
  "name": "STRIPE_PRICE_TEAM_ANNUAL",
  "valueFrom": "arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/prod/stripe_price_team_annual"
},
{
  "name": "STRIPE_PRICE_COMPANY_MONTHLY",
  "valueFrom": "arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/prod/stripe_price_company_monthly"
},
{
  "name": "STRIPE_PRICE_COMPANY_ANNUAL",
  "valueFrom": "arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/prod/stripe_price_company_annual"
}
```

Register and update services:

```powershell
aws ecs register-task-definition `
  --cli-input-json file://td-backend-rev43.json `
  --region ca-central-1
# expect: revision 43 returned

aws ecs register-task-definition `
  --cli-input-json file://td-worker-rev21.json `
  --region ca-central-1
# expect: revision 21 returned

aws ecs update-service `
  --cluster luciel-cluster `
  --service luciel-backend-service `
  --task-definition luciel-backend:43 `
  --force-new-deployment `
  --region ca-central-1

aws ecs update-service `
  --cluster luciel-cluster `
  --service luciel-worker-service `
  --task-definition luciel-worker:21 `
  --force-new-deployment `
  --region ca-central-1
```

Watch the rolling deploy. Both services have
`deploymentCircuitBreaker` enabled with rollback, so a sustained
failure auto-reverts to `:42` / `:20`. Wait for both services to
report `deploymentStatus: PRIMARY` with `runningCount` matching
`desiredCount`:

```powershell
aws ecs describe-services `
  --cluster luciel-cluster `
  --services luciel-backend-service luciel-worker-service `
  --region ca-central-1 `
  --query 'services[].deployments[?status==`PRIMARY`].{name:taskDefinition,status:rolloutState,running:runningCount,desired:desiredCount}' `
  --output table
# expect: COMPLETED for both, running == desired
```

Tail CloudWatch for the post-deploy window — zero ERROR / Traceback /
CRITICAL across both log groups for 5 minutes:

```powershell
aws logs tail /ecs/luciel-backend --since 10m --region ca-central-1 `
  | Select-String -Pattern "ERROR|Traceback|CRITICAL"
# expect: no matches
```

## Phase 5 — Manual CloudFront invalidation (marketing site)

Per `D-vantagemind-dns-cloudfront-mismatch-2026-05-13`, the apex
domain `vantagemind.ai` serves stale CloudFront-cached assets after
Amplify deploys. The Amplify build for Luciel-Website PR #3 is
already green at this point; the new Pricing page, Signup flow, and
Dashboard Team tab are reachable at the Amplify-issued URL
immediately, and need a manual CloudFront invalidation to reach the
apex domain.

```powershell
# Find the CloudFront distribution that fronts vantagemind.ai
aws cloudfront list-distributions `
  --query "DistributionList.Items[?Aliases.Items[?@=='vantagemind.ai']].{Id:Id,Domain:DomainName}" `
  --output table

# Invalidate everything
$DIST_ID = "<id from above>"
aws cloudfront create-invalidation `
  --distribution-id $DIST_ID `
  --paths "/*"
# expect: Invalidation created with Status=InProgress; takes ~5min
```

## Phase 6 — Smoke tests against ALB direct URL

Run three smoke flows against
`https://luciel-alb-1617994381.ca-central-1.elb.amazonaws.com` (the
ALB direct URL, per
`D-vantagemind-dns-cloudfront-mismatch-2026-05-13`) and the
Amplify-issued URL of the marketing site. The apex domain is fine to
spot-check once the CloudFront invalidation completes but is not the
authoritative smoke target during the deploy window.

### Smoke 1 — Sarah, Individual annual

1. From a fresh browser session, open the Amplify-issued URL,
   navigate to `/pricing`.
2. Toggle the cadence pill to **Annual**. Verify the chip reads
   "2 months free". Verify the three displayed prices are $300, $3,000,
   $20,000.
3. Click **Get started** on the Individual card. Land on `/signup`
   with `?tier=individual&cadence=annual`.
4. Submit `sarah+30a1@example.com` (or operator-side throwaway).
   Verify the trial copy reads "One bill a year" with no
   `14-day-trial` mention (annual flow doesn't ship a trial).
5. Complete Stripe Checkout with the Stripe test card
   `4242 4242 4242 4242`.
6. Wait for the magic-link email (CloudWatch `[magic-link-email]`
   marker confirms the SES send). Click the link.
7. Land on `/dashboard`. Verify the **Team** tab is **not** visible
   (Individual tier shouldn't show it). Verify Luciels tab shows
   "0 of 3 used" cap counter.
8. Click **Create new Luciel**. Verify the scope dropdown is **not
   rendered** (only one choice for Individual: agent). Submit.
   Verify the Luciel appears in the list and the counter advances to
   "1 of 3 used".

### Smoke 2 — Marcus, Team monthly

1. Same browser-incognito start. Toggle Pricing cadence to **Monthly**.
2. Click **Get started** on the Team card. Land on `/signup` with
   `?tier=team&cadence=monthly`. Verify the trial copy reads "7-day
   free trial".
3. Submit `marcus+30a1@example.com`. Complete Checkout.
4. Magic-link → `/dashboard`. Verify **Team** tab is now visible.
   Verify Luciels tab shows "1 of 10 used" cap counter
   (pre-mint shipped one default agent + one default domain at
   signup per `TierProvisioningService`).
5. Open the Team tab. Verify **Add teammate** form is present.
   Submit `teammate+30a1@example.com`. Verify a magic-link email
   fires for the teammate (CloudWatch marker again).
6. From a second fresh browser, click the teammate's magic link.
   Verify the teammate lands on `/dashboard` and sees the Luciels
   under the team's domain scope but not the operator's other
   tenants.
7. Back as Marcus, click **Create new Luciel**. Verify the scope
   dropdown renders with two choices: "Just me (agent)" and "Our team
   (domain)". Create one of each. Verify the cap counter advances to
   "3 of 10 used".

### Smoke 3 — Diane, Company monthly

1. Pricing page → **Monthly** → Company card. Note the primary CTA
   is **Book a demo** (the hybrid default). Append `?showSkip=1` to
   the URL and reload — verify the "Skip the call →" secondary CTA
   becomes visible.
2. Click the **Skip the call →** link. Land on `/signup` with
   `?tier=company&cadence=monthly`.
3. Submit `diane+30a1@example.com`. Complete Checkout.
4. Magic-link → `/dashboard`. Verify **Team** tab visible.
   Luciels tab cap counter "1 of 50 used".
5. **Create new Luciel** — verify scope dropdown renders with three
   choices: "Just me (agent)", "Our team (domain)", "Whole company
   (tenant)". Create one of each. Verify cap counter advances to
   "4 of 50 used".

If any of the three smoke flows fails, **roll back per Phase 7**.
Capture the failure mode and the CloudWatch log range; open an
incident-grade drift on the spot.

## Phase 7 — Rollback (only if Phase 6 fails)

ECS rolling deploy with circuit breaker is the first-line auto-fix; a
hard-stuck deploy reverts to `:42` / `:20` without operator action.
For partial-state failures (image deployed, but a Phase 6 smoke
fails), revert manually:

```powershell
aws ecs update-service `
  --cluster luciel-cluster `
  --service luciel-backend-service `
  --task-definition luciel-backend:42 `
  --force-new-deployment `
  --region ca-central-1

aws ecs update-service `
  --cluster luciel-cluster `
  --service luciel-worker-service `
  --task-definition luciel-worker:20 `
  --force-new-deployment `
  --region ca-central-1
```

The migration is **not** rolled back. The two new columns stay in
place under their server-side defaults; the `:42` image does not read
them, and any future re-roll of `:43` finds the schema already
correct. (Rolling back a migration with live customer data is a
separate, more invasive operation that this runbook does not cover
because the migration is forward-only-additive.)

The 5 new SSM parameters stay in place. They are read only by `:43+`;
`:42` does not list them in its `secrets` block.

The 5 new Stripe Prices stay live in Stripe. Stripe does not support
deleting prices once they are created; the operator-side mitigation
is to mark them `active=false` if they need to be hidden from the
Customer Portal pending a re-deploy. The new image refusing to roll
does not cause Stripe to charge anyone — no customer can reach
`/checkout` for the new tiers until `:43` is live.

## Phase 8 — Post-deploy doc-truthing

Already landed on the deploy commit per the agent-side doc-truthing
pass:

- `CANONICAL_RECAP.md` §12 — new Step 30a.1 row (Billing category).
- `CANONICAL_RECAP.md` §14 — price table promoted from "reserved" to
  "shipped" with all 6 SKUs and the per-tier cap.
- `ARCHITECTURE.md` §3.2.13 — "Annual pricing, multi-SKU… out of
  scope" sentence replaced with the shipped-surface description; new
  tier↔scope mapping paragraph added.
- `DRIFTS.md` §3 —
  `D-billing-team-company-not-self-serve-2026-05-13` full-close
  stanza; two new derivative drifts opened
  (`D-tier-scope-mapping-service-layer-only-2026-05-13`,
  `D-vantagemind-dns-cloudfront-mismatch-2026-05-13`).

Final operator action after a clean Phase 6:

```powershell
# Cut the closing tag on the doc-truthing commit (the merge of PR #49 + docs)
git checkout main
git pull --ff-only origin main
git tag -a step-30a-1-tiered-self-serve-complete `
  -m "Step 30a.1 — Tiered self-serve (Individual + Team + Company)"
git push origin step-30a-1-tiered-self-serve-complete
```

The closing tag is the deploy attestation referenced in
`CANONICAL_RECAP.md` §12, `ARCHITECTURE.md` §3.2.13, and
`DRIFTS.md` §3.

---

## Triangulation

- **Canonical recap:** §12 Step 30a.1 row (the deliverable this
  runbook lands); §14 (the 6-SKU price table this runbook configures).
- **Architecture:** §3.2.13 (the seven-route Stripe surface this
  runbook deploys the multi-tier shape of); §3.6 (the marketing-site
  CloudFront path Phase 5 manually invalidates).
- **Drifts:** `D-billing-team-company-not-self-serve-2026-05-13`
  (closes); `D-tier-scope-mapping-service-layer-only-2026-05-13`
  (opens — derivative); `D-vantagemind-dns-cloudfront-mismatch-2026-05-13`
  (opens — derivative; Phase 5 records the manual invalidation
  workaround until that drift closes).
- **Pull requests:**
  [Luciel#49](https://github.com/aryanonline/Luciel/pull/49) (backend
  PR with 9 commits A–H + docs);
  [Luciel-Website#3](https://github.com/aryanonline/Luciel-Website/pull/3)
  (website PR with 2 commits A+B + C).
- **Closing tag:** `step-30a-1-tiered-self-serve-complete`, cut on
  the doc-truthing commit on backend `main` after Phase 6 smoke
  passes.
