# Step 28 — Master Plan (Hardening Sprint)

**Created:** 2026-04-27, 2:15 PM EDT
**Branch:** `step-28-hardening`
**Built on:** `adc5ba0` (`step-24.5b-20260503`)
**First commit on branch:** `81c0088` (D1 closure)
**Author:** Aryan Singh, VantageMind AI
**Status:** Living document — updates as discovery happens

---

## 1. Step 28 in One Paragraph

Step 28 is the hardening sprint that converts Luciel from "shipped and
working" to "defensibly shipped and working." Steps 26b through 24.5b
got the product to live prod with a 14/14 verification gate, a durable
identity layer, and an async memory worker. Step 28 closes the
deferred drift items those arcs accumulated, brings the operational
posture up to what we will tell GTA brokerage prospects during tech
due diligence, and establishes the observability baseline that lets
us detect issues before customers do. Step 28 is done when the
defensible-security claim is true at every layer (auth, scope, audit,
DB role separation, network), the observability claim is true
(alarms + auto-scaling + healthchecks), the operational hygiene claim
is true (clean repo, clean prod state, no embarrassing artifacts), and
all 8 deferred drift items + 7 standing items + 5 cosmetic items from
the Step 24.5b canonical recap are either resolved or explicitly
deferred-with-reason. Estimated 13-15 commits across 4 phases over
2-3 weeks, with prod rollout at each phase close and GTA outreach
kicking off after Phase 1 prod ships.

---

## 2. The Four Phases

### Phase 1 — Security & Compliance Hardening

**Purpose:** Close every known security/compliance gap before any
external prospect conversation. After Phase 1 prod ships, the
"defensible PIPEDA posture" claim is true at the auth, scope, audit,
DB role, and network layers.

**Items:**
- D1 (DONE, commit `81c0088`) — leaked local platform-admin key rotated
- Consent route double-prefix bug (`/api/v1/api/v1/consent/*`)
- D11 — `memory_items.actor_user_id` orphan sweep + NOT NULL flip
- Separate `luciel_worker` Postgres role (least-privilege at DB layer,
  Step 27b security contract gap)
- Dedicated `luciel-worker-sg` security group (egress-only to
  RDS/Redis/SSM/SQS/LLM endpoints)

**Commits:** 5 (D1 done, 4 remaining)
**Wall-clock:** 6-8 hrs across 2-3 sessions
**Gate to Phase 2:** Phase 1 PR merged to main, prod rollout green,
tagged `step-28-phase-1-YYYYMMDD`, GTA outreach kickoff initiated
**Business value at close:** Multi-tenant security story is end-to-end
defensible; no known compliance gaps; outreach unblocked.

### Phase 2 — Observability & Reliability

**Purpose:** Establish the detect-before-customer-notices baseline.
Phase 2 ships in parallel with GTA outreach (engineering and sales
tracks decoupled).

**Items:**
- CloudWatch alarms (queue-depth >50, DLQ depth >0, RDS connection
  count >80%, ECS CPU >80%, ALB 5xx rate >1%) + SNS email pipeline
- ECS auto-scaling target-tracking (web on CPU, worker on SQS
  ApproximateNumberOfMessages)
- Container-level healthCheck on web task-def (`curl -f
  http://localhost:8000/health || exit 1`) — defense-in-depth
  independent of ALB target group health
- Container-level healthCheck on worker task-def (`celery inspect
  ping`)
- Batched retention deletes (Step 28 standing item from canonical
  recap §13 item 10) — current single-statement DELETE doesn't scale
  for large tables

**Commits:** 4
**Wall-clock:** 4-6 hrs
**Gate to Phase 3:** Phase 2 PR merged, prod rollout green, tagged
`step-28-phase-2-YYYYMMDD`, alarms verified by deliberately
triggering one (DLQ poison-pill test or CPU stress)
**Business value at close:** Operational maturity story is real, not
aspirational. Foundation for "we run reliably" claim.

### Phase 3 — Operational Hygiene

**Purpose:** Clean the repo, clean prod state, eliminate embarrassing
artifacts that would surface during a prospect's screen-share demo.

**Items:**
- Test-residue tenant sweep on prod (`step27-prodgate-*` tenants
  5329, 4085, 4991, 8756, 7676, 6060 plus `step27-syncverify-7064`)
- Runbook-artifact JSON gitignore cleanup (`*-task-def-*.json`,
  `step-*-snapshot-id.txt` patterns added explicitly)
- Pillar 15 in `app.verification` — consent route regression guard
  (asserts `/api/v1/consent/grant` returns expected, double-prefix
  returns 404)
- AgentConfig legacy model removal (Step 24.5 audit window closed
  per canonical recap §13 item 14)
- SSM param naming standardization to `/luciel/production/<NAME>`
  pattern (currently inconsistent: kebab `database-url` vs slash
  `REDIS_URL` per canonical recap §13 item 17)
- Step 26 JSON archive write-path bug (`step26_report.json` doesn't
  reliably overwrite per canonical recap §13 item 16)
- `DELETE /admin/tenants/<id>` endpoint (currently soft-deactivate
  via PATCH only; Step 28 standing item)

**Commits:** 4
**Wall-clock:** 3-5 hrs
**Gate to Phase 4:** Phase 3 PR merged, prod rollout green, tagged
`step-28-phase-3-YYYYMMDD`, prod tenant list shows only legitimate
tenants
**Business value at close:** Demo-clean. Nothing in the repo or prod
state would prompt "what's that?" from a prospect.

### Phase 4 — Cosmetic & Deferred Closure

**Purpose:** Close the long-tail items so the Step 24.5b drift table
ends Step 28 with zero open rows.

**Items:**
- D3 (cosmetic per 24.5b drift table)
- D4 (cosmetic per 24.5b drift table)
- Any items added to drift register during Phases 1-3 that were
  triaged as cosmetic
- Final retrospective recap commit — what we planned vs what we
  shipped

**Commits:** 2
**Wall-clock:** 2 hrs
**Gate to Step 28 close:** Phase 4 PR merged, drift register has zero
open P1+ items, master plan updated with retrospective, tagged
`step-28-YYYYMMDD` on the final main commit
**Business value at close:** Step 28 fully closed, drift discipline
demonstrated end-to-end.

---

## 3. Live Drift Register

This register is the single source of truth for what's open in Step 28.
Every commit that closes an item updates the status here in the same
commit. Every discovery during execution gets appended with date,
source, and triage disposition.

**Status legend:**
- `OPEN` — not yet started
- `IN_PROGRESS` — assigned to current commit
- `RESOLVED` — closed, with closing commit SHA referenced
- `DEFERRED` — explicitly punted to a later step with reason
- `COSMETIC` — non-blocking, scheduled for Phase 4

**Priority legend:**
- `P0` — security/compliance gate; blocks GTA outreach
- `P1` — operational quality; would surface in tech due diligence
- `P2` — hygiene; would surface in screen-share demo
- `P3` — cosmetic; long-tail closure

### Seeded from Step 24.5b drift table (8 deferred items)

Verbatim titles from `docs/recaps/2026-04-27-post-step-24-5b-canonical.md`
Section 3 (lines 204-222). D10 reclassified from "deferred" to
RESOLVED-as-convention (it was a template style change adopted in 24.5b
Commit 3, not a Step 28 item). D16 was the same item I'd separately
listed as "consent double-prefix" standing item — deduped in this table.

| ID  | Priority | Phase | Status | Title | Resolution path |
|-----|----------|-------|--------|-------|-----------------|
| **D1**  | P0 | 1 | RESOLVED (`81c0088`) | Local platform-admin key `luc_sk_HY_RK` exposed in chat 2026-04-26 | Rotated 2026-04-27, audit row 1997, key id=539 replacement |
| **D2**  | P2 | 3 | OPEN | `admin_audit_log.py` has duplicate `ACTION_KNOWLEDGE_*` declarations and `RESOURCE_KNOWLEDGE` value override (`"knowledge_embedding"` → `"knowledge"` silently) | One-line constants dedup commit; verify `ALLOWED_ACTIONS`/`ALLOWED_RESOURCE_TYPES` unchanged externally |
| **D5**  | P1 | 1 | OPEN | `ApiKeyService.deactivate_key` doesn't accept `audit_ctx` or emit audit rows | Retrofit `deactivate_key(audit_ctx=...)` as canonical audit-emitting path. D1 closure used inline workaround; future rotations use the service layer |
| **D10** | —  | — | RESOLVED (24.5b convention) | Full-file Alembic migration template had commented-out revision-identifier lines | Convention adopted in 24.5b Commit 3: future migrations are diff-only, never full-file. Removed from Step 28 scope |
| **D11** | P1 | 1 | OPEN | `memory_items.actor_user_id` has 10 historical orphan NULL rows blocking NOT NULL flip | Phase 1 commit: orphan sweep via existing backfill flow, then NOT NULL flip migration |
| **D14** | P2 | 3 | OPEN | Synthetic emails (`.luciel.local`) in DB from backfill would 422 if PATCHed through public API (`UserUpdate` → `EmailStr`) | Latent; rare. Step 28 cleanup either widens schema validator or routes synthetic-email PATCHes through internal-only service path |
| **D16** | P1 | 1 | OPEN | `app/api/v1/consent.py` declares `prefix="/api/v1/consent"` (full absolute path); when parent router mounts under `/api/v1`, prefix doubles into `/api/v1/api/v1/consent/...` | Phase 1 consent commit: change to relative `prefix="/consent"`, add Pillar 15 regression guard in Phase 3 |
| **D18** | P2 | 3 | OPEN | Local LLM-based memory extractor produces 0 `memory_items` rows for some message shapes (verification suite chat patterns) | Extractor improvements; not security/compliance. Step 28 quality work |
### Standing items from prior step closures (7 items)

| Item | Priority | Phase | Status | Origin |
|------|----------|-------|--------|--------|
| Separate `luciel_worker` Postgres role (least-privilege at DB layer) | P1 | 1 | OPEN | Step 27b security contract gap, canonical recap §13 item 8 |
| Dedicated `luciel-worker-sg` security group | P1 | 1 | OPEN | Step 27b deferred, canonical recap §13 item 9 |
| CloudWatch alarms (queue/DLQ/RDS/ECS/ALB) + SNS email | P1 | 2 | OPEN | Step 27b deferred, canonical recap §13 item 6 |
| ECS auto-scaling target-tracking | P1 | 2 | OPEN | Step 27b deferred, canonical recap §13 item 7 |
| Container-level healthCheck on web + worker task-defs | P1 | 2 | OPEN | Canonical recap §13 item 19 |
| Batched retention deletes | P1 | 2 | OPEN | Canonical recap §13 item 10 |

### Hygiene items (P2/P3)

| Item | Priority | Phase | Status | Origin |
|------|----------|-------|--------|--------|
| Test-residue tenant sweep on prod (`step27-prodgate-*`, `step27-syncverify-7064`) | P2 | 3 | OPEN | Step 27 closure backlog, canonical recap §13 item 2 |
| Runbook-artifact JSON gitignore cleanup | P2 | 3 | OPEN | Canonical recap §13 item 3 |
| Pillar 15 — consent route regression guard | P2 | 3 | OPEN | Phase 1 consent fix follow-up |
| AgentConfig legacy model removal | P2 | 3 | OPEN | Step 24.5 audit window, canonical recap §13 item 14 |
| SSM param naming standardization | P3 | 3 | OPEN | Canonical recap §13 item 17 |
| Step 26 JSON archive write-path bug | P3 | 3 | OPEN | Canonical recap §13 item 16 |
| `DELETE /admin/tenants/<id>` endpoint | P2 | 3 | OPEN | Canonical recap §13 standing item |
| D3 (cosmetic per 24.5b drift table) | P3 | 4 | OPEN | 24.5b drift table |
| D4 (cosmetic per 24.5b drift table) | P3 | 4 | OPEN | 24.5b drift table |

### Discovered during Step 28 execution (grows over time)

| Date | Item | Discovered during | Priority | Triage | Status |
|------|------|-------------------|----------|--------|--------|
| 2026-04-27 | `AuditContext` import path was `app.repositories.admin_audit_repository`, not `app.middleware.audit_context` as I'd assumed from canonical recap §2.6 | D1 closure (commit `81c0088`) | P3 | Memory correction, not a bug. Note for future commits: the canonical recap's import paths are descriptive, not verbatim — always grep current code. | RESOLVED |
| 2026-04-27 | `AdminAuditRepository.record()` uses `autocommit=False` (no underscore), not `auto_commit=False` | D1 closure | P3 | Memory correction. Pattern locked: any multi-line Python from PowerShell uses here-string-to-tempfile. | RESOLVED |
| 2026-04-27 | PowerShell `python -c "...f-string..."` collides with PS parser when escaped quotes embed `in` keyword | D1 closure allowlist check | P3 | Pattern documented in this register. Use `@'...'@ \| Set-Content -Path _file.py` pattern for any non-trivial Python from PS. | RESOLVED |

---

## 4. Sequencing Rules / Non-negotiables

These are the rules that make Step 28 execute as planned rather than
drift. They are written down because every prior step learned at least
one of them the hard way.

### Branch & PR discipline

1. **One branch for the whole step.** `step-28-hardening` accumulates
   all phase commits. No per-phase branches.
2. **One PR per phase, not per commit.** Phase 1 PR opens after Phase 1's
   final commit, merges to main, gets the phase tag. Then Phase 2 work
   begins on the same `step-28-hardening` branch (rebased onto main).
3. **Each phase ends with a tag** in the form `step-28-phase-N-YYYYMMDD`.
4. **Step 28 closes with a final tag** `step-28-YYYYMMDD` on the merge
   commit of the Phase 4 PR.

### Commit discipline

5. **Every commit closes one drift item or one logical change.** No
   "while I'm in there" expansions. If a side-discovery looks worth
   doing, append to drift register first, schedule for a later commit.
6. **Every commit's message references the drift register.** Format:
   `type(28): <one-line summary> (closes <D-id> | standing-item-N)`.
7. **Last commit of each phase updates the drift register** in the same
   commit, marking items RESOLVED with the closing SHA.
8. **No code change without a drift register entry.** If something
   isn't tracked, write it down before touching code.

### Verification discipline

9. **Local 14/14 (or N/N as Pillars are added) green before any commit
   on the branch.** No "I'll fix verification later" commits.
10. **Each phase ends with a prod rollout** per the established runbook
    pattern (Phase 0-6 from 24.5b runbook). Each phase has its own
    runbook artifact in `docs/runbooks/step-28-phase-N-deploy.md`.
11. **Each phase has a rollback contract** documented in the runbook,
    with the previous phase's tag as the rollback ceiling.

### Discovery discipline

12. **Triage on detection, not later.** Anything unexpected gets a
    drift register row in the same shell session it was discovered in.
    Triage = close-now (this phase), defer (specific later phase),
    cosmetic (Phase 4), or P0 (stop-and-fix on a separate branch).
13. **P0 discoveries halt the current phase.** Single-commit fix on a
    separate branch off main, fast-forward merge, then resume the
    Step 28 branch rebased onto the new main.
14. **Phase scope is fixed at phase start.** Mid-phase additions only
    if the addition is a P0 or if the addition strictly closes a
    P0/P1 already in the register for that phase. Anything else is
    deferred to a later phase.

### Re-planning discipline

15. **The master plan is a living document.** Section 3 (drift
    register) updates with every commit. Sections 1, 2, 5, 6 update
    only at phase-close, in the phase-close commit, with the change
    visible in the commit diff.
16. **Re-planning is a deliberate act.** If discovery during a phase
    makes the original phase plan wrong, the response is: stop coding,
    write a brief "re-plan note" in the drift register row, update
    the master plan in a dedicated commit before resuming.

---

## 5. Step 28 Close Gate

Step 28 is done — and the `step-28-YYYYMMDD` tag goes on the final
main commit — when **all** of the following are true:

1. **All P0 items RESOLVED.** Currently: D1 (DONE). No new P0 items
   discovered without immediate closure.
2. **All P1 items RESOLVED or explicitly DEFERRED-with-reason** in the
   drift register. "Deferred-with-reason" means a row in the register
   with: target step, written justification, target date.
3. **All four phases tagged on origin** (`step-28-phase-1` through
   `step-28-phase-4`).
4. **Local verification green** at whatever pillar count is current
   (14/14 today; Pillar 15 added in Phase 3, so 15/15 by Step 28
   close).
5. **Prod verification green in MODE=full** at the same pillar count,
   run via ECS execute-command per the established runbook pattern.
6. **Drift register has zero open P1+ items.** P2/P3 cosmetic items
   may remain open if explicitly scheduled for a future step.
7. **Master plan retrospective committed.** Section 1 updated with
   what shipped vs what was planned, lessons learned, items moved to
   future steps.
8. **Tag `step-28-YYYYMMDD`** on the merge commit of the Phase 4 PR
   with annotated message capturing: phase tags referenced, drift
   items resolved (count + IDs), commits in step (count + first/last
   SHA), prod state at close (web task-def, worker task-def, Alembic
   head, RDS snapshot ID).

### What "done" looks like for the business

After Step 28 close, the canonical claims Luciel can make to a
brokerage prospect during tech due diligence are:

- "PIPEDA defensible at every layer" — auth, scope, audit, DB role
  separation, network egress (Phase 1)
- "We detect issues before customers do" — alarms, auto-scaling,
  health checks, batched cleanup (Phase 2)
- "Operational hygiene matches the engineering discipline" — clean
  repo, clean prod state, audit trail end-to-end (Phase 3)
- "Drift discipline is real" — every known item closed or
  scheduled-with-reason; nothing on a sticky note (Phase 4)

These are not aspirational. Each maps to a specific phase tag with a
specific commit list and a specific prod rollout. Tech due diligence
becomes "here's the tag, here's the runbook, here's the audit log" —
not "trust us."

---

## 6. Re-planning Trigger Conditions

When to update this plan vs follow it. Written down so we don't
debate it mid-execution.

### Triggers that REQUIRE re-planning

1. **Any P0 discovery during a phase.** Stop the phase. Single-commit
   fix on a separate branch off main. Fast-forward merge. Update the
   drift register. Then re-plan the current phase if the P0 changed
   what's possible.
2. **Discovery that adds >1 commit to a phase's scope.** Update Section
   2's commit count + wall-clock estimate for that phase. Update the
   phase's runbook artifact. Triage whether the addition pushes any
   currently-planned items to a later phase.
3. **Discovery that crosses phases.** If Phase 1 work surfaces a Phase
   3 item, append it to the register under Section 3's "Discovered
   during Step 28 execution" sub-table with the phase assignment. Do
   not silently expand the current phase.
4. **A phase's wall-clock estimate exceeds 2x the original.** Stop and
   re-plan. The estimate was wrong; figure out why before continuing.
5. **A drift item turns out to require a separate step.** Some items
   may surface as "this is bigger than Step 28 — it deserves its own
   step." Move it out of Step 28 with a written justification, update
   the canonical step list.

### Triggers that do NOT require re-planning

6. **Memory corrections** (e.g. import path drift, kwarg name drift —
   like the three RESOLVED items in Section 3's discovery sub-table).
   These are footnotes, not re-plans.
7. **Cosmetic discoveries** (P3) — append to register, schedule for
   Phase 4, keep going.
8. **Estimate misses within 2x** — note for retrospective, do not
   re-plan.

### Re-planning is a deliberate act

When re-planning is triggered:
- Stop coding.
- Write a brief "re-plan note" in the affected drift register row(s)
  capturing what changed and why.
- Update Sections 2, 5, 6 of the master plan as needed.
- Commit the master plan update as a dedicated commit:
  `docs(28): re-plan after <discovery> — <one-line impact>`.
- Resume execution.

This commit is the audit trail of "we adjusted course and here's why."
Future-you, a security reviewer, or a brokerage prospect's tech team
can walk the master plan's commit history and see every re-planning
moment.

---

## 7. Business Context

### Where Luciel is today (post-24.5b)

Luciel is live at `https://api.vantagemind.ai`, hosted on AWS ECS
Fargate in ca-central-1, with a 14/14 verification suite green on
prod. The architecture is settled (AgentLucielInstance split,
multi-tenant identity layer, async memory worker, scope-policy
enforcement at every layer). The five tagged releases on origin
(`step-26b-20260422`, `step-27a-20260422`, `step-27-20260425`,
`step-27c-deployed-20260425`, `step-27-completed-20260426`,
`step-24.5b-20260503`) trace the production-credibility arc from
"first prod release" to "durable user identity layer with Q6 cascade."

The product is functional. The remaining gap is operational maturity:
known security/compliance items deferred from prior steps, observability
that doesn't yet exist, and hygiene items that would surface during a
prospect screen-share. Step 28 closes that gap.

### Why Step 28 matters for the GTA outreach window

Marketing/outreach has been considered unblocked from the product
side since Step 26b shipped (April 22, 2026). What's been blocking
outreach in practice is the D1 leaked key (closed 2026-04-27 in
commit `81c0088`) and the residual sense that "we should fix N more
things before we pitch." Step 28 makes that sense concrete: Phase 1
closes the security/compliance items that would surface in tech due
diligence. After Phase 1 prod ships, the gating question — "is my
data safe with you?" — has a defensible answer at every layer.

GTA outreach kicks off after Phase 1 prod ships, in parallel with
Phase 2 work. The sales and engineering tracks decouple at that
point.

### How each phase translates to a prospect-conversation talking point

**Phase 1 — Security & Compliance:**
- "Your data lives in your tenant only. Cross-tenant access is blocked
  at the authorization layer, the worker payload layer, and the DB role
  layer. We have audit logs for every administrative action."
- "Our verification suite includes a cross-tenant identity-spoof test
  that runs on every release. It hasn't ever failed."
- "PIPEDA is non-negotiable. All data is in ca-central-1. Retention
  policies are tenant-scoped. Deletion is logged immutably."

**Phase 2 — Observability & Reliability:**
- "We get paged before you notice. Queue depth, DLQ, RDS connection
  count, CPU, ALB error rate — all alarmed."
- "We auto-scale on real metrics, not on schedules."
- "Health checks are independent of the load balancer."

**Phase 3 — Operational Hygiene:**
- "Every administrative action is in the audit log."
- "Every deployment is in a git tag with a runbook."
- "Every drift item we've ever discovered is in a register, with a
  status and a closing SHA."

**Phase 4 — Cosmetic & Deferred Closure:**
- (Internal milestone, not customer-facing.)

### First tenant target

REMAX Crossroads, Markham. First individual user: Sarah, senior
listings agent. The Step 28 hardening sprint is the last engineering
prerequisite before the first sales conversation.

### Pricing reminder (locked from canonical recap §2)

- Individual: $30-80/mo
- Team: $300-800/mo
- Company: $2k+/mo
- Bottom-up upgrade path with pro-rated credit (Step 38)

Stay in real estate until 35 paying tenants before expanding verticals.

---

## Section 10 — Phase 2 Status Snapshots

Appended over time. Each snapshot is a dated section; do NOT edit prior
snapshots in place — history matters. New snapshots go at the bottom of
this section.

### Snapshot 2026-05-03 (evening) — mid-Phase-2, post-incident reconciliation

**Branch:** `step-28-hardening-impl`, HEAD `43e2e7a` (about to be
bumped by the docs reconciliation commit this section is part of).

**Phase 2 commits landed so far (chronological):**

| SHA | Type | Status |
|---|---|---|
| `75f6015` | Commit 2 — audit-log API mount | Code-only, shipped |
| `bfa2591` | Commit 2b — audit-log review fixes | Code-only, shipped |
| `56bdab8` | Commit 3 — Pillar 13 A3 fix | Code-only, shipped |
| `0d75dfe` | Commit 8 — batched retention deletes | Code-only, shipped |
| `925c64a` | Commit 9 — Phase 2 close (recap v1.1 + runbook) | Code-only, shipped |
| `2c7d0fb` | HOTFIX — Pillars 7/17/19 fixes | Code-only, shipped |
| `31e2b16` | Phase 3 backlog v1 + sandbox e2e | Code-only, shipped |
| `2b5ff32` | Mint-script hardening (post-incident) | Code-only, shipped |
| `43e2e7a` | Mint incident recap + Phase 3 backlog v2 | Code-only, shipped |

**Phase 2 commits NOT yet landed (prod-touching):**

| # | Description | Status |
|---|---|---|
| 4 | Worker DB role swap to `luciel_worker` + `luciel_admin` rotation | **Blocked on P3-J + P3-K** (see Phase 3 backlog). Mint script is hardened and dry-run clean (`2b5ff32`). |
| 5 | 5 CloudWatch alarms + SNS pipeline | Pending (untouched) |
| 6 | ECS auto-scaling target tracking | Pending (untouched) |
| 7 | Container healthChecks | Pending (untouched) |

**What Phase 2 has revealed but not yet delivered (the discovery
surface):**

This section exists because the Commit 4 work surfaced gaps that
weren't on any list at Phase 2 start:

1. **Mint script DSN-leak class of bug.** Caught in dry-run before any
   prod mutation. Patched in `2b5ff32`. Documented in
   `docs/recaps/2026-05-03-mint-incident.md`. Pattern E ("plaintext
   credentials live in SSM only") was correctly stated in the
   canonical recap but was paper-only without redaction discipline
   on the leak surfaces.
2. **Original "second IAM gap" diagnosis was wrong.** Direct read of
   the migrate-role policy showed `ssm:GetParameter` and
   `ssm:PutParameter` are both present on `/luciel/production/*`.
   The genuine gap is `ssm:GetParameterHistory` only. Recap and
   backlog corrected inline 2026-05-03 evening.
3. **`luciel-admin` IAM user has no MFA.** P0 finding, bigger than
   the worker-DSN incident. Captured as P3-J in the Phase 3 backlog;
   must be the next operator action.
4. **No IAM-traceable boundary exists between human-initiated
   credential operations and routine admin activity.** Original
   architecture would either grant the migrate task role read access
   to the admin DSN (reproducing the leak conditions) or have
   `luciel-admin` directly read the DSN (no audit boundary).
   Resolution: Option 3 boundary captured as P3-K — dedicated
   `luciel-mint-operator-role` with MFA-required AssumeRole.

**Revised Phase 2 close gate (2026-05-03 evening):**

The original gate (canonical recap §4.1) was: `python -m
app.verification` 19/19 green, all 5 alarms `OK`, both auto-scaling
targets registered, both services healthCheck-enabled, zero worker
connections as `luciel_admin`. That gate stands; we add three
clauses:

6. **MFA enabled on `luciel-admin`** (P3-J) — verified by
   `aws iam list-mfa-devices --user-name luciel-admin` returning a
   non-empty `MFADevices` list.
7. **`luciel-mint-operator-role` exists with MFA-required trust
   policy** (P3-K) — verified by
   `aws iam get-role --role-name luciel-mint-operator-role` showing
   `aws:MultiFactorAuthPresent` condition on the AssumeRolePolicy.
8. **Admin password rotated, leak log stream deleted** (P3-H) —
   verified by
   `aws logs filter-log-events --log-group-name /ecs/luciel-backend --filter-pattern '"LucielDB2026Secure"'`
   returning zero events, AND the password fingerprint recorded in
   the mint incident recap §9 addendum matches the SSM-stored
   value's first 12 SHA256 hex chars.

**Revised Phase 2 wall-clock estimate:**

Original estimate: 3-4 hours total wall-clock for hands-on execution
of Commits 4-7. Revised: 5-7 hours, accounting for P3-J (~5 min),
P3-K (~45 min including helper script + smoke test), P3-G policy
diff (~15 min), P3-H rotation (~1 hour), plus the original 3-4 hours
for Commits 4-7. Spread across 2-3 sessions for context hygiene.

**Honest assessment of where Phase 2 is:**

5 of 9 originally-scoped commits land. 4 prod-touching commits
remain. The Commit 4 blocker has revealed itself as larger than
originally scoped, but the larger scope is the right scope — we now
have a defensible IAM architecture for credential operations rather
than a convenient one. The trade is well-spent.

The canonical recap was bumped v1.0 → v1.1 at Commit 9 (`925c64a`).
It is now at v1.2 (this commit and the canonical-recap edits in the
same docs reconciliation pass).

---

**End of master plan.**

Living document. Updates land as commits on `step-28-hardening`.
Section 3 (drift register) updates with every Step 28 commit.
Sections 1-2, 5-7 update at phase-close in dedicated `docs(28): re-plan`
commits.
Section 10 (Phase 2 Status Snapshots) appends a new dated snapshot at
any significant Phase 2 milestone or discovery.
