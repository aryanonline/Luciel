# Step 30a + Step 31.2 — Production deploy (schema-first, then code, single rollout)

**Scope:** Land both currently-undeployed pieces of work onto production
in one schema-first rollout, closing the next stanza of
`D-step-24-5c-and-31-schema-and-code-undeployed-to-prod-2026-05-12`
(extended on 2026-05-13 to cover Step 30a + Step 31.2):

1. The Alembic migration `b8e74a3c1d52` (Step 30a — `subscriptions`
   table, with composite indexes `ix_subscriptions_tenant_active` and
   `ix_subscriptions_stripe_customer`, UNIQUE on
   `stripe_subscription_id`, `user_id` FK to `users.id` with
   `ON DELETE RESTRICT`, JSONB `provider_snapshot`) advances production
   RDS from revision `3dbbc70d0105` (Step 24.5c head — assumed already
   landed from the prior runbook) to `b8e74a3c1d52`.
2. The application image built from `main` HEAD (after PR #48 +
   Luciel-Website PR #2 merge) rolls onto `luciel-backend-service` and
   `luciel-worker-service` as new task-definition revisions `:40` and
   `:20`. This image carries the Step 30a code (Stripe webhook handler
   `BillingWebhookService`, `OnboardingService` magic-link mint,
   `SES` magic-link email send, `/signup` → Checkout → portal flow)
   **and** the Step 31.2 code (`SessionCookieAuthMiddleware`, lifted
   v1 `luciel_instance_id` carve-out on `/admin/embed-keys`).

**Pre-deploy state (presumed, verified at Step 1):**

- Code on `main` HEAD: the merge commit of PR #48 (Step 31.2 backend) on
  `aryanonline/Luciel`. Closing tag
  `step-31-2-cookie-bridge-and-instance-embed-keys-complete` points at
  `ab0d3c6`; the merge commit will be one forward. Step 30a is already
  on `main` at `4aea209`.
- Last production-deployed application image: pinned in
  `td-backend-rev39.json` and `td-worker-rev19.json`, ECR digest
  `sha256:f0bf303272fb0801eefc4cf0d20d2ddb624f2a5f60c8e845cbe422869739f863`.
- Presumed production RDS Alembic revision: `3dbbc70d0105`. This
  presumes the Step 24.5c+31 runbook landed; if it did not (i.e. prod
  RDS is still at `a7c1f4e92b85`), the migration chain
  `a7c1f4e92b85 → 3dbbc70d0105 → b8e74a3c1d52` will roll in one Alembic
  invocation — Alembic walks the chain in order automatically. **Step 1b
  is the load-bearing check.**
- ECR repo:
  `729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend`
- Cluster: `luciel-cluster`. Services: `luciel-backend-service`,
  `luciel-worker-service`. Region: `ca-central-1`.

**Step 31.2 has no migration.** The `api_keys.luciel_instance_id` FK
column predates this work. The Step 31.2 commits (`f90b9a2`, `0322ade`,
`c88dfec`) are code-only — middleware mount + API surface tightening +
e2e harness. The only schema delta in this deploy is Step 30a's
`b8e74a3c1d52`.

**Default-safety claim (why schema-first is mandatory):**

The Step 30a migration is **additive only** — one new table, two
composite indexes, one UNIQUE constraint, one FK. No destructive
operations, no column type changes on existing tables, no NOT NULL
backfills. Inside Alembic's PostgreSQL transactional DDL, any failed
step rolls back to the pre-migration state cleanly.

**Why schema *must* go first:** the new application image executes
`BillingWebhookService.handle_event()` on the first Stripe webhook POST
to `/billing/webhook` after rollout. That handler writes to
`subscriptions`. If the new image rolls before the migration applies,
the first webhook fails with
`psycopg.errors.UndefinedTable: relation "subscriptions" does not exist`
— and Stripe's redelivery loop will retry until our subscription state
diverges from theirs. The migration must complete and be verified
before ECS task-def update.

**Why this isn't a maintenance window:** the migration is additive only
and the old code path doesn't touch `subscriptions` (the table didn't
exist when the `:39` image was built). The period between
migration-complete and task-def update operates correctly on both code
paths — old tasks keep serving traffic with no reference to the new
table, new tasks roll in with the table now present. No write-side or
read-side conflict exists. ECS rolling deploy with circuit breaker
remains the rollout mechanism; zero expected downtime.

---

## Step 0 — Sanity check from operator workstation

```powershell
cd C:\Users\aryan\Projects\Business\Luciel

# Confirm you are on the right commit
git checkout main
git pull --ff-only origin main
git log --oneline -1
# expect: the merge commit of PR #48 (Step 31.2 backend)

# Confirm the closing tags are reachable
git show --no-patch step-31-2-cookie-bridge-and-instance-embed-keys-complete | head -3
# expect: tag pointing at ab0d3c6

# Step 30a is already on main from the prior 30a merge (commit 4aea209)
git log --oneline 4aea209 -1
# expect: step 30a — Subscription billing (...) (#47)

# Confirm AWS profile (per operator-patterns.md Hazard 2)
aws configure list-profiles
# expect exactly: default
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
# expect: luciel-backend:39 desired=running, luciel-worker:19 desired=running
```

### Step 1b — Verify production Alembic revision (load-bearing)

Run a one-shot from the currently-deployed `:39` task-def revision:

```powershell
$PROBE_TASK = aws ecs run-task `
  --cluster luciel-cluster `
  --task-definition luciel-backend:39 `
  --launch-type FARGATE `
  --network-configuration "awsvpcConfiguration={subnets=[<private-subnet-ids>],securityGroups=[<sg-id>],assignPublicIp=DISABLED}" `
  --overrides '{\"containerOverrides\":[{\"name\":\"luciel-backend\",\"command\":[\"alembic\",\"current\"]}]}' `
  --region ca-central-1 `
  --query 'tasks[0].taskArn' --output text

aws ecs wait tasks-stopped --cluster luciel-cluster --tasks $PROBE_TASK --region ca-central-1
# Pull the log lines (CloudWatch Logs Insights, /ecs/luciel-backend, last 10 min):
#   fields @timestamp, @message | filter @message like /alembic/ | sort @timestamp desc | limit 5
```

**Expected output:** `3dbbc70d0105 (head)` (if the prior Step 24.5c+31
runbook landed) **or** `a7c1f4e92b85` (if it did not).

**Either is acceptable** — the new image's Alembic walks the chain
either way. But record which one you observe; it determines how many
revisions Step 2 reports rolling through.

### Step 1c — Verify ECR digest of the running image

```powershell
$RUNNING_DIGEST = aws ecs describe-task-definition `
  --task-definition luciel-backend:39 --region ca-central-1 `
  --query 'taskDefinition.containerDefinitions[0].image' --output text
Write-Host $RUNNING_DIGEST
# expect: ...@sha256:f0bf303272fb0801eefc4cf0d20d2ddb624f2a5f60c8e845cbe422869739f863
```

## Step 2 — Apply Alembic migration on production RDS

Same one-shot pattern as Step 24.5c+31. The `:39` image **does not
contain** `b8e74a3c1d52` in its `alembic/versions/` directory (Step 30a
introduced it). So we build the new image first (Steps 3.0–3.4), then
return here to run the migration from the newly-registered `:40`
revision, **without** updating the running service. Steps 3.0–3.4 below
happen first, then Step 2 (revisited), then Step 3.5.

### Step 2 (revisited, after Step 3.0–3.4) — invoke the migration

```powershell
$MIGRATE_TASK = aws ecs run-task `
  --cluster luciel-cluster `
  --task-definition luciel-backend:40 `
  --launch-type FARGATE `
  --network-configuration "awsvpcConfiguration={subnets=[<private-subnet-ids>],securityGroups=[<sg-id>],assignPublicIp=DISABLED}" `
  --overrides '{\"containerOverrides\":[{\"name\":\"luciel-backend\",\"command\":[\"alembic\",\"upgrade\",\"head\"]}]}' `
  --region ca-central-1 `
  --query 'tasks[0].taskArn' --output text
Write-Host "Migration task: $MIGRATE_TASK"

aws ecs wait tasks-stopped --cluster luciel-cluster --tasks $MIGRATE_TASK --region ca-central-1
```

Expected log lines (`/ecs/luciel-backend`, Logs Insights):

If prod was at `3dbbc70d0105`:
```
INFO  [alembic.runtime.migration] Running upgrade 3dbbc70d0105 -> b8e74a3c1d52, Step 30a: subscriptions table
```

If prod was at `a7c1f4e92b85` (chain walk):
```
INFO  [alembic.runtime.migration] Running upgrade a7c1f4e92b85 -> 3dbbc70d0105, step 24.5c: conversations + identity_claims
INFO  [alembic.runtime.migration] Running upgrade 3dbbc70d0105 -> b8e74a3c1d52, Step 30a: subscriptions table
```

with no `ERROR` / `Traceback` / `CRITICAL` lines following. Then re-run
the Step 1b probe (this time expect `b8e74a3c1d52 (head)`).

### Step 2 acceptance criteria

- `alembic current` against prod RDS returns `b8e74a3c1d52 (head)`.
- Optional schema-level verification via scoped `psql` from a bastion
  or another one-shot:
  - `\dt subscriptions` returns 1 row.
  - `\d subscriptions` shows columns: `id` (BIGSERIAL PK), `tenant_id`
    VARCHAR(100), `user_id` UUID FK to `users.id` ON DELETE RESTRICT,
    `customer_email` VARCHAR(320), `tier`, `status`, the four datetime
    columns, `cancel_at_period_end`, `canceled_at`, `active`,
    `stripe_customer_id`, `stripe_subscription_id` (UNIQUE),
    `stripe_price_id`, `last_event_id`, `provider_snapshot` JSONB.
  - `\di+ ix_subscriptions_tenant_active` returns 1 row,
    `(tenant_id, active)`.
  - `\di+ ix_subscriptions_stripe_customer` returns 1 row,
    `(stripe_customer_id)`.

**Rollback at this step:** Alembic's PostgreSQL transactional DDL rolls
back automatically on any in-step failure. If the migration completes
but later steps fail and we need to revert, the downgrade target is
`3dbbc70d0105`:

```powershell
$DOWNGRADE = aws ecs run-task `
  --cluster luciel-cluster --task-definition luciel-backend:40 `
  --launch-type FARGATE `
  --network-configuration "awsvpcConfiguration={...}" `
  --overrides '{\"containerOverrides\":[{\"name\":\"luciel-backend\",\"command\":[\"alembic\",\"downgrade\",\"3dbbc70d0105\"]}]}' `
  --region ca-central-1
```

Downgrade is safe because `subscriptions` has no rows yet (no new code
has served traffic between migration and task-def update).

## Step 3 — Build and roll the new application image

### Step 3.0 — Build the image locally

**Platform pin.** Confirm `runtimePlatform.cpuArchitecture` on
`td-backend-rev39.json` matches your local build target. Use `--platform
linux/amd64` (or `linux/arm64`, whichever matches the registered
task-def) on every `docker build`.

```powershell
cd C:\Users\aryan\Projects\Business\Luciel
$GIT_SHA = git rev-parse --short HEAD
$IMG_TAG = "step-30a-and-31-2-$GIT_SHA"

docker build --platform linux/amd64 `
  -t luciel-backend:$IMG_TAG `
  -f Dockerfile .
```

### Step 3.1 — Push to ECR

```powershell
aws ecr get-login-password --region ca-central-1 | `
  docker login --username AWS --password-stdin `
  729005488042.dkr.ecr.ca-central-1.amazonaws.com

docker tag luciel-backend:$IMG_TAG `
  729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend:$IMG_TAG

docker push `
  729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend:$IMG_TAG

$NEW_DIGEST = aws ecr describe-images `
  --repository-name luciel-backend `
  --image-ids imageTag=$IMG_TAG `
  --region ca-central-1 `
  --query 'imageDetails[0].imageDigest' --output text
Write-Host "New ECR digest: $NEW_DIGEST"
```

### Step 3.2 — Render task-def revision 40 (backend)

Copy `td-backend-rev39.json` to `td-backend-rev40.json`, edit:

- `containerDefinitions[0].image`: replace digest with `$NEW_DIGEST`
- Remove any auto-populated fields ECS adds on registration
  (`taskDefinitionArn`, `revision`, `status`, `requiresAttributes`,
  `compatibilities`, `registeredAt`, `registeredBy`)

Verify no env-var deltas between `:39` and `:40` other than the image
digest. Step 30a + 31.2 do not require new env vars; the Stripe keys
and SES sender used by Step 30a were already wired in the
`:39`-deployed `step-30a-foundations` deploy on staging — confirm by
diffing the two JSON files and expecting only the `image` line to
differ.

### Step 3.3 — Render task-def revision 20 (worker)

Same operation against `td-worker-rev19.json` → `td-worker-rev20.json`.
Worker also picks up the Step 31.2 middleware (it imports the same FastAPI
app) but the middleware is a no-op for the worker's job-processing
entry points; this is for image-uniformity, not because the worker
exercises the new auth surface.

### Step 3.4 — Register both task defs

```powershell
aws ecs register-task-definition `
  --cli-input-json file://td-backend-rev40.json --region ca-central-1
aws ecs register-task-definition `
  --cli-input-json file://td-worker-rev20.json --region ca-central-1
```

**Now jump back to Step 2 and run the migration against `:40`. Then
return here for Step 3.5.**

### Step 3.5 — Update the services

```powershell
aws ecs update-service `
  --cluster luciel-cluster --service luciel-backend-service `
  --task-definition luciel-backend:40 `
  --region ca-central-1

aws ecs update-service `
  --cluster luciel-cluster --service luciel-worker-service `
  --task-definition luciel-worker:20 `
  --region ca-central-1

aws ecs wait services-stable `
  --cluster luciel-cluster `
  --services luciel-backend-service luciel-worker-service `
  --region ca-central-1
```

`services-stable` blocks until rollout completes and circuit-breaker
threshold is not tripped. Expect ~3–4 minutes.

## Step 4 — Observed-clean verification

Five-pillar shape: log review, idempotency probe, Step 30a-specific
checks, Step 31.2-specific checks, customer-path smoke.

### Step 4.1 — CloudWatch log review (clean startup)

Logs Insights query against `/ecs/luciel-backend` and
`/ecs/luciel-worker`, last 15 minutes:

```
fields @timestamp, @message
| filter @message like /ERROR/ or @message like /Traceback/ or @message like /CRITICAL/
| sort @timestamp desc
| limit 50
```

**Expected:** zero matches. A clean rollout produces only `INFO` lines.

### Step 4.2 — Step 30a-specific checks

```
fields @timestamp, @message
| filter @message like /\[magic-link-email\]/
| sort @timestamp desc
| limit 10
```

After the first signup-driven webhook arrives, expect at least one
`[magic-link-email] from=... to=... subject=... url=... ...` line.

```
fields @timestamp, @message
| filter @message like /SignatureVerificationError/
| sort @timestamp desc
```

**Expected:** zero matches. A non-zero count means Stripe's webhook
secret is misconfigured and `BillingWebhookService` is rejecting every
event — fix `STRIPE_WEBHOOK_SECRET` SSM parameter and re-roll.

Idempotency probe — from a bastion `psql`:

```sql
SELECT stripe_subscription_id, last_event_id, status
  FROM subscriptions
  ORDER BY created_at DESC
  LIMIT 5;
```

For each row, every redelivered Stripe event with the same `event.id`
should be rejected at `BillingWebhookService` line 357/421/512
(`if sub.last_event_id == event_id: return`). Hand-trigger from Stripe
Dashboard "Send test event" and confirm log shows
`event already applied, skipping`.

### Step 4.3 — Step 31.2-specific checks

Mount confirmation in the startup log line:

```
fields @timestamp, @message
| filter @message like /SessionCookieAuthMiddleware/
| sort @timestamp desc
| limit 5
```

**Expected:** one line per container start confirming the middleware
is mounted on `app`.

Cookied curl against `/api/v1/dashboard/tenant` from a session cookie
captured by clicking a real magic link:

```bash
# After clicking the magic-link email and inspecting Set-Cookie:
curl -i \
  --cookie "luciel_session=<value-from-Set-Cookie>" \
  https://api.<prod-host>/api/v1/dashboard/tenant
# expect: HTTP/2 200 with tenant payload
```

A 401 here means `SessionCookieAuthMiddleware` is not bridging the
cookie to the admin auth principal — block the deploy as a regression.

Run the live e2e harness against prod (one-shot, read-only path until
the embed-key mint step which uses an idempotent throwaway):

```powershell
cd C:\Users\aryan\Projects\Business\Luciel
$env:LUCIEL_E2E_BASE_URL = "https://api.<prod-host>"
$env:LUCIEL_E2E_TEST_EMAIL = "aryan+e2e-30a-31-2@<your-domain>"
python -m tests.e2e.step_31_2_live_e2e
# expect: all assertions pass, exit code 0
```

### Step 4.4 — Customer-path smoke (Sarah's journey)

End-to-end manual: hit `https://<prod-marketing-host>/signup`, complete
Stripe Checkout in test mode with a 14-day trial card, follow the
magic-link email to `/dashboard`, create a Luciel in the Luciels tab,
test-chat in instance detail, mint a pinned embed key in the Deploy
tab, paste the snippet on a throwaway test page, send a message
through the widget. End state:

- Dashboard Overview shows the new tenant.
- `subscriptions` row exists with `active=true`, `status=trialing`,
  the trial expiry date set, the Stripe customer + subscription IDs
  populated.
- A pinned `api_keys` row exists with `luciel_instance_id` set, not
  null (this is the Step 31.2 carve-out being enforced).
- Widget chat returns a real response and records a `conversations`
  row + audit log entries.

If any of these fail, rollback per the Rollback section.

## Step 5 — Doc-truthing close

After observed-clean verification on prod, add closing stanzas. The
three-document doctrine requires updates in all three files; commit
them in one doc-truthing pass under a closing tag.

### CANONICAL_RECAP.md §12 — close stanzas

For Step 24.5c row (only if Step 1b observed `a7c1f4e92b85` and the
chain walk landed both 24.5c and 30a together — otherwise skip; Step
24.5c was closed by its own runbook):

> **2026-05-13 close:** Migration `3dbbc70d0105` landed via the chain
> walk during the Step 30a+31.2 deploy. Prod RDS now at
> `b8e74a3c1d52`. No customer-facing impact in the 24.5c stanza of
> the chain.

For Step 31 row:

> **2026-05-13 close:** Code path active in prod under image
> `step-30a-and-31-2-<sha>`; dashboard endpoints serving 200s.

For Step 30a row:

> **2026-05-13 close:** Migration `b8e74a3c1d52` landed
> (`subscriptions` table + indexes). Code path active under image
> `step-30a-and-31-2-<sha>`. First signup-driven webhook observed
> at <timestamp>; `[magic-link-email]` log marker firing.

For Step 31.2 row:

> **2026-05-13 close:** Code-only deploy (no migration). Cookie bridge
> mounted; instance-pinned embed keys minting correctly via Step 32
> Deploy tab. Sarah's customer journey observed end-to-end on prod.

### DRIFTS.md — close stanzas

`D-step-24-5c-and-31-schema-and-code-undeployed-to-prod-2026-05-12`:

> **2026-05-13 resolved:** Prod RDS at `b8e74a3c1d52`; all three steps
> (24.5c, 31, 30a) code and schema deployed. Drift closed.

`D-step-30a-billing-shape-test-moderation-config-failure-2026-05-13`:
keep open — this is a dev-config drift unaffected by the prod deploy.

`D-admin-audit-logs-actor-user-id-fk-missing-2026-05-13`: keep open —
schema-correctness work for Step 32a.

`D-magic-link-auth-cookie-session-2026-05-13`: keep open with note —
cookie bridge is the partial advance; password/SSO/MFA work is
re-targeted to Step 32a.

### ARCHITECTURE.md

§3.2.2 and §3.2.13 already updated by the doc-truthing pass on
`ab0d3c6` — no further edits needed post-deploy. §3.2.8 and §4.6
triangulation refs already point at Step 32a for the rotation runbook.

### Commit and tag

```powershell
git add docs/CANONICAL_RECAP.md docs/DRIFTS.md
git commit -m "step 30a + 31.2 prod-deploy close — RECAP rows + drift resolution"
git tag -a step-30a-and-31-2-prod-deployed -m "Prod deploy of Step 30a (subscriptions migration + billing code) + Step 31.2 (cookie bridge + instance carve-out)"
GIT_CONFIG_GLOBAL=/home/user/.gitconfig-proxy git push origin main
GIT_CONFIG_GLOBAL=/home/user/.gitconfig-proxy git push origin step-30a-and-31-2-prod-deployed
```

---

## Rollback

**If Step 2 fails (migration):** Alembic transactional DDL rolls back
automatically. Re-investigate before reattempting.

**If Step 3.5 services-wait fails or circuit breaker trips:** ECS
automatically reverts to the prior task-def revision. Confirm with:

```powershell
aws ecs describe-services --cluster luciel-cluster `
  --services luciel-backend-service luciel-worker-service `
  --region ca-central-1 `
  --query 'services[].{name:serviceName,taskDef:taskDefinition}'
# expect: back to :39 / :19
```

Schema state is forward (`b8e74a3c1d52`) but old code path doesn't
reference `subscriptions`, so service operates normally on the old
image.

**If Step 4 verification fails after a stable rollout:** roll the
services back manually:

```powershell
aws ecs update-service --cluster luciel-cluster `
  --service luciel-backend-service `
  --task-definition luciel-backend:39 --region ca-central-1
aws ecs update-service --cluster luciel-cluster `
  --service luciel-worker-service `
  --task-definition luciel-worker:19 --region ca-central-1
```

If you have already begun accepting real subscription webhooks under
`:40` and need to roll schema back, downgrade Alembic to
`3dbbc70d0105` (see Step 2 rollback block) — but **only after**
manually backing up the `subscriptions` table contents if any rows
were written. The downgrade DROPs the table.

---

## Cross-references

- ARCHITECTURE.md §3.2.2 (instance pinning, Step 31.2 addendum)
- ARCHITECTURE.md §3.2.13 (cookie bridge, Step 31.2 addendum)
- ARCHITECTURE.md §3.2.8 + §4.6 (rotation runbook → Step 32a slot)
- CANONICAL_RECAP.md §12 (Step 30a + Step 31 + Step 31.2 rows)
- DRIFTS.md §3 (`D-step-24-5c-and-31-schema-and-code-undeployed-to-prod-2026-05-12`)
- Prior runbook: `docs/runbooks/step-24-5c-and-31-prod-deploy.md`
- Prior runbook: `docs/runbooks/operator-patterns.md` (Hazard 2, Hazard 5)
- Closing tags: `step-31-2-cookie-bridge-and-instance-embed-keys-complete` (commit `ab0d3c6`), `step-32-admin-dashboard-ui-complete` on Luciel-Website (commit `4f2d64c`)
- PRs: Luciel#48, Luciel-Website#2
