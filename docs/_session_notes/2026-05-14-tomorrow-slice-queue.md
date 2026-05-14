# Tomorrow's Slice — Queue Carried Forward From 2026-05-13 Session

**Context:** Step 30a.1 Tonight's slice landed cleanly at commit `31bcc7a73cbde516bccdf1498722974509f8770d` on `main`, tagged `step-30a-1-tiered-self-serve-complete` (annotated tag object `fc0a18254d3e0a8cb3a8e4355174e23d080769c2`). Prod state at sealing: Alembic `c2a1b9f30e15 (head)`, ECR digest `sha256:d240034dadc0afc2ebd7456522a98a1c852d2ed3baacb9e2b9d754c5668eda5d` at tag `step-30a-1`, ECS task-defs `luciel-backend:43` + `luciel-worker:22`, both services rolloutState=COMPLETED with single PRIMARY deployment, zero ERROR/Traceback/CRITICAL/Exception across `/ecs/luciel-backend` and `/ecs/luciel-worker` over 10m post-deploy window.

This note carries forward the items deliberately deferred to tomorrow's session. Operator delegation phrase invoked at the session close: **"you know our discipline and symmetry, hence, I will leave the judgement onto you"** — agent (partner) made the call to defer all of the below to tomorrow's fresh-head session rather than land anything else tonight after the closing tag was cut.

---

## A. The Stripe deferred slice (the named tomorrow's slice)

Tracked at drift `D-stripe-live-account-not-yet-activated-2026-05-13` in `docs/DRIFTS.md` §3. The full resolution path is in that drift stanza; this is the operator-facing summary.

**Gate (must complete first):**
- Stripe live-mode activation form. Canadian sole-prop on Aryan's personal identity (no business registered yet). Inputs: legal name, address (Markham, ON), SIN, business description, expected processing volume, banking details for payouts. Activation latency: minutes to hours depending on Stripe's review. Cannot start the rest of the slice until activation completes.

**Then, in order:**
1. **Step 1c** — runbook prerequisite check that's deferred from tonight's Step 1a/1b
2. **Phase 1 — Stripe Prices** — create 6 Prices in live mode (the tier-cadence fanout: solo/team/business × monthly/annual)
3. **Phase 2 — SSM puts** — 6 writes to `/luciel/prod/stripe/price_id/*` parameter-store paths with the new live price IDs
4. **Force service redeploy** — `aws ecs update-service --force-new-deployment` on both `luciel-backend-service` and `luciel-worker-service` to pick up the new SSM values at task-boot
5. **Phase 5 — CloudFront invalidation** — manual `/*` invalidation per the runbook (the distribution serves the marketing site that surfaces tier pricing)
6. **Phase 6 — 3 smoke flows** — end-to-end checkout against the live Stripe account for one tier/cadence per primitive (one solo-monthly, one team-annual, one business-monthly, or whichever 3 cover the matrix)

**Closing ritual when slice lands:**
- Re-cut `step-30a-1-tiered-self-serve-complete` forward onto the closing commit (tag-discipline: "code + docs + prod all agree here")
- Close `D-stripe-live-account-not-yet-activated-2026-05-13` per drift-closure ritual (move to closed-drifts section or mark CLOSED inline with closing commit SHA + date)
- Flip CANONICAL_RECAP §12 Step 30a row 🔧→✅ with new closure stanza (preserve the tonight's 🔧 row per Pattern E)
- Update CANONICAL_RECAP §12 Step 30a.1 row to record Tomorrow's-slice closure
- Update CANONICAL_RECAP §14 to reflect new prod state (Stripe live, 6 Prices, 6 SSM keys populated)

---

## B. Side-observations queue (tonight's session, doc-truthing only — no prod touch)

These are observations from tonight's deploy that should land as a separate doc-truthing commit (or fold into the Stripe-closure commit if scope stays clean). All five are renderer-or-process bugs in our runbook/recap, not system drifts.

1. **Worker revision off-by-one.** Runbook `docs/runbooks/step-30a-1-prod-deploy.md` Phase 4 said worker would roll `:20→:21`. Actual was `:21→:22` because operator registered worker `:21` out-of-band on 2026-05-13 17:22 EDT (lockstep image roll, same `8fbc267d` digest as backend `:42`). **Fix:** update runbook Phase 4 + CANONICAL_RECAP §12 Step 30a.1 row to record the actual revision pair `:43`/`:22`, and note the out-of-band `:21` registration in the recap timeline.

2. **Runbook network config bug.** Runbook Step 1b and Phase 3 one-shot Fargate commands specified `assignPublicIp=DISABLED`. First Step 1b attempt (task ARN `b0f1048506c445228180c0e670e9e049`) failed with `ResourceInitializationError: unable to pull secrets or registry auth ... dial tcp 99.79.34.215:443: i/o timeout`. Root cause: the two ECS subnets (`subnet-0e54df62d1a4463bc`, `subnet-0e95d953fd553cbd1`) route 0.0.0.0/0 via IGW `igw-028d864c65be623b6` (no NAT), and the VPC has only SSM-family VPC endpoints — no ECR or Logs endpoints. Fargate tasks need public IPs to reach ECR. **Fix:** runbook Step 1b + Phase 3 commands must say `assignPublicIp=ENABLED`. Add an explainer paragraph: "VPC has IGW route, no NAT, no ECR/Logs endpoints — one-shot tasks need public IPs."

3. **PowerShell BOM trap.** On Windows PowerShell 5.1, `Set-Content -Encoding utf8` writes UTF-8 **with BOM** (`EF BB BF`). AWS CLI `register-task-definition --cli-input-json` rejects BOM-prefixed JSON with `Invalid JSON received`. **Fix:** runbook needs a no-BOM warning block for any `--cli-input-json` flow. Recommended pattern: `[System.IO.File]::WriteAllText($path, $json, (New-Object System.Text.UTF8Encoding $false))`. Add to a "PowerShell Operator Notes" section at the top of the runbook so it applies to all phases, not just Phase 4.

4. **Fargate cold-start window.** One-shot Fargate task with image pull from ECR takes ~90-95 seconds from `run-task` to task RUNNING. Operator pacing matters when waiting for `describe-tasks` to show terminal state. **Fix:** add a "Cold-start expectations" note to runbook Phase 3 (and any future one-shot Fargate phases): "Expect ~90-95s from run-task to RUNNING. Use `aws ecs wait tasks-running` with `--cli-read-timeout 180` to avoid premature timeouts."

5. **Gitignore hygiene on task-def fetch/register files.** Tonight's session created `td-backend-rev42-fetched.json`, `td-backend-rev43.json`, `td-worker-rev21-fetched.json`, `td-worker-rev22.json` in repo root on operator's laptop. These are ephemeral artifacts of the Phase 4 register-task-definition workflow — should not be committed. **Fix:** check `.gitignore` currently has `td-*.json` or equivalent. If not, add it. Verify by `git status` against operator's laptop on tomorrow's session open.

---

## C. CANONICAL_RECAP §12 table overflow — format migration

**Observation:** §12 Status Table (4-column: Theme / Step / Title / Notes) clips long Notes cells at the right margin when rendered on GitHub web. Visible on Step 24.5c row in the screenshot the operator pasted at session close; will affect every row with multi-paragraph closure notes (30a, 30a.1, 31, vantagemind rows, every drift-cited row). Not a content bug — content is correct. Format bug — markdown tables can't wrap intelligently.

**Why this is not a drift:** drifts are gaps between CANONICAL_RECAP's claim, ARCHITECTURE's design, and reality. This is a rendering bug in CANONICAL_RECAP itself, not a state delta. Belongs in the side-observations queue, not DRIFTS §3.

**Proposed fix:** migrate §12 from 4-column table to heading-per-step format. Each step gets:

```markdown
### Step <id> — <title>

**Theme:** <theme>
**Status:** <emoji> <closure-or-status-summary>
**Notes:**

<multi-paragraph prose with code spans, links, citations as needed>

**Architecture:** ARCHITECTURE §<section>
**Closing tag:** `<tag-name>` (if applicable)
**Drift:** DRIFTS §3 `<drift-id>` (if applicable)
```

**Why heading-per-step:** markdown headings get word-wrap by default; tables don't. The cell-prose pattern has outgrown the table primitive. Tables are for grids of short values; we have prose+code+citations per row now, which is heading territory. This is what every long-form ledger doctrine converges on once cells stop being one-liners.

**Touches every row in §12.** This is a substantive doc-truthing commit. Should be reviewed with fresh eyes, not rushed. Recommended commit-pairing: land in the same commit as Tomorrow's-slice closure so §12 lands in its new shape with new content already correctly formatted (avoids re-flowing twice).

**Open design questions to think about before starting:**
- Does §14 (the prod-state snapshot) also want the same format treatment, or does its tabular nature stay appropriate?
- Should the **Drift** line cite multiple drifts inline when applicable, or list each on its own line?
- Should **Status** be a single line or expand to status + date + closing-commit-sha when applicable?
- Should we preserve the existing table format at the top of §12 as a "TL;DR / index" with one-line summaries, with the heading-per-step format expanded below? Or full migration with no table remnant?

These are not blocking questions — they're "decide once at the start of the migration" questions. Worth a 5-minute think before opening the editor.

---

## D. Update CANONICAL_RECAP §14 to reflect new prod state (not yet committed)

**State to record:**
- RDS Alembic head: `c2a1b9f30e15` (was `b8e74a3c1d52` before tonight)
- ECR latest backend image: digest `sha256:d240034dadc0afc2ebd7456522a98a1c852d2ed3baacb9e2b9d754c5668eda5d` at tag `step-30a-1`
- ECS task-defs in service: `luciel-backend:43`, `luciel-worker:22`
- Tag at commit `31bcc7a`: `step-30a-1-tiered-self-serve-complete`
- Customer surface state: `/billing/checkout` still returns 501 (unchanged from before tonight, because no live Stripe Prices yet — that's the deferred slice)

**Why not committed tonight:** operator-fatigue surface. Same reasoning as the §12 format migration. Land in tomorrow's doc-truthing commit.

---

## E. Operator state at session close

- Aryan on laptop `C:\Users\aryan\Projects\Business\Luciel`, on `main` at `31bcc7a` with `M .gitignore` benign local mod
- Python venv `.venv` active
- Docker Desktop 29.2.1 working
- AWS CLI working, profile `default`, AWS account `729005488042`, region `ca-central-1`
- Stripe CLI v1.40.9 installed, not authenticated (no live account yet)
- Local task-def JSON artifacts present per side-observation #5

**Tag verification step still pending from operator:**
```powershell
git fetch origin --tags
git tag --verify step-30a-1-tiered-self-serve-complete  # optional
git show step-30a-1-tiered-self-serve-complete --stat --no-patch
```
Operator may have run this between the agent's last full message and the session-close exchange about the screenshot. If output not yet pasted at tomorrow's session open, prompt for it as the first action — we want explicit confirmation that the tag fetched cleanly and resolves through to commit `31bcc7a` with the doc-truthing diffstat. If it did not run, run it as the first action tomorrow.

---

## Carry-forward summary

When tomorrow's session opens, agent's first three moves should be (in order):

1. Read this note in full.
2. Confirm operator's tag-verify output if not already in the chat. If not yet run, prompt for it.
3. Propose the Stripe activation form pre-fill scaffold OR direct activation flow, depending on operator energy and Stripe form readiness.

The Stripe slice is the named primary work for tomorrow. Side-observations queue (B) + §12 migration (C) + §14 update (D) are all doc-truthing surface that should land as part of (or alongside) the Stripe-closure commit, not as separate noise commits.
