# Luciel Canonical Recap

**Version:** v2.1
**Last updated:** 2026-05-05 ~20:03 EDT (Step 28 Phase 2 verification gate CLOSED — prod `python -m app.verification` returned **19/19 GREEN** end-to-end against `https://api.vantagemind.ai`, closing the last unchecked exit-criteria box from v2.0). Net result for v2.1: six closure commits landed on `step-28-hardening-impl` between 5f83a55..bf4539f — (9) `dddf8cb` mounts the audit-log router so P19 returns the audit page instead of 404; (10) `fd26080` fixes the verify harness response parser for `GET /admin/verification/teardown-integrity` and migrates P12/P13/P14 setup binds to the new platform-admin-gated `bind-user` route; (11) `0cde3e7` makes UUID JSON-serializable when written into `admin_audit_logs.context_payload` (JSONB column) by the agent repository — the prior `TypeError: Object of type UUID is not JSON serializable` was masking the real `permission denied for table scope_assignments` failure beneath; (12) `99dfdc0` ships five new platform_admin-gated admin routes (`POST /admin/scope-assignments`, `GET /admin/scope-assignments/{id}`, `POST /admin/scope-assignments/{id}/end` with `audit_label` as Query param, `POST /admin/scope-assignments/promote`, `POST /admin/users/{id}/deactivate`) so the verify harness can mutate scope-assignment / user state via HTTP under the API process's admin DSN, never via the verify task's worker DSN — the worker DSN is intentionally zero-privileged on `scope_assignments` and `users` per migration `f392a842f885` (least-privilege by design, NOT a regression to be fixed by GRANTs); (13) `eaa80b5` migrates pillars 12, 13, 14 from direct `SessionLocal()` writes against scope_assignments/users to the new HTTP routes (P12 three sites, P14 four sites, P13 one teardown site, plus removal of now-unused `AuditContext` import in P13); (14) `42e95d1` fixes two harness bugs surfaced by Run 4 — P12 was calling `bind-user(A2)` before `promote()` which violated the Step 24.5b uniqueness invariant (a User holds at most one active Agent per tenant), and P14 was passing `params=` to the verify `call()` helper which has no such kwarg (resolved by inlining `?audit_label=...` into the path; the label is a controlled f-string of alnum/colon/hyphen, no urlencoding required). Production state at v2.1 cut: backend service on `luciel-backend:20` / digest `sha256:3b695018a3e01b0059e9a0ff53328dee1640ead150180cd7bb54f93acb0821bc` (commits 9-13, redeployed 19:46 EDT, services-stable confirmed at 19:47 EDT, deployment PRIMARY/COMPLETED with desiredCount=runningCount=1); verify task on `luciel-verify:5` / digest `sha256:195e30fffc157d4536f84ed96781eb90e9cdc4353d7e1e89cebb4b5602c82d51` (commits 9-14 — commit 14 is harness-only so backend was deliberately not redeployed for it; this is honest scope-cut, not a missed step); final verify task `7b2a5f213b854db5b694245d0040e974` ran 20:01:47 UTC, exitCode 0, FINAL STEP 26 MATRIX `RESULT: 19/19 pillars green` with all four pre-fix failure modes (P10, P12, P14, P19) now PASS. The `luciel-instance hard-DELETE -> 500` line still appears in teardown but does not surface in P10 because P10 walks `state.tenant_id` only and tenant cascade soft-deactivates the row regardless; tracked under existing P3-Q drift `D-luciel-instance-admin-delete-returns-500-2026-05-04` (still observed 2026-05-05; no scope change, deferred to Step 29 hygiene). Five new drifts logged this version: three RESOLVED in-session (D-audit-log-router-not-mounted-pre-commit-9-2026-05-05, D-uuid-not-json-serializable-via-jsonb-2026-05-05, D-verify-harness-direct-db-writes-against-worker-dsn-2026-05-05) and two DEFERRED to Step 29 with explicit follow-up scope (D-verify-task-pure-http-2026-05-05 broader debt of removing all direct-DB session usage from the verify harness; D-call-helper-missing-params-kwarg-2026-05-05 upgrading `app/verification/http_client.py:call()` to accept `params=` so future query-string callers don't have to inline-encode). The architectural lesson preserved for future reference: when a verification harness shares a Docker image with the production backend, it inherits the backend's IAM/DB role boundary; any harness operation that needs to mutate state outside the worker DSN's GRANT set MUST route through an HTTP admin endpoint, never a direct `SessionLocal()` write — the worker DSN's permission boundary IS the security contract, not an inconvenience to grant around. **v2.0 bullet preserved verbatim below for audit continuity.**

**v2.0 bullet preserved verbatim below for audit continuity:**

_Last updated: 2026-05-05 ~17:35 EDT (Step 28 Phase 2 CLOSED — Commits 5, 6, 7 all SHIPPED and verified live in production; tag `step-28-phase-2-complete` to follow). Net result for v2.0:_ Commit 5 stack `luciel-prod-alarms` deployed clean (7 alarms, SNS topic `luciel-prod-alerts`, email subscription confirmed by operator at 2026-05-05 ~16:49 EDT, heartbeat alarm transitioned `ALARM → OK` at 16:45:23 EDT proving end-to-end alerting plumbing on real production heartbeat datapoints of `Sum=4.0` per 60s window from worker rev 11). Commit 6 stack `luciel-prod-worker-autoscaling` deployed clean (ScalableTarget on `service/luciel-cluster/luciel-worker-service` with capacity 1-4 on `ecs:service:DesiredCount`, single CPU TargetTracking policy `luciel-worker-cpu-target-tracking` at 60% with cooldowns 60s/300s, AWS service-linked role `AWSServiceRoleForApplicationAutoScaling_ECSService` attached). Production undisturbed across both deploys: `luciel-worker-service` ACTIVE, 1 desired / 1 running / 0 pending, rolloutState COMPLETED, 0 failedTasks. Two new drifts logged this version (both RESOLVED in-session via `f49eae4` and `69d1a3a`, see Section 15). One drift logged as DEFERRED with explicit follow-up scope: `D-celery-broker-not-verified-deferring-backlog-autoscaling-2026-05-05`. **Last updated (earlier today, v1.9, 2026-05-05 ~16:15 EDT)**: Phase 2 Commit 7 SHIPPED — worker container health checks live in production via rev 11 producer-heartbeat / mtime-probe design after a four-iteration evolution; Commit 5 CloudWatch alarms IN PROGRESS at v1.9 cut.

**v1.9 bullet preserved verbatim below for audit continuity:**

_Phase 2 Commit 7 SHIPPED — worker container health checks live in production via rev 11 producer-heartbeat / mtime-probe design after a four-iteration evolution; Commit 5 CloudWatch alarms IN PROGRESS._ Net result: worker rev 11 RUNNING HEALTHY in `luciel-worker-service` on `luciel-worker:11` (image digest `sha256:f5ae6997cf2a9f3b75a1488994810f61054c8fbf1299a2e106be8558763f3da0`, tag `worker-rev11`); 17 heartbeat log lines observed in `/ecs/luciel-worker` at 15s cadence (drift 7ms over 3.5min); single PRIMARY deployment, `rolloutState: COMPLETED`, `failedTasks: 0`. Six new drifts logged this version (all RESOLVED in-session, see Section 15). The full forensic narrative of the rev 7→8→9→10→11 evolution lives at `docs/recaps/2026-05-05-commit-7-healthcheck-rev11.md`. Previous (v1.8) bullet preserved verbatim below for audit continuity.

**Earlier (v1.8, 2026-05-05 ~10:53 EDT, P3-S Half 2 Step 5 GREEN — Commit 4 worker DB role swap COMPLETE).** After Commit `0cd87be` (pg_roles + SSM-presence rotation guard + 11-test smoke suite + recap v1.7) and Commit `65f8996` (`mint-td-rev3.json` with new image digest), the rev-3 cycle ran cleanly end-to-end:

- **Step 4 retry on `luciel-mint:3` (dry-run, 2026-05-05 14:48:55 UTC)**: Fargate task `ed5fd118ce024fa8b1e1cce15552eee3` exitCode 0; CloudWatch confirmed the new four-stage pre-flight callout `(pre-flight SSM-writable + first-mint-or-force-rotate + DB connect + role-state PASSED)` — the explicit phrasing is the structural proof that the read-only `pg_roles` SELECT actually executed in dry-run, closing `D-dry-run-validates-subset-of-real-run-pg-authid-not-exercised-2026-05-05`. `pw_fingerprint cd9a489dd131` (throwaway dry-run value), `force_rotate False`. Zero production state mutated.
- **Step 5 real-run on `luciel-mint:3` (2026-05-05 14:51:27 UTC)**: Fargate task `27638cebbd8349f8bcb8d70e4c55714b` exitCode 0; CloudWatch banner `WORKER DB PASSWORD MINTED`; `pw_fingerprint ff89f2831b32` (sha256 first 12 of the actual minted password — fingerprint is the integrity anchor, not a leak); `pw_length 43 chars` (matches `secrets.token_urlsafe(32)` base64url); `force_rotate False` (first-mint path). Pattern E redaction held perfectly: no `postgresql://` strings, no DSN, no raw password, no stack traces in container stdout/stderr. **Production state mutated as designed**: (a) `luciel_worker` ALTER ROLE PASSWORD committed to RDS master (old bootstrap password is now dead in RDS), (b) SSM SecureString `/luciel/production/worker_database_url` v1 created (KMS-encrypted, `LastModifiedDate 2026-05-05T14:51:29.156Z` — SSM PutParameter ran ~2s after ALTER ROLE; the script's atomicity defenses ensured no partial-mutation window). Both mutations independently verified: `aws ssm get-parameter` (metadata only, no `--with-decryption` per `D-admin-dsn-disclosed-in-chat-2026-05-05`) confirmed Name/Type/Version/LastModifiedDate. Two MFA TOTPs burned (one for dry-run, one for real-run); both consumed productively, zero burned uselessly on rev 3. Helper bailed at line 394 on both runs (known `D-mint-helper-aws-stderr-causes-native-command-error-2026-05-05`, deferred); ceremony correctness was unaffected and the manual log-readback added <60 s per ceremony.

**The `verify_first_mint_or_force_rotate` guard is now armed**: with SSM v1 present, any future `mint-via-fargate-task.ps1` invocation without `--force-rotate` will be refused (covered by 4 GREEN smoke tests; not exercised live). **Phase 2 Commit 4 is complete — the Pattern N ceremony is proven end-to-end through real production mint.** Sections §4.3 (worker task-def update to read the new SSM path) and §4.4 (rolling deploy) are next; both run under the operator's default identity (no MFA cycles needed) and are deferred to the next session for fresh-head review of the worker rolling-deploy surface. Three deferred items remain inside Phase 2: helper line-394 stderr polish, `D-iam-policy-review-relies-on-mental-model-2026-05-05` static analyzer (Phase 3), `D-proxy-vs-github-remote-not-explicitly-verified-2026-05-05` (Phase 3). No new drifts logged this version. Previous version's bullet preserved verbatim below for audit continuity.

**Earlier (v1.7, 2026-05-05 ~10:35 EDT, mid-fix-cycle pause)** — Step 5 real-run first attempt on `luciel-mint:2` (task `6dc293a3be784529948e7a7dc0e73091`) failed cleanly with `FATAL: role update failed: InsufficientPrivilege: permission denied for table pg_authid`. Zero production state mutated. Root cause: `pg_authid` unreadable by `rds_superuser` on AWS RDS. Patch landed in commit `0cd87be`: `verify_role_state` switched to `pg_roles` (RDS-readable sanitized view); rotation-vs-first-mint signal moved from `pg_authid.rolpassword IS NULL` (masked as `********` in `pg_roles`) to SSM-presence via new `verify_first_mint_or_force_rotate(*, region, ssm_path, force_rotate)` (`ParameterNotFound` ⇒ first mint, allow; parameter exists + `--force-rotate` ⇒ rotation, allow; parameter exists, no flag ⇒ refuse). 11-test smoke suite at `tests/test_mint_worker_db_password_ssm.py` (TestVerifyRoleStateUsesPgRoles × 4, TestVerifyFirstMintOrForceRotate × 4, TestEnvVarOnlyPath × 3) all GREEN locally. Two drifts logged this version: `D-mint-script-uses-pg-authid-not-readable-on-rds-2026-05-05` and `D-dry-run-validates-subset-of-real-run-pg-authid-not-exercised-2026-05-05` — the latter resolved structurally by extending pre-flight to also run `verify_role_state(_preflight_conn)` against the connection that is opened-then-closed in BOTH dry-run and real-run paths.

**Earlier (v1.6, 2026-05-05 ~10:19 EDT, Step 4 dry-run GREEN)**, after Commit `251a0f3` (mint-helper file:// JSON fix), Commit `7514b98` (mint-operator role 9-Sid expansion for ecs:RunTask + iam:PassRole + DescribeTasks/StopTask + log-read), Commit `97e928d` (mint-td-rev2.json swapping in image digest with patched script), and ECR rebuild + push of `luciel-backend:p3-s-half-2-2026-05-05` (digest `sha256:3275322b7ef80a508680dced0ed4a142de77b0d3fd984318878bc57157ff7b95`). **P3-S Half 2 Step 4 (Pattern N dry-run mint) is GREEN**: Fargate task `33908e96941d4dbda45594f241565c3b` launched on `luciel-mint:2`, exited 0 cleanly, CloudWatch logs show `(pre-flight SSM-writable + DB connect-only PASSED)` plus explicit `DRY RUN -- no Postgres or SSM writes performed` line. Zero production state mutated. The Pattern N ceremony is now proven end-to-end through dry-run. Step 5 (real-run) is gated behind a fresh `confirm_action` + new MFA TOTP. Six new drifts logged this session — see Section 15. Previous version's bullet preserved verbatim below for audit continuity.

**Earlier (v1.5, 2026-05-04 ~20:18 EDT)** (session-end pause), after Commit A (`81b9e5a`), Commit D (`55a36b4`), the repo-hygiene `.gitignore` cleanup (`86239ab`), the **runbook §4 v2 revision** (`374912a`), the **runbook §4.2 follow-up patch** (`6d596f7`), the **P3-K-followup mint-role policy patch** (`e1154bd`), and the **session-end recap + P3-S backlog** (this commit). The Commit 4 mint ceremony attempt today was correctly refused twice by the mint script's defensive layers — first by `preflight_ssm_writable` (resolved by `e1154bd`), then by `psycopg.connect()`'s outer try/except when the laptop could not reach private-VPC RDS. **Zero production state mutated; Pillar 13 A3 fix remains live and 19/19 green.** Two new open drifts logged: `D-option-3-ceremony-cannot-reach-private-rds-from-laptop-2026-05-04` and sister `D-mint-script-dry-run-skips-preflight-2026-05-04`. New Phase-3 backlog item P3-S (P0, blocks Phase 2 close): re-architect the mint ceremony as a Pattern N variant (Fargate one-shot task running mint script in-VPC). Recommended sub-option P3-S.a: dedicated `luciel-mint` task definition + task role. Estimated 60-90 min in a fresh session.

**Earlier in this session:** the P3-K-followup mint-role policy patch (`e1154bd`). The P3-K-followup adds two new statements (`ReadWorkerSsmForPreflightAndMint`, `EncryptWorkerSsmSecureStringViaSsm`) to `infra/iam/luciel-mint-operator-role-permission-policy.json` after the v2.1 mint dry-run passed but the real run was blocked by `mint_worker_db_password_ssm`'s `preflight_ssm_writable` atomicity check. **The pre-flight working as designed prevented production state mutation — zero `ALTER USER`, zero SSM write, zero half-completed mint.** Root cause: P3-K's original policy was scoped only for admin-DSN read, but the Option 3 ceremony runs the mint script INSIDE the assumed role and so requires worker-SSM write rights on the role itself. Drift entries `D-runbook-mint-missing-workerhost-arg-2026-05-04` and `D-p3-k-policy-missing-worker-ssm-write-2026-05-04` log the fix-on-fix honestly. Three new Phase-3 backlog items added: P3-R (MFA TOTP echoes in PowerShell during mint ceremony — P2, defense-in-depth). The runbook revision rewrites `docs/runbooks/step-28-phase-2-deploy.md` §4 to mandate the Option 3 ceremony (`scripts/mint-with-assumed-role.ps1`) for Commit 4, removes the old §4.7 (`luciel_admin` rotation already done by P3-H on 2026-05-03 23:56 UTC), corrects SSM-path casing to canonical lowercase `/luciel/production/worker_database_url`, and adds a §4.0 pre-mint checklist + four-row prerequisite gate table (P3-J/K/G/H all ✅). v1 of §4 is preserved in commit `925c64a` and explicitly superseded in the revision-history block at the top of §4. Commit A fixed a one-line auth-middleware typo (`user_id = agent.user_id` shadowed the never-read local while leaving `request.state.actor_user_id` bound to `None`) that caused **every Pillar 13 A3 legitimate setup-turn `MemoryItem` insert to fail Postgres D11 NOT NULL and be silently swallowed** by the extractor's broad `except Exception`. Post-fix verification ran **19/19 GREEN** including Pillars 11 (async memory), 13 (spoof + legit), 16 (D11). Drift entry `D-pillar-13-a3-real-root-cause-2026-05-04` resolved by `81b9e5a`. Commit D archived the 19/19 run to `docs/verification-reports/` and removed the P13_DIAG instrumentation. Five new Phase-3 items (P3-M, P3-N, P3-O, P3-P, P3-Q) logged for the compounding observability/hygiene gaps the bug exposed. The repo-hygiene commit pulled forward `D-gitignore-duplicate-stanzas-2026-05-01` from Phase 4 (corrupted UTF-16 line + 6 duplicate patterns + stray quote). Broader repo audit found no other deletable orphans — historical runbooks/recaps and root-level task-def JSONs were preserved as audit evidence per the canonical "don't delete audit history" protocol. v1.5 supersedes v1.4's pre-fix Pillar-13 framing.
**Supersedes:** v2.0 (2026-05-05 ~17:35 EDT, Commits 5/6/7 shipped, prod-verify checkbox open); v1.9 (2026-05-05 ~16:15 EDT, Commit 7 SHIPPED + Commit 5 IN PROGRESS); v1.8 (2026-05-05 ~10:53 EDT, Commit 4 worker DB role swap COMPLETE); v1.7 (2026-05-05 ~10:35 EDT, mid-fix-cycle pause); v1.6 (2026-05-05 ~10:19 EDT, Step 4 dry-run GREEN); v1.5 (2026-05-04 ~20:18 EDT, session-end pause pre-P3-S Half 2); v1.4 (2026-05-03 23:56 UTC, post P3-H); v1.3 (2026-05-03 late-evening, post P3-J/G/K); v1.2 (2026-05-03 evening, mid-Phase-2 docs reconciliation); v1.1 (2026-05-03 Phase 2 mid-stream close); v1 (2026-05-03 Phase 1 close)
**Next update:** at Phase 3 close OR when a strategic-question answer changes. Phase 2 is now CLOSED with all four target commits (Commits 4-7) shipped and verified.
**Source-of-truth rule:** if a chat recap or session summary contradicts this document, this document wins. Update via PR with rationale; do not produce contradicting recaps inline.

---

## Section 0 — Locked items (re-derivation prohibited)

These are settled. Any future recap must read from this section, not infer.

### 0.1 Locked: Strategic question answers (Q1–Q8)

| Q | Answer | Roadmap slot | Status |
|---|---|---|---|
| **Q1** Two-key confusion → unified scope creation | Single admin permission; caller's scope dictates what they can create. Agent ↔ LucielInstance split. One tenant-admin key at onboarding | Steps 23, 24.5/25a | ✅ Done |
| **Q2** Tenant/domain/agent dashboards showing business value | Three-tier views driven by trace aggregations + `DomainConfig.value_metrics` + workflow actions | Step 31 (after 34) | 📋 Planned |
| **Q3** Vector + graph hybrid retrieval | Yes; pg-recursive CTEs first, opt-in per domain via `DomainConfig.entity_schema`; graduate to Neo4j/AGE at 100 tenants | Step 37 (after 35) | 🔬 Decision-gate |
| **Q4** Luciels communicating within a scope (Councils) | Yes; orchestrator Luciel; inter-Luciel tool calls; ScopePolicy policed at tool-call time; widget key can resolve to council_id; optional column on LucielInstance | Step 36 (after 33 eval gate) | 📋 Planned |
| **Q5** Bottom-up expansion (Sarah → her domain → her company) | Email-stable User identity; tenant-merge endpoint re-parents Luciels/knowledge/memories/sessions; pricing tiers + pro-rated credit | Step 38 (after 35) | 📋 Planned |
| **Q6** Role changes (promote/demote/depart) | Data lives with scope, not person; `users` + `scope_assignments`; mandatory hard key rotation; immutable audit log; Luciels and knowledge owned by scope | Step 24.5b, commit `adc5ba0` | ✅ Done |
| **Q7** Multi-channel delivery (widget/phone/email/SMS) | Channel adapter framework; inbound webhooks + outbound Tool registrations scoped via ScopePolicy; channels emergent from config, not a column | Step 34a candidate (between 33 and 34) | 📋 Candidate |
| **Q8** Cross-channel session continuity (widget ↔ phone) | Add `conversation_id` FK on sessions (session-linking, not session-merging); cross-session retriever pulls recent messages from other open sessions in same conversation; phone/email become identity claims linked to `users.id` | Step 24.5c candidate (after 28, before 30) | 📋 Candidate |

### 0.2 Locked: Pricing tier structure (2026-05-01 v3 reframe)

| Tier | Price | Audience | Scope |
|---|---|---|---|
| **Individual** | $30–80/mo | Single agent ("Sarah") | One agent scope, her LucielInstances only |
| **Team / Domain** | $300–800/mo | A team within a brokerage | Domain scope, multiple agents under it |
| **Company / Tenant** | $2,000+/mo | Whole brokerage | Tenant scope, all domains + agents |

**Structural insight:** Pricing tiers map 1:1 to the scope hierarchy `agent → domain → tenant`. The architecture diagram and the price card are the same diagram. **Scope-correctness IS the price-card enforcer** — leaked agent → sibling-agent read at Individual tier gives away Team-tier value at Individual-tier price.

**Tier B (Step 33b):** Per-customer dedicated AWS account/RDS/ECS. Build only when first prospect demands. Most enterprise needs handled by Tier 3 multi-tenant.

**Tier C (on-prem):** Excluded unless paid prospect asks. Tier B is highest pre-built isolation tier.

### 0.3 Locked: Roadmap (Steps 24.5c–38)

| Category | Step | Description | Status |
|---|---|---|---|
| Hardening | **28** | Operational maturity sprint — security/compliance, observability, hygiene, cosmetic — **4 phases**, 13–15 commits | **Phase 1 complete** at `bd9446b` (tag `step-28-phase-1-complete`). **Phase 2 code-only portion shipped** (Commits 2/2b/3/8/9 on `step-28-hardening-impl`); Phase 2 prod-touching commits 4–7 packaged as code+IaC+runbook (`docs/runbooks/step-28-phase-2-deploy.md`), pending hands-on prod execution. Phases 3/4 pending |
| Identity | **24.5c** | User identity claims (phone/email/SSO) + conversation grouping (Q8) | Candidate, slot before Step 30 |
| Testing | **29** | Automated testing suite (pytest wrap of `app.verification`) | Planned |
| Billing | **30a** | Stripe billing (subscription tiers, webhooks, tier-gated features) | Candidate, slot between 29 and 30 |
| Frontend | **30b** | Embeddable chat widget (JS drop-in for tenant sites) — **highest-leverage commit, REMAX trial unblock** | Planned |
| Frontend | **31** | Hierarchical tenant dashboards (Q2) | Planned (after 34) |
| Frontend | **32** | Agent self-service (Sarah spins up her own LucielInstances under her agent scope) | Planned |
| Frontend | **32a** | File input support (per-agent config, builds on 25b parsers) | Planned (may merge with 32) |
| Intelligence | **33** | Evaluation framework (relevance, persona consistency, escalation precision scoring) | Planned |
| Enterprise | **33b** | Dedicated tenant infrastructure tier (per-customer AWS account) | Candidate, build when prospect demands |
| Intelligence | **34** | Workflow actions (book appointment, send email, create lead, query DB) — unlocks Step 31 with real business-value data | Planned |
| Intelligence | **34a** | Channel adapter framework (SMS/email/phone, Q7) | Candidate, slot between 33 and 34 |
| Intelligence | **35** | Multi-vertical expansion framework (repeatable playbook for legal/mortgage/engineering) | Planned |
| Advanced | **36** | Luciel Council (multi-Luciel orchestration within scope, ScopePolicy at tool-call time, Q4) | Planned (after 33 eval gate) |
| Advanced | **37** | Hybrid retrieval (graph + vector, pg-recursive CTEs first, Neo4j/AGE at 100 tenants, Q3) | Planned (after 35) |
| Advanced | **38** | Bottom-up expansion / tenant merge (email-stable identity re-parents Luciels/knowledge/memories/sessions, Q5) | Planned |

### 0.4 Locked: Architectural decisions

- **Scope hierarchy IS billing boundary** (4 levels: tenant → domain → agent → luciel_instance, plus orthogonal users/api_keys/scope_assignments)
- **Soft-delete model** — all DELETE endpoints flip `active=false`. No hard-delete API surface. Hard-purge is scheduled retention worker (future).
- **Tenant cascade in code** — `PATCH /api/v1/admin/tenants/{id}` with `active=false` triggers atomic in-code cascade through all 7 child resource types. Walker is now backup tool.
- **Driver:** psycopg v3 (not psycopg2)
- **Manifest:** pyproject.toml (not requirements.txt)
- **Schema naming:** `_configs` suffix (tenant_configs, domain_configs, agent_configs)
- **Prod region:** ca-central-1, account 729005488042
- **Prod URL:** api.vantagemind.ai (ALB-fronted, NOT API Gateway)
- **Database:** RDS Postgres, master role `luciel_admin`. New role `luciel_worker` exists with least-privilege grants. **Step 28 Phase 2 Commit 4 will swap the worker to use it AND rotate the `luciel_admin` password** — packaged as runbook, pending hands-on execution.
- **Retention purges are batched** as of Phase 2 Commit 8 (`0d75dfe`). Defaults: 1000 rows/batch, 50 ms inter-batch sleep, 10000 batches/run cap. `FOR UPDATE SKIP LOCKED` keeps purges safe to run alongside live chat traffic. Tunable via `LUCIEL_RETENTION_*` env vars.
- **Operator patterns codified:** E (secrets), N (migrations), O (recon), S (cleanup walker)
- **Three-channel audit** for every prod mutation: CloudTrail + CloudWatch + admin_audit_logs

### 0.5 Locked: Deliberate exclusions

These require **roadmap-level conversation** to add, not commit-level.
- **No mobile app** — chat widget covers surface
- **No marketplace / user-generated Luciels** — verticals are operator-defined
- **No model training / fine-tuning** — foundation models via API; differentiation is judgment + integration depth
- **No internationalization** — ca-central-1, English-only, until customer demand surfaces
- **No Tier C on-prem** — unless paid prospect asks
- **No competitor feature-parity chasing** — if not on roadmap, deliberately out of scope

---

## Section 1 — Business model and identity

**What Luciel is:** B2B SaaS, multi-tenant AI assistant platform. Subscription tiers map 1:1 to scope hierarchy. Architecture and price card are the same diagram, scaled.

**Wedge market:** GTA brokerage. **REMAX Crossroads** = first warm lead, Tier 3 (Company-tenant, $2,000/mo). Markham-local outreach.

**Customer entity:** The tenant (a brokerage). **NOT** the end-user (the brokerage's clients).

**Initial vertical:** GTA real estate. Adjacent verticals later: mortgage, legal, engineering consulting (vertical-Q1 strategic question pending real lead signal).

---

## Section 2 — Architecture

### 2.1 Scope hierarchy (architectural primitive AND monetization primitive)
tenant → domain → agent → luciel_instance
Plus orthogonal: `user` (platform identity, tenant-agnostic, FK target for `actor_user_id`), `api_key` (auth credential, scoped to any level), `scope_assignment` (user-tenant binding).

### 2.2 Data tables

**Live-aware** (have `active` column, soft-delete model):
- tenant_configs, domain_configs, agents, agent_configs (legacy), luciel_instances, api_keys, scope_assignments, **memory_items** (active column added Step 27b)

**Append-only** (no `active` column):
- sessions, messages, traces, admin_audit_logs, knowledge_embeddings, retention_policies, deletion_logs, user_consents

**Identity layer:**
- users (no tenant_id, platform-wide)

### 2.3 memory_items — most sensitive data
Distilled user inferences from Step 27b async extraction. Contains preferences, identity facts, behavioral patterns. PIPEDA P5 (limit retention) target.

Columns: `user_id` (free-form brokerage-supplied), `tenant_id` (NOT NULL), `agent_id` (nullable), `category`, `content`, `active`, `message_id` FK, `luciel_instance_id` FK, `actor_user_id` (UUID FK to users, **NOT NULL** as of `f392a842f885` applied 2026-05-02).

Scope: 3-level (tenant + agent + luciel_instance). Domain inferred via agent (no `domain_id` column on memory_items).

### 2.4 Auth + audit
- API keys: `luc_sk_` prefix + 12-char prefix indexed for audit
- AuditContext factories: `from_request()` (HTTP), `system()` (background jobs), `worker()` (Celery — preserves enqueuing key prefix, fixed Commit 14)
- AdminAuditLog: canonical audit trail, every mutation writes one row in same transaction, ALLOWED_ACTIONS allow-list
- Three-channel audit pattern: CloudTrail + CloudWatch + admin_audit_logs

### 2.5 Cascade discipline (Commit 12, `f9f6f79`)
PATCH `/api/v1/admin/tenants/{id}` with `active=false` triggers atomic in-code cascade through 7 leaves:
1. memory_items (broadest scope)
2. api_keys
3. luciel_instances (all scope levels)
4. agents (new-table)
5. agent_configs (legacy)
6. domain_configs
7. tenant_config itself

Plus sub-tenant cascade on agent/domain/instance deactivation. Pillar 18 enforces end-to-end. Walker is now backup tool.

### 2.6 Operator patterns (codified in `docs/runbooks/operator-patterns.md`)
- **Pattern E** — Secret handling discipline (Commit 9, `bd9446b`)
- **Pattern N** — Prod migrations via `luciel-migrate:N` ECS one-shot
- **Pattern O** — Read-only prod recon via `luciel-recon:1` ECS one-shot
- **Pattern S** — Per-resource cleanup walker (backup tool as of Commit 12)

---

## Section 3 — What's accomplished (Step 28 Phase 1 complete; Phase 2 code-only commits shipped)

### 3.1 Phase 1 commits shipped (chronological, verified from git log)

| Hash | Date | Commit |
|---|---|---|
| `adc5ba0` | 2026-04-27 | Step 24.5b — Durable User Identity Layer (Q6 + Q5 prerequisite) |
| `81c0088` | 2026-04-27 | D1 closure — rotate leaked local platform-admin key |
| `330d975` | 2026-04-27 | Master plan + Phase 1 tactical plan + drift register seed |
| `e024dd4` | 2026-04-27 | Mid-Phase-1 canonical recap |
| `679db3d` | 2026-04-27 | D16 consent route double-prefix fix + dependent test callsites |
| `baf06f7` | 2026-04-27 | D11 memory_items.actor_user_id NOT NULL flip + Pillar 16 |
| `67c65a5` | 2026-04-30 | D5 — retrofit ApiKeyService.deactivate_key with audit_ctx |
| `40d9fb8` | 2026-04-30 | D-worker-role — least-privilege luciel_worker Postgres role |
| `ca55b12` | 2026-04-30 | Commit 8a — luciel-worker mint script + worker-sg runbook |
| `8028699` | 2026-05-01 | Commit 8a (artifacts only second instance) |
| `7560397` | 2026-05-01 | Commit 8b-prereq — fix luciel-migrate task-def + codify Pattern N |
| `9ff2690` | 2026-05-01 | Commit 8b-prereq-data — codify Pattern O + discover prod tenant residue |
| `15bd315` | 2026-05-01 | Commit 8b-prereq-cleanup — clear 18 active verification residue tenants via Pattern S walker |
| `f9f6f79` | 2026-05-02 | **Commit 12: tenant cascade in code (PIPEDA P5)** — deployed + smoke-tested in prod |
| `3d64ca9` | 2026-05-02 | chore — untrack msg.txt session-authoring scratchpad |
| `62a5783` | 2026-05-02 | **Commit 11: orphan cleanup migration** — applied to prod, 3 migrations + NOT NULL flip live |
| `6b71bcb` | 2026-05-02 | **Commit 10: admin memory endpoints + stash integration** — deployed |
| `2e31797` | 2026-05-02 | **Commit 14: worker audit attribution fix** (Pillar 13 A2) |
| `bd9446b` | 2026-05-03 | **Commit 9: Phase 1 close** — Pattern E codified, tag `step-28-phase-1-complete` |

### 3.1b Phase 2 commits shipped (code-only portion)

Branch: `step-28-hardening-impl` (NOT yet merged to `step-28-hardening`).

| Hash | Commit |
|---|---|
| `75f6015` | **Phase 2 Commit 2** — audit-log API mount (`GET /api/v1/admin/audit-log`); closes recap §4.1 item 4 + drift `D-audit-log-api-404` |
| `bfa2591` | **Phase 2 Commit 2b** — audit-log review fixes: H1 (route prefix), H2 (per-resource `_SAFE_DIFF_KEYS` allow-list redaction), H3 (real second-tenant scope guard test), M1/M3, L1/L2 |
| `56bdab8` | **Phase 2 Commit 3** — Pillar 13 A3 sentinel-extractable fix (user-fact-shaped setup turn, A3 keyed on `MemoryItem.message_id` FK, 30 s polling). Brings dev verification 17/18 → 19/19 (Pillar 19 audit-log mount also green). |
| `0d75dfe` | **Phase 2 Commit 8** — batched retention deletes/anonymizes via `FOR UPDATE SKIP LOCKED LIMIT n` chunks with per-batch commit. Settings: `retention_batch_size`, `retention_batch_sleep_seconds`, `retention_max_batches_per_run`. Partial-failure semantics: writes `DeletionLog` with `"PARTIAL: ..."` reason then re-raises. Removes outer `db.rollback()` from `enforce_all_policies` (now harmful with per-batch commits + auto-committing audit log). |
| `925c64a` | **Phase 2 Commit 9** — Phase 2 close (code-only portion): canonical recap v1.1 + new `docs/runbooks/step-28-phase-2-deploy.md` covering all 9 Phase-2 commits incl. runbook sections for prod-touching Commits 4–7. |
| `2c7d0fb` | **Phase 2 HOTFIX** — Pillars 7 (test drift), 17 (real bug), 19 (test design flaw); restores dev verification to green and unblocks Commit 4 attempt. |
| `31e2b16` | **Phase 3 compliance backlog seeded** — `docs/PHASE_3_COMPLIANCE_BACKLOG.md` (P3-A through P3-G). Items surfaced during Phase 2 hotfix diagnosis but deliberately deferred to Phase 3 to keep Phase 2 focused. |
| `81b9e5a` | **Phase 2 Commit A (Pillar 13 A3 real fix)** — one-line auth-middleware binding fix (`user_id = agent.user_id` → `actor_user_id = agent.user_id`, `app/middleware/auth.py:124`) + 12-line forensic comment + 5-test regression guard at `tests/middleware/test_actor_user_id_binding.py` (AST canary + 4 behavioral). Two-way proven (FAIL with bug, PASS with fix). Resolves drift `D-pillar-13-a3-real-root-cause-2026-05-04`. Post-fix verification 19/19 green. |
| `13035da` | **Phase 2 Commit D (P13_DIAG removal + verification archive)** — strips P13_DIAG instrumentation from `app/middleware/auth.py` (17 lines) and `app/services/chat_service.py` (41 lines), deletes `diag_p13_repro.py` (269 lines), archives 19/19 verification report at `docs/verification-reports/step28_phase2_postA_sync_2026-05-04.json` + README. Commit B (extractor B-hybrid) and Commit C (async-flag flip) WITHDRAWN — second post-Commit-A repro proved the system was always architected correctly for prose+tool-call replies and Pillar 11 (async path) was already green. Net −147 lines. See `docs/recaps/2026-05-04-pillar-13-a3-real-root-cause.md` for full forensic narrative including 3 discarded hypotheses. |
| `2b5ff32` | **Phase 2 Commit 4 mint-script hardening** — `scripts/mint_worker_db_password_ssm.py` rebuilt to never log a constructed DSN, never accept admin DSN as runtime input, suppress full-DSN error bodies, and log only sanitized SSM ARN identifiers. Authored after a dry-run of the original mint script leaked the admin DSN (incl. password `LucielDB2026Secure`) into CloudWatch log group `/ecs/luciel-backend` stream `migrate/luciel-backend/d6c927a05eb943b5b343ca1ddef0311c`. |
| `43e2e7a` | **Mint-incident recap** — `docs/recaps/2026-05-03-mint-incident.md`. Five root causes: (1) admin DSN as input parameter; (2) full-DSN error bodies; (3) shared `luciel-ecs-migrate-role` task role with no separation between migrate and mint duties; (4) no MFA enforcement on the human identity (`luciel-admin`) doing privileged ops; (5) no compensating control for accidental log-line leakage. Drove the P3-J / P3-K / P3-H additions to the Phase 3 backlog. |
| `837da98` | **Phase 2 Commit 7 rev 7 attempt** — `worker-td-rev7.json` adds container `healthCheck` block invoking `celery -A app.worker.celery_app inspect ping -d celery@$HOSTNAME`. Failed in production: ECS reported task UNHEALTHY because Fargate `$HOSTNAME` did not match the celery worker hostname (auto-derived from `socket.gethostname()` mid-init). First instance of the family of unobservable-probe failures. |
| `27723b0` | **Phase 2 Commit 7 rev 8 attempt** — `worker-td-rev8.json` switches probe to `celery -A app.worker.celery_app inspect ping` (no `-d` filter, accepts any responder). Failed in production: probe stdout went to Docker's per-container health buffer, NOT awslogs, so the failure mode was diagnosable only by `aws ecs describe-tasks ... healthStatus`, not by log search. Confirmed `D-celery-inspect-ping-unobservable-on-fargate-2026-05-05`. |
| `594821e` | **Phase 2 Commit 7 rev 9 infrastructure** — Authors `scripts/healthcheck_worker.py` (parses `celery inspect ping --json`, exits 0 on any responder, exits 1 otherwise) and adds `procps` to the Dockerfile (rev 8 had failed in part because `pgrep` was unavailable in scratch-derived images, useful for a fallback liveness check). Image digest baked into rev 9. |
| `bb6dd7a` | **Phase 2 Commit 7 rev 9 attempt** — `worker-td-rev9.json` invokes the new healthcheck script via `python /app/scripts/healthcheck_worker.py`. Failed in production: `pip install -e .` from earlier image bake had set the entrypoint argv0 to `python` not the script name, breaking the heredoc invocation. Confirmed `D-pip-entrypoint-argv0-is-python-not-script-name-2026-05-05`. |
| `d56f08c` | **Phase 2 Commit 7 rev 10 fix** — Patches `scripts/healthcheck_worker.py` to use element-membership match (`celery@<hostname>` ∈ responder set) instead of full-equality string compare, after rev 9's element-equality logic miscompared a list-of-one against a bare string. Failed in production: probe still ran inside celery's process tree and the CMD-SHELL stdout still wasn't observable in awslogs — meaning element-membership semantics were correct but the diagnosis pathway was structurally broken. Final instance of "observable-by-design" being absent. |
| `dbdc469` | **Phase 2 Commit 7 rev 10 attempt** — `worker-td-rev10.json` deploys the rev-10 patch. Failed in production with the same observability gap as rev 9. Decision point: stop iterating on the inspect-ping probe family; redesign with the producer outside the probe so that producer logs go to awslogs and any failure is diagnosable from CloudWatch. |
| `079f327` | **Phase 2 Commit 7 rev 11 design — the win.** `app/worker/celery_app.py` +87 lines: hooks `worker_ready` signal, daemon thread touches `/tmp/celery_alive` every 15s and logs `healthcheck heartbeat: touched /tmp/celery_alive` at INFO (visible in awslogs); `worker_shutdown` signal stops the thread cleanly via `threading.Event`. `scripts/healthcheck_worker.py` rewritten as 4-line `os.stat()` probe on `/tmp/celery_alive`, exit 0 if mtime within 60s window, else 1 — pure stdlib, sub-ms latency, zero broker/celery imports. Probe topology inverted: producer is observable, probe is silent. Resolves the entire family of rev 7→10 failures. |
| `fceb7e9` | **Phase 2 Commit 7 rev 11 deploy** — `worker-td-rev11.json` (image digest `sha256:f5ae6997cf2a9f3b75a1488994810f61054c8fbf1299a2e106be8558763f3da0`, tag `worker-rev11`). HEALTHCHECK config: `interval: 30, timeout: 15, retries: 3, startPeriod: 60`. Deployed via `aws ecs update-service --service luciel-worker-service --task-definition luciel-worker:11 --force-new-deployment` at 16:10 EDT; deployment `rolloutState: COMPLETED` at 16:14 EDT; `desired=1, running=1, pending=0`; running task `6a7be8840a184b44bf89879c67e1d886` started 16:10:49 EDT. CloudWatch evidence: 17 events from `/ecs/luciel-worker` matching `healthcheck heartbeat`, first at 20:10:55.521 (`initial touch of /tmp/celery_alive`), then 15 periodic ticks at exactly 15.000s ± 1ms cadence. **Commit 7 ✅ DEPLOYED HEALTHY.** Full forensic narrative of the four-iteration journey: `docs/recaps/2026-05-05-commit-7-healthcheck-rev11.md`. |

**Not yet shipped (prod-touching, packaged for hands-on execution):**

| # | Description | Why packaged-not-executed |
|---|---|---|
| ~~4~~ | ~~Worker DB role swap to `luciel_worker` + `luciel_admin` password rotation~~ ✅ **SHIPPED 2026-05-05 14:51:27 UTC** — see §3.1b commits `0cd87be` + `65f8996`; SSM `/luciel/production/worker_database_url` v3 (post-leak rotation); worker rev 6 deployed and verified RDS auth GREEN. Original blocker bullet preserved below for audit continuity. **BLOCKED on P3-J + P3-K (Option 3 architecture).** First mint attempt (2026-05-03) leaked the admin DSN to CloudWatch via `--dry-run` error body. Hardened mint script (`2b5ff32`) is necessary but not sufficient: prerequisites are (a) MFA enabled on `luciel-admin` per P3-J; (b) dedicated `luciel-mint-operator-role` (MFA-required, scoped to `ssm:GetParameter` on `/luciel/database-url` + KMS Decrypt) per P3-K; (c) leaked password rotation per P3-H (rotate `luciel_admin`, delete the leaking log stream). Migrate task role NEVER receives read on admin DSN. Runbook §4 must be revised to invoke the mint via `aws sts assume-role --serial-number ... --token-code ...` ceremony before re-attempt. |
| 5 | 6 CloudWatch alarms (worker-no-heartbeat via log MetricFilter, UnhealthyTaskCount, RDS connection count, RDS CPU, worker error log rate, SSM GetParameter failures) + SNS pipeline (`luciel-prod-alerts`, subscribe `aryans.www@gmail.com`) | **IN PROGRESS 2026-05-05 ~16:15 EDT.** Touches CloudWatch + SNS. CFN stack at `cfn/luciel-prod-alarms.yaml` chosen over CLI for reproducibility/auditability. Runbook: §5. |
| 6 | ECS Application Auto Scaling target tracking on `luciel-worker-service` (signal TBD: SQS depth via custom metric or CPU); `luciel-backend-service` already covered by ALB target group health. | Touches ECS + Application Auto Scaling. Runbook: §6. |
| ~~7~~ | ~~Container-level healthChecks (web `curl localhost:8000/health`, worker `celery inspect ping`)~~ ✅ **SHIPPED 2026-05-05 ~16:15 EDT** via worker rev 11 (`fceb7e9`) producer-heartbeat / mtime-probe design after rev 7→8→9→10 evolution. Backend container has ALB target group health (no separate container probe needed). Full forensic narrative: `docs/recaps/2026-05-05-commit-7-healthcheck-rev11.md`. |

### 3.2 Prod state at Phase 1 close
- Branch: `step-28-hardening`, HEAD `bd9446b`, tag `step-28-phase-1-complete`
- Backend service: `luciel-backend:17` (digest `sha256:39fecc49...95193`)
- Alembic head: `f392a842f885`
- memory_items.actor_user_id NOT NULL enforced
- Cascade-in-code verified end-to-end via prod smoke test on `step28-smoke-cascade-372779`
- Pillar 18 (tenant cascade end-to-end) green on dev
- Dev verification at Phase 1 close: 17/18 pillars green, Pillar 13 only red (A3 — sentinel-not-extractable, deferred to Phase 2)
- **Dev verification post-Phase-2-Commit-3:** 19/19 green (Pillar 13 A3 fixed + Pillar 19 audit-log mount included)

### 3.3 Phase 1 business impact
- **PIPEDA Principle 5 compliance** real in prod (cascade-in-code)
- **Audit log integrity** — append-only by database grant (worker can't UPDATE/DELETE pending Phase 2 role swap)
- **Atomic cascade** — no half-states on tenant deactivation
- **Brokerage DD audit story** defensible: "what data persists for ended tenancies?" answers cleanly
- **Tier 3 (REMAX) compliance baseline** — Pattern N/O/S/E discipline defensible to compliance officers
- **Stripe (Step 30a) precondition met** — programmatic cascade exists; subscription cancellations safe to wire when Step 30a ships

### 3.4 What Phase 1 did NOT deliver (intentional)
- No new product surface (no widget, no dashboard, no UI)
- No new revenue (zero paying customers — Phase 1 is pre-revenue hardening)
- REMAX trial still blocked on Step 30b (chat widget)
- Worker still runs as `luciel_admin` superuser (Phase 2 swap pending)

---

## Section 4 — What's next

### 4.1 Step 28 Phase 2 — Operational hardening (in progress)

**Code-only portion: SHIPPED on `step-28-hardening-impl`.** See §3.1b.
- ~~Pillar 13 A3 fix~~ ✅ Commit 3 `56bdab8`
- ~~Audit-log API mount~~ ✅ Commit 2 `75f6015` + Commit 2b `bfa2591` review fixes
- ~~Batched retention~~ ✅ Commit 8 `0d75dfe`
- ~~Phase 2 deploy runbook + recap update~~ ✅ Commit 9 (this commit)

**Prod-touching portion (REVISED 2026-05-05 ~16:15 EDT):**
1. ~~**Commit 4 — Worker DB role swap**~~ ✅ **SHIPPED 2026-05-05 14:51:27 UTC.** Mint via Pattern N (`luciel-mint:4` Fargate task) wrote SSM `/luciel/production/worker_database_url` v1 → rotated to v3 post-leak. Worker rev 6 deployed; Celery boots, RDS auth GREEN, Pattern E redaction held in CloudWatch.
2. **Commit 5 — CloudWatch alarms** — **IN PROGRESS 2026-05-05 ~16:15 EDT.** 6 alarms revised from the original 5: (a) `luciel-worker-no-heartbeat` — Log MetricFilter on `/ecs/luciel-worker` for `healthcheck heartbeat: touched` <1 occurrence in 90s over 2 periods (the new heartbeat from Commit 7 rev 11 makes this the strongest worker-liveness signal); (b) `luciel-worker-unhealthy-task-count` — RunningTaskCount <1 for 2×1min; (c) `luciel-worker-error-log-rate` — Log MetricFilter for `ERROR` >5 in 5min over 2 periods; (d) `luciel-rds-connection-count` — DatabaseConnections >80% max for 5min; (e) `luciel-rds-cpu` — CPUUtilization >85% for 10min; (f) `luciel-ssm-getparameter-failures` — Log MetricFilter for SSM access denials >0 in 5min. SNS topic: reuse existing `luciel-*` topic if one exists in account, else create `luciel-prod-alerts` and subscribe `aryans.www@gmail.com`. CFN stack at `cfn/luciel-prod-alarms.yaml`.
3. **Commit 6 — ECS auto-scaling** — to be authored. Application Auto Scaling target tracking on worker service (signal TBD: SQS depth via custom metric or CPU). Backend service has ALB target group health and is not currently CPU-bound; revisit only if pre-revenue load profile changes.
4. ~~**Commit 7 — Container healthChecks**~~ ✅ **SHIPPED 2026-05-05 ~16:15 EDT** via rev 11 producer-heartbeat / mtime-probe design (after rev 7→8→9→10 evolution; see §3.1b commits `837da98` through `fceb7e9` and `docs/recaps/2026-05-05-commit-7-healthcheck-rev11.md` for the four-iteration learning record).

**Phase 2 full close gate:** `python -m app.verification` 19/19 green against prod, all 5 alarms `OK`, both auto-scaling targets registered, both services on healthCheck-enabled task-def revisions, `pg_stat_activity` shows zero worker connections as `luciel_admin`, **AND** the following P3 prerequisites for Commit 4 are satisfied:
- **MFA enforced on `luciel-admin`** — `aws iam list-mfa-devices --user-name luciel-admin` returns a non-empty `MFADevices` array (P3-J resolved). ✅ **Verified 2026-05-03 23:48:11 UTC** — `SerialNumber: arn:aws:iam::729005488042:mfa/Luciel-MFA`. Account-wide sweep (`aws iam list-users`) confirmed `luciel-admin` is the only IAM user, so privileged-human MFA boundary is fully closed.
- **Dedicated `luciel-mint-operator-role` exists with MFA-required AssumeRole** — `aws iam get-role --role-name luciel-mint-operator-role` returns a trust policy with `Bool: aws:MultiFactorAuthPresent=true` and `NumericLessThan: aws:MultiFactorAuthAge=3600` (P3-K resolved). The migrate task role is NOT granted read on `/luciel/database-url`. ✅ **Verified 2026-05-04 00:14:10 UTC** (CreateDate). Trust policy, inline permission policy `luciel-mint-operator-permissions`, and `MaxSessionDuration: 3600` all match `infra/iam/*.json` design byte-for-byte. Smoke test (`mint-with-assumed-role.ps1 -DryRun`) succeeded at 2026-05-04 00:19:22 UTC; `aws ssm get-parameter --name /luciel/production/worker_database_url` returned `ParameterNotFound` post-smoke-test, confirming the dry-run wrote nothing.
- **Migrate-role policy diff applied** — `aws iam get-role-policy --role-name luciel-ecs-migrate-role --policy-name luciel-migrate-ssm-write` returns 6 SSM actions including `ssm:GetParameterHistory` (P3-G resolved). ✅ **Verified 2026-05-03 evening.** Live policy matches `infra/iam/luciel-migrate-ssm-write-after-p3-g.json` byte-for-byte.
- **Leaked admin password rotated and leaking log stream deleted** — `aws logs filter-log-events --log-group-name /ecs/luciel-backend --filter-pattern '"LucielDB2026Secure"'` returns zero events (P3-H resolved). ✅ **Verified 2026-05-03 23:56:22 UTC.** RDS rotation 23:18:31 UTC; SSM `/luciel/database-url` v1→v2 at 23:22:54 UTC; §4 SQLAlchemy ECS verification `P3H_VERIFY_OK select=1 user=luciel_admin db=luciel` at 23:31:53 UTC; contaminated stream `migrate/luciel-backend/d6c927a05eb943b5b343ca1ddef0311c` deleted at 23:52:16 UTC; §7 final sweep returned 0 hits across `/ecs/luciel-backend`, `/ecs/luciel-worker`. Residual SSM history v1 plaintext tracked as P3-L (P2, deferred post-Commit-4).

When met, tag `step-28-phase-2-complete`.

Estimated (REVISED 2026-05-03 evening): 4 prod-touching commits + 3 P3 prerequisites (J, K, H), ~5–7 hours total wall-clock across 2–3 sessions for hands-on execution. Runs in parallel with Steps 29/30.

### 4.2 Step 28 Phase 3 — Hygiene + compliance hardening

**Authoritative tracker:** `docs/PHASE_3_COMPLIANCE_BACKLOG.md` (commit `31e2b16` + 2026-05-03 evening rescope). Items below are a flat snapshot; the backlog file is the canonical priority and sequencing source.

- ~~**P3-J (P0, NEW 2026-05-03)**~~ — ✅ **RESOLVED** 2026-05-03 23:48 UTC. MFA on `luciel-admin` (`Luciel-MFA`). Account has only one IAM user; privileged-human MFA boundary is fully closed.
- ~~**P3-K (P1, NEW 2026-05-03)**~~ — ✅ **RESOLVED** 2026-05-04 00:14 UTC (role created + permission policy + smoke-test verified). `luciel-mint-operator-role` live with MFA-required AssumeRole, scoped read on `/luciel/database-url` + KMS Decrypt via SSM. Helper `scripts/mint-with-assumed-role.ps1` shipped in commit `9e48098`; mint script `--admin-db-url-stdin` flag in `ce66d06`.
- ~~**P3-G (P2, RESCOPED 2026-05-03)**~~ — ✅ **RESOLVED** 2026-05-03 ~20:09 EDT. `ssm:GetParameterHistory` added to `luciel-migrate-ssm-write`; live policy matches design byte-for-byte.
- ~~**P3-H (P1, RESOLVED 2026-05-03)**~~ — ✅ **RESOLVED** 2026-05-03 23:56:22 UTC. RDS master pw rotated, SSM v1→v2, §4 ECS SQLAlchemy verification passed, contaminated CloudWatch stream deleted, residual sweep clean. Full timeline + audit metadata in `PHASE_3_COMPLIANCE_BACKLOG.md` P3-H section.
- **P3-L (P2, NEW 2026-05-03, DEFERRED)** — SSM parameter `/luciel/database-url` history v1 retains plaintext `LucielDB2026Secure` after the P3-H rotation. Only `luciel-admin` (MFA-gated per P3-J) can read parameter history. Mitigation: delete-and-recreate the SSM parameter post-Commit-4. See `PHASE_3_COMPLIANCE_BACKLOG.md` P3-L for full rationale and fix shape.
- Dedicated read-only recon role
- Pattern O helper script extraction (`scripts/run_prod_recon.ps1`)
- LucielInstanceRepo `_for_agent`/`_for_domain` cascade autocommit-aware
- RESOURCE_KNOWLEDGE duplicate definition cleanup
- Memory admin endpoints test coverage (dedicated pillar)
- CloudWatch retention policies (365-day cap)
- Mint script accepts SQLAlchemy dialect prefix

### 4.3 Step 28 Phase 4 — Cosmetic (single-sweep candidate)
- `.gitignore` dedup
- Markdown re-fencing in runbooks
- UTF-8 display-name cleanup
- JMESPath label fixes
- PowerShell quoting drift codifications

### 4.4 Post-Phase-1 roadmap (Steps 29–38)

**Step 29 — Automated test suite** (1–2 sessions)
Convert `app.verification` to pytest. CI gate on every push.

**Step 30a — Stripe billing** (2–3 sessions, candidate)
Subscription tiers, signature-validated webhooks, `subscription.deleted` → cascade, tier-gated flags.

**Step 30b — Embeddable chat widget** (3–5 sessions)
**THE highest-leverage commit on the roadmap.** REMAX trial unblock.

**Step 31 — Hierarchical tenant dashboards** (after Step 34)
Three-tier hierarchical business-value attribution.

**Step 32 — Agent self-service**
Sarah spins up her own LucielInstances under her agent scope.

**Step 32a — File input UX**
Drag-drop knowledge ingestion. May merge with Step 32.

**Step 33 — Evaluation framework**
Automated scoring. Decision gate before Step 36 Council.

**Step 33b — Dedicated tenant infrastructure (Tier B)**
Per-customer AWS account. Build only when prospect demands.

**Step 34 — Workflow actions**
External integrations: Calendly, Gmail, HubSpot, listing DB. Step 31 dashboards depend on this.

**Step 34a — Channel adapter framework** (candidate)
SMS, email, phone. Channels emergent from config, not a column.

**Step 35 — Multi-vertical expansion framework**
Repeatable playbook for legal/mortgage/engineering.

**Step 36 — Luciel Council** (after Step 33 eval gate)
Multi-Luciel orchestration within scope. Orchestrator + tool-call ScopePolicy + council_id resolution.

**Step 37 — Hybrid retrieval** (after Step 35)
Vector + graph. CTEs first, opt-in via `DomainConfig.entity_schema`, Neo4j/AGE at 100 tenants.

**Step 38 — Bottom-up expansion / tenant merge** (after Step 35)
Email-stable User identity. Tenant-merge endpoint + pro-rated billing credit.

---

## Section 5 — Future concepts / design surface

NOT on the numbered roadmap. Kept here so they don't get rediscovered as "new ideas":
- **Voice-first Luciel** — channel adapter beyond SMS/phone
- **Real-time co-pilot mode** — with-consent listening, real-time suggestions
- **Cross-tenant referral graph** — network effect at scale
- **Knowledge marketplace** — curated, operator-curated (NOT user-generated)
- **Compliance-as-code** — codified compliance contracts for regulated verticals
- **Anonymous benchmarking across tenants** — aggregated metrics
- **Tenant-side admin AI (meta-Luciel)** — AI helping tenant admins manage own deployments

---

## Section 6 — Pricing strategy

### 6.1 Tier card (locked, see §0.2)

### 6.2 Per-tier characteristics

**Tier 1 Individual ($30–80/mo):**
- Customer IS the end-user
- Self-service onboarding
- Cancellation fully automatic via Stripe webhook
- Memory cascade automatic
- **Hard dependency: code-level cascade** (✅ Commit 12)
- Volume play: 60–170 customers to reach $5K MRR

**Tier 2 Team ($300–800/mo):**
- Team lead within brokerage
- Domain scope, multi-agent
- Operator-onboardable for first 5–10, self-service after Step 32
- **Hard dependency: scope-correctness enforcement** (Pillars 7, 8, 13)

**Tier 3 Company ($2,000+/mo):**
- Brokerage itself
- Whole-tenant scope
- Operator-onboarded indefinitely (high-touch)
- Audit-heavy brokerage DD
- **Hard dependency: Step 28 Phase 1 hardening** (✅ done)
- REMAX Crossroads = first warm lead

### 6.3 Unit economics intent
- Per-tenant gross margin target: 70%+ at scale
- Foundation model API costs: pass-through with margin OR included up to tier cap
- AWS infra: small per-tenant once N>10
- Founder time: 100% Aryan; scales with vertical expansion, not tenant count

### 6.4 Churn target
- Individual + Team: 5%/year
- Company: 2%/year
- Sticky due to: accumulated KB + cascade-correct departure + integration depth

---

## Section 7 — Moat (priority order)

1. **Integration depth per vertical** — 6 months of ingested knowledge + workflows wired into CRM/calendar = high switching cost
2. **Audit posture** — brokerage-DD-defensible operator discipline (this commit's tag = real proof)
3. **Scope hierarchy correctness** — competitors usually treat AI memory as flat; Luciel's `tenant→domain→agent→instance` maps to real org structure
4. **Cascade-correct departure semantics** — when agent leaves or tenant cancels, access deactivates correctly without manual intervention

---

## Section 8 — Five willingness-to-pay drivers

Brokerages don't pay for "we have an LLM wrapper." They pay for:
1. **Maintainability** — codify, don't tribal-knowledge
2. **Scalability** — per-tenant operations generalize
3. **Reliability** — idempotency, no half-states
4. **Security** — Pattern E discipline, no keys leak
5. **Traceability** — three-channel audit per mutation

Every Step 28 Phase 1 commit maps to one of these pillars.

---

## Section 9 — Go-to-market phases

**Phase 1 (current) — REMAX Crossroads warm trial**
1-on-1 outreach via Markham real-estate connections. Free trial in exchange for case study + audit-log demo rights. **Gated by Step 30b (chat widget).**

**Phase 2 — REMAX referral expansion**
Crossroads as reference customer. ~50 brokerages within 30km of Markham. Tier 3 contracts, 6-month minimum.

**Phase 3 — Multi-brand expansion**
Royal LePage, Coldwell Banker, Century 21. Same wedge, next brand.

**Phase 4 — Adjacent verticals**
Insurance, legal, mortgage. (Vertical-Q1 strategic question — which first.)

---

## Section 10 — Revenue milestones

- **Milestone 1 — First paying tenant:** REMAX Crossroads, Tier 3 ($2,000/mo). Gated by Step 30b. ETA Q3 2026.
- **Milestone 2 — $5K MRR:** Achievable shapes:
  - 1 Company + 5 Team + 20 Individual = $5.5K (realistic mix, ✅ Commit 12 dependency met)
  - 60–170 Individual @ $30–80 (volume play, requires Commit 12)
  - 2–3 Company @ $2K (DD-heavy)
- **Milestone 3 — $10K MRR:** ~5 Tier 3, Q1 2027
- **Milestone 4 — $100K MRR:** 50 Tier 3 + 100 Tier 2, Q4 2027
- **Milestone 5 — $1M ARR:** Mixed tiers + Tier 4, Q4 2028

**Critical-path:** First revenue does NOT require Commit 12 (REMAX is Tier 3). Milestone 2 DOES require Commit 12. Both shipped today.

---

## Section 11 — Compliance posture

### 11.1 Defensible NOW
- PIPEDA Principle 5 (limit retention) via cascade-in-code
- PIPEDA Principle 1 (accountability) via AuditContext + worker audit attribution
- Atomic transactions on tenant deactivation
- Operator pattern discipline (Pattern E/N/O/S codified)

### 11.2 NOT defensible yet
- GDPR Article 17 right-to-deletion (per-end-user, future)
- GDPR Article 20 data portability export (future)
- SOC2 / HIPAA (would need security audit + Tier B)
- Encryption-at-rest documentation (RDS encrypts; DD packet doesn't yet codify)
- Hard-purge timing SLA (soft-deleted rows persist; future retention worker)
- **MFA on privileged human identities** — `luciel-admin` IAM user has `MFADevices: []` as of 2026-05-03 evening. P3-J fixes; brokerage DD will fail this check until resolved.
- **Separation-of-duties on operator IAM roles** — `luciel-ecs-migrate-role` is currently used for both Alembic migrations and password mint. P3-K splits mint into a dedicated MFA-required `luciel-mint-operator-role`. Until then the blast radius of a compromised migrate task role includes admin-DSN read.
- **Audit-emission gaps for IAM-side privileged actions** — AssumeRole calls into the future `luciel-mint-operator-role` will land in CloudTrail, but Luciel's `admin_audit_logs` does not yet ingest CloudTrail. Considered acceptable for Phase 2, but explicit gap for Tier B / SOC2 readiness.
- **Plaintext credential rotation hygiene** — leaked `luciel_admin` password (`LucielDB2026Secure`) sits in CloudWatch log group `/ecs/luciel-backend` stream `migrate/luciel-backend/d6c927a05eb943b5b343ca1ddef0311c` until P3-H rotates and deletes.

### 11.3 Brokerage DD answer template
"When a brokerage cancels their subscription, every memory data point, every API key, every agent persona, every domain, every Luciel instance flips to inactive in a single atomic transaction. Audit logs in admin_audit_logs show exactly what was deactivated, when, by whom, and what cascade reason. Soft-deleted rows scheduled for hard-purge within [N days] (future retention worker)."

---

## Section 12 — Working memory anchors (drift recovery)

If conversation context is lost, these are the most important facts to preserve:

1. **Pricing is scope-aligned:** Individual=agent, Team=domain, Company=tenant. Architecture diagram = price card.
2. **Commit 12 (`f9f6f79`) unblocks the Individual tier** — without code-level cascade, $30/mo tier cannot ship.
3. **REMAX Crossroads is Tier 3 / $2K/mo**, gated by Step 30b widget, NOT by Commit 12.
4. **Step 30b is the highest-leverage commit on the roadmap** — REMAX trial unblock.
5. **Step 28 has 4 phases** (security/compliance, observability, hygiene, cosmetic). Phase 1 complete at `bd9446b`.
6. **Pillar 13 A3 fixed by Phase 2 Commit 3** (`56bdab8`) — was a test-design issue (sentinel-not-extractable), never a security gap. Dev now 19/19 green.
7. **Worker DB role swap is Phase 2 Commit 4** — packaged as runbook (`docs/runbooks/step-28-phase-2-deploy.md` §4), but the first mint attempt on 2026-05-03 leaked the admin DSN to CloudWatch. Re-attempt is BLOCKED on three P3 prerequisites: P3-J (MFA on `luciel-admin`), P3-K (dedicated `luciel-mint-operator-role` with MFA-required AssumeRole; migrate task role does NOT get admin-DSN read), and P3-H (rotate leaked `LucielDB2026Secure` + delete leaking log stream). Option 3 architecture is locked: human operator assumes the mint role via `aws sts assume-role --serial-number ... --token-code ...`, runs mint via `scripts/mint-with-assumed-role.ps1`, then the assumed credentials expire in ≤1 hour. Worker still runs as `luciel_admin` until all three resolve.
8. **Five willingness-to-pay drivers:** maintainability, scalability, reliability, security, traceability.
9. **Three deliberate exclusions:** no mobile, no marketplace, no model training. Adding any requires roadmap conversation.
10. **Operator patterns codified:** E (secrets), N (migrations), O (recon), S (cleanup, now backup). Runbooks at `docs/runbooks/`.
11. **Locked strategic-question answers:** Q1 ✅, Q2 → Step 31, Q3 → Step 37, Q4 → Step 36, Q5 → Step 38, Q6 ✅, Q7 → Step 34a, Q8 → Step 24.5c.
12. **This recap is source of truth** — if a chat recap contradicts it, this document wins. Update via PR with rationale.

---

## Section 13 — Resumption protocol

**Every new session begins with this 4-step ritual. No work proposed before completing it.**

### Step 1: Read this canonical recap
Get-Content docs/CANONICAL_RECAP.md

Read the full file. Do not infer from memory; re-read.

### Step 2: Read git state
git log -1 --format=fuller HEAD
git log --oneline -10
git status --short
git stash list


### Step 3: Run 5-block pre-flight (before any prod-touching work)

Block 1 — AWS identity (expect 729005488042):
`aws sts get-caller-identity --query Account --output text`

Block 2 — Git state (expect clean working tree):
`git status --short; git log -1 --oneline; git stash list`

Block 3 — Docker:
`docker info --format "{{.ServerVersion}} {{.OperatingSystem}}"`

Block 4 — Dev admin key (expect True / 50):
`$env:LUCIEL_PLATFORM_ADMIN_KEY.StartsWith("luc_sk_"); $env:LUCIEL_PLATFORM_ADMIN_KEY.Length`

Block 5 — Verification (expect 19/19 post-Phase-2-Commit-3):
`python -m app.verification`

If Block 5 returns anything other than 19/19 green, **diagnosis is the only acceptable next action.** Do not proceed to prod work on a red dev. (Pre-Phase-2 baseline was 17/18 with Pillar 13 A3 red — superseded by Commit 3 `56bdab8`.)

### Step 4: State back to user, in 5 lines
- Where we are (HEAD, phase, milestone)
- What's locked
- What's open
- What session-specific delta exists
- What we're about to do

Only after Step 4, propose work.

---

## Section 14 — Discipline reminders

- **Don't trust narrative recap over commit message** — `git log -1 --format=fuller HEAD` is canonical for code; this document is canonical for strategy
- **Use openapi.json as first source of truth** when prod exposes it
- **Pre-mutation recon is cheap** — $0.0007 per Pattern O query
- **Dry-run before real-run** — walker `-DryRun`, migration dev test, mint `--dry-run`
- **Idempotency** — re-run after mutation proves end-state, costs nothing
- **Three independent audit channels** for every mutation
- **Don't surgically regex-patch tool scripts** — rewrite full file
- **PowerShell quoting is fragile** — file-arg JSON for AWS CLI, never inline-Python via `python -c` from Windows shell
- **Stop sessions when verification goes red unexpectedly** — diagnose fresh, don't forge through
- **Trust-but-verify saves** — every code edit gets method-level / function-level existence check, not just import-success
- **Don't defer indefinitely** — push back on undefined "later"; Phase 2/3/4 IS scheduled, "later" without a phase is not
- **Don't substitute inference for memory** — re-read this recap and the prior commit when context gets long

---

## Section 15 — Drift register

### Phase 2 (operational hardening)
- **D-audit-log-router-not-mounted-pre-commit-9-2026-05-05** (NEW, code-only, no production-data impact) → ✅ **RESOLVED in-session via Commit 9 (`dddf8cb`)**. Pillar 19 was failing with HTTP 404 on `GET /api/v1/admin/audit-log` because the audit-log router module existed (`app/api/v1/admin_audit.py`) but was never imported and `include_router`'d into the `/api/v1/admin` prefix in `app/main.py`. The route therefore did not exist at runtime even though the code that should have served it shipped intact. Commit 9 added the missing import + include_router, which made the audit-log surface visible and tenant-scoped on the next deploy. **No production state mutated** by the fix itself; the route was simply absent before. Forward-looking guard: a smoke-style pillar should assert `OpenAPI` route presence for every admin module, not just the response-shape — this prevents "the code exists but isn't wired in" failures from hiding behind 404 noise.
- **D-uuid-not-json-serializable-via-jsonb-2026-05-05** (NEW, code-only, masked another failure) → ✅ **RESOLVED in-session via Commit 11 (`0cde3e7`)**. `app/repositories/agent_repository.py` was constructing `admin_audit_logs.context_payload` (JSONB column) with raw `uuid.UUID` values inside the dict. SQLAlchemy / psycopg JSON encoder rejects bare UUID with `TypeError: Object of type UUID is not JSON serializable`, so the audit-write half of agent.bind_user (and other agent-mutation paths) was raising mid-transaction. Critically, the TypeError was masking the underlying `permission denied for table scope_assignments` failure beneath — the audit-write happens AFTER the protected-table write, so the protected-table error never surfaced cleanly until the JSON serialization was fixed. Commit 11 wraps UUIDs as `str()` at the JSONB-payload boundary in `agent_repository`. Forward-looking guard: any code path that writes a `dict` into a JSONB column must coerce non-JSON-native types (UUID, Decimal, datetime, set) at the boundary; a repository-layer `_jsonable(value)` helper would prevent recurrence.
- **D-verify-harness-direct-db-writes-against-worker-dsn-2026-05-05** (NEW, code-only, surfaced production correctness boundary) → ✅ **RESOLVED in-session via Commits 12 + 13 + 14 (`99dfdc0` + `eaa80b5` + `42e95d1`)**. The verify harness historically opened `SessionLocal()` and ran direct `INSERT/UPDATE` SQL against `scope_assignments`, `users`, and `agents.user_id` to set up Pillar 12/13/14 fixtures. Migration `f392a842f885` (Step 28 Commit 4) intentionally created the `luciel_worker` Postgres role with ZERO grants on `scope_assignments` and `users` because the worker process never legitimately writes those tables — user/scope-assignment state changes flow through admin endpoints under the API process's `luciel_admin` DSN, never the worker DSN. The verify task runs as `luciel-verify` ECS task with the worker DSN (correct, by design), so direct-DB writes from the harness against those tables fail with `InsufficientPrivilege: permission denied`. Three architectural options were considered and the user explicitly green-lit Option A: (A) add admin HTTP routes for the operations the harness needs, migrate harness callsites to those routes — preserves the security boundary; (B) GRANT INSERT/UPDATE on `scope_assignments` to `luciel_worker` — dilutes the security contract, would have permanently widened production privilege scope to satisfy a test harness; (C) give the verify task the admin DSN — defeats the whole role-separation design that Commit 4 was built to enforce. Resolution: Commit 12 ships the five admin routes (suffixed `_p2c12` to avoid namespace collisions in long-form `admin.py`), Commit 13 migrates 8 harness callsites across P12/P13/P14, Commit 14 fixes two follow-on bugs in the migration (P12 promote-ordering + P14 query-string). **Production permission boundary unchanged — no GRANTs added to `luciel_worker`; the worker role remains zero-privileged on `scope_assignments` and `users` exactly as the migration intended.** Forward-looking guard: any new pillar must declare its DB-write needs up-front; if those writes fall outside the worker DSN's GRANT set, the pillar must use HTTP admin routes from the start (this is now an architectural rule, not a per-pillar judgment call).
- **D-verify-task-pure-http-2026-05-05** (NEW, code-only, broader debt) → 🟡 **DEFERRED to Step 29 with explicit follow-up scope**. The Commit 13 migration eliminated direct-DB *writes* from P12/P13/P14, but P12/P13/P14 still hold a `SessionLocal()` for read-only forensics queries against tables the worker DSN can SELECT (`api_keys`, `memory_items`, `messages`). Those reads work today and the suite is 19/19 green, but holding any DB session inside the verify task is debt: it couples the verify image to a DB connection pool, it adds another moving part to the verification graph, and it means future schema changes to tables like `memory_items` could break verify even when the user-facing API contract is unchanged. The honest long-term design is for the verify task to hold ZERO DB sessions and read all state via HTTP (the runbook section that authored this drift names it "Pattern N pure-HTTP verify"). Deferred to Step 29 because (a) the suite is green tonight without it, (b) authoring read-side admin endpoints for the forensics queries is a larger surface than tonight's mutation routes, and (c) the user's standing principle "no compromises in security and programmatic errors" is satisfied by today's commits since the actual *security* boundary (worker DSN cannot mutate protected tables) is intact — this remaining debt is a coupling concern, not a security concern. Step 29 entry point: enumerate the SELECT statements in `app/verification/tests/pillar_*.py`, design HTTP read endpoints for each, migrate, then drop `SessionLocal` from the verify image entirely.
- **D-call-helper-missing-params-kwarg-2026-05-05** (NEW, code-only) → 🟡 **DEFERRED to Step 29 with explicit follow-up scope**. `app/verification/http_client.py:call()` does not accept a `params=` kwarg — only `json/files/data` are forwarded to httpx. P14's first migration attempt passed `params={"audit_label": ...}` to satisfy the new admin route's `audit_label` Query parameter and crashed with `TypeError: call() got an unexpected keyword argument 'params'`. Commit 14 worked around it by inlining `?audit_label=...` into the path. The path-inlining is safe for the current callsite (label is a controlled f-string of alnum/colon/hyphen) but it is fragile: any future caller passing a label with `&`, `=`, `?`, or whitespace will silently corrupt the URL parse on the server side. Long-term fix: extend `call()` to accept `params=` and forward to httpx, then migrate the inlined query string back to a params dict. Deferred to Step 29 alongside `D-verify-task-pure-http` because both touch `http_client.py`. Forward-looking guard: any new admin route declared with `fastapi.Query(...)` parameters must have its harness caller reviewed against the `call()` signature — if `params=` is needed, fix `call()` first, never inline-encode unless the value space is provably safe.
- **D-luciel-instance-hard-delete-500-still-observed-2026-05-05** (REFERENCE, no new tracking needed) → 🟡 **EXISTING DRIFT, no scope change**. Tonight's verify run logged `luciel 42 DELETE -> 500` during teardown, same shape as `D-luciel-instance-admin-delete-returns-500-2026-05-04` (P3-Q in Phase 3 backlog). Pillar 10 still passes because it walks `state.tenant_id` only and tenant cascade soft-deactivates the `luciel_instance` row regardless of whether the explicit hard-DELETE succeeds. No new drift entry; the existing P3-Q is the right home. This note exists so a future reader scanning v2.1's drift list does not miss the cross-reference. Step 29 owns the actual investigation.
- **D-celery-fargate-hostname-mismatch-in-healthcheck-2026-05-05** (NEW, code-only, no production impact) → ✅ **RESOLVED in-session via rev 11**. Commit 7 rev 7 (`worker-td-rev7.json`, `837da98`) probed worker liveness via `celery -A app.worker.celery_app inspect ping -d celery@$HOSTNAME`. ECS reported task UNHEALTHY because Fargate's `$HOSTNAME` env var was set at task launch but the celery worker's identity (`celery@<hostname>`) is auto-derived inside the worker process via `socket.gethostname()` mid-init, after Fargate has already mutated the in-container `/etc/hostname` to a longer task-id-derived form. The `-d celery@$HOSTNAME` filter therefore looked up a name that the celery process itself had never registered. No production state mutated; one rev-cycle burned. Resolution path went through rev 8 (drop `-d` filter), rev 9 + 10 (extract probe to `scripts/healthcheck_worker.py`), and finally rev 11 (invert probe topology — producer touches `/tmp/celery_alive` from inside celery process, mtime probe checks freshness, no hostname assumption anywhere). Forward-looking guard: any container probe that depends on env-var-derived hostnames must verify the hostname matches the in-process derivation; prefer a probe that takes no host or topology assumptions.
- **D-celery-inspect-ping-unobservable-on-fargate-2026-05-05** (NEW, code-only, no production impact) → ✅ **RESOLVED in-session via rev 11**. Commit 7 rev 8 (`worker-td-rev8.json`, `27723b0`) dropped the `-d` filter to accept any celery responder. Probe was syntactically correct, but failed silently in production with no diagnostic signal in CloudWatch — `celery inspect ping`'s stdout went to Docker's per-container health buffer (visible only via `aws ecs describe-tasks ... healthStatus`), NOT to awslogs. Without log visibility the failure was indistinguishable from broker connectivity issues, hostname mismatches, or worker-process crash, blocking diagnosis. Resolution: rev 11 inverted the probe topology so the **producer** (the heartbeat thread inside the celery process) emits log lines to awslogs at INFO level, and the **probe** is a silent local-disk mtime check. Producer-side observability replaces probe-side observability. Forward-looking guard: any ECS healthcheck CMD must either log to stderr and have its log driver capture stderr, OR have an out-of-band observability anchor (in this case, the producer); never rely on CMD-SHELL stdout being routable.
- **D-healthcheck-cmdshell-output-not-in-awslogs-2026-05-05** (NEW, infra knowledge gap, no production impact) → ✅ **RESOLVED in-session via rev 11**. Generalized form of `D-celery-inspect-ping-unobservable-on-fargate-2026-05-05`. ECS task `containerDefinitions[].healthCheck.command` runs in a CMD-SHELL invocation whose stdout/stderr go to Docker's per-container health-check buffer, retained ~10 most-recent invocations and surfaced ONLY via `aws ecs describe-tasks --include CONTAINER_INSTANCE_HEALTH ...`, NOT via the task's awslogs configuration. The CMD-SHELL output is therefore unsearchable, ungreppable, and invisible to CloudWatch alarms. Documented in this drift register so any future container probe author knows up-front that CMD-SHELL output is opaque. Resolution: rev 11 design routes all liveness signals through the producer thread which logs at INFO via celery's own logger (which IS configured for awslogs), making `healthcheck heartbeat: touched` greppable, alarmable, and auditable in CloudWatch.
- **D-pip-entrypoint-argv0-is-python-not-script-name-2026-05-05** (NEW, code + image-build, no production impact) → ✅ **RESOLVED in-session via rev 11**. Commit 7 rev 9 (`bb6dd7a`) attempted to invoke the new healthcheck via `python /app/scripts/healthcheck_worker.py`. Failed because the `pip install -e .` step earlier in the image bake had set the script's entrypoint argv0 to `python`, not the script name — meaning `sys.argv[0]` inside the script returned `'python'` and downstream argparse / file-path logic that depended on `argv[0]` being the script name silently misbehaved. The bug was subtle because the script ran without crashing; it just didn't behave as written. Resolution: rev 11 healthcheck script no longer depends on `sys.argv[0]`, AND the producer-side heartbeat means script behavior is observable via the heartbeat log line rather than via probe exit-code interpretation. Forward-looking guard: never rely on `sys.argv[0]` for self-identification in scripts that may be wrapped by pip-installed entrypoints; use `__file__` or pass identity explicitly.
- **D-ecs-service-name-asymmetry-with-td-family-2026-05-05** (NEW, process-only, no production impact, DEFERRED to runbook clarification) → ✅ **RESOLVED in-session by explicit naming convention**. During Commit 7 deploy ceremonies, advisor and operator both at points conflated the ECS *service name* (`luciel-worker-service`) with the task-definition *family name* (`luciel-worker`). They are deliberately distinct: the service is what `aws ecs update-service` targets; the family is what `aws ecs register-task-definition` increments. Mistakes here can target the wrong thing (e.g. update a service that doesn't exist, or describe a task-def revision and assume it's running). Resolved by adding an explicit naming-convention callout to the runbook §7 (and forward to §5/§6): "the service name ALWAYS ends in `-service`; the task-def family is the service name minus `-service`; never abbreviate either at the API call site." Forward-looking guard: every AWS ECS write-side command in any runbook must use the fully-qualified name without abbreviation, and any new runbook section must be reviewed for the asymmetry.
- **D-cloudwatch-alarm-period-must-be-multiple-of-60-2026-05-05** (NEW, IaC-only, no production impact) → ✅ **RESOLVED in-session via `f49eae4`**. First Commit 5 stack deploy (`ce0e3a2`, `aws cloudformation deploy --stack-name luciel-prod-alarms`) failed because `WorkerNoHeartbeatAlarm` had `Period: 90`. CloudWatch's hard constraint is that alarm `Period` must be one of `10, 20, 30` or a multiple of `60` — `90` is not legal even though it sits between two legal values. The constraint is enforced server-side, not at template-validate time, so `aws cloudformation validate-template` passed cleanly and the failure surfaced only at create-stack. Two collateral alarms (`SsmAccessFailureAlarm`, `WorkerErrorLogRateAlarm`) entered `CREATE_FAILED` with reason "Resource creation cancelled" because the in-flight create aborted on the first hard error. Stack rolled back to `ROLLBACK_COMPLETE`. Resolution: reshape heartbeat alarm to `Period: 60, EvaluationPeriods: 5, DatapointsToAlarm: 4` (i.e. 4 of 5 consecutive 60s windows must miss — ~4-5min before page, tolerates one transient log-ingestion glitch). Producer still touches `/tmp/celery_alive` every 15s so each 60s window expects ~4 heartbeats; `Sum<1` boundary still cleanly identifies celery-process-death. **No production state mutated** (stack rollback is atomic). Forward-looking guards: (a) any future CloudFormation alarm template must run a Period audit before push (`grep 'Period:' file.yaml | awk '{print $2}'` and reject any value not in `{10,20,30}` or `n%60==0`); (b) `validate-template` is necessary but not sufficient — server-side resource-handler validation runs only at deploy time, so first-deploy failures are expected on novel resource types and the runbook should not interpret `validate-template` PASS as "deploy will succeed"; (c) when re-attempting a failed CFN deploy, always check stack status first — a `ROLLBACK_COMPLETE` stack must be deleted before recreating with the same name.
- **D-cfn-description-1024-char-limit-2026-05-05** (NEW, IaC-only, no production impact) → ✅ **RESOLVED in-session via `69d1a3a`**. First Commit 6 stack deploy (`e7b5f95`, `aws cloudformation deploy --stack-name luciel-prod-worker-autoscaling`) was rejected at the `CreateChangeSet` boundary with `Template format error: 'Description' length is greater than 1024`. The verbose `Description:` block authored on the template totaled 1522 characters (capacity bounds rationale + cooldowns rationale + reversibility note). CFN enforces a hard 1024-char cap on the `Description` field and rejects the template before any resource is created. Sister to `D-cloudwatch-alarm-period-must-be-multiple-of-60-2026-05-05` — both are server-side CFN constraints not caught by `aws cloudformation validate-template`. Resolution: collapse `Description` to a single-line summary (~184 chars) and move the full rationale into the commit message + this recap entry, where audit value is preserved without bloating the template. **No stack created** (template format error fires before changeset creation, so unlike the Period bug there was no `ROLLBACK_COMPLETE` cleanup needed). Forward-looking guards: (a) any future CFN template must run a length audit on `Description:` before push (`awk '/^Description:/{print length($0)-13; exit}' file.yaml`); (b) prefer one-line `Description:` values; route narrative content to commit messages, recaps, and runbooks; (c) reinforces the broader lesson that `validate-template` PASS is necessary but not sufficient — server-side validation runs at deploy time and catches at least three categories not covered by the template parser: numeric ranges (Period), string lengths (Description), and resource-handler-specific constraints.
- **D-celery-broker-not-verified-deferring-backlog-autoscaling-2026-05-05** (NEW, process-only, scoped DEFERRAL with explicit follow-up) → 🟡 **DEFERRED in-session by design choice**. Commit 6 originally scoped two scaling policies on the worker service: a CPU TargetTracking policy AND a backlog-per-worker policy. The backlog policy requires knowing the celery broker — if SQS, the policy uses `ApproximateNumberOfMessagesVisible / RunningTaskCount`; if Redis, the policy needs a custom-metric publisher emitting `LLEN celery` to CloudWatch. The recap mentions "Redis caching" but does not assert which broker celery uses, and authoring a backlog policy against the wrong broker would be a programmatic error against the user's standing principle ("no compromises in security and programmatic errors"). Resolution: ship CPU-only autoscaling now (broker-agnostic baseline), defer backlog policy to a follow-up commit gated on (1) explicit broker verification via `app/worker/celery_app.py` source review or `aws ssm get-parameter`/`aws elasticache describe-cache-clusters` and (2) appropriate broker-specific policy design. Capacity ceiling MaxCapacity=4 + `luciel-rds-connection-count` alarm at 90 connections provide the safety floor in the interim. **No production impact** — autoscaling is operating correctly on CPU; this drift records the deliberate scope cut, not a defect. Forward-looking guard: the next Phase 3 backlog grooming pass should add an item "P3-T: verify celery broker and add backlog-per-worker autoscaling policy" with the broker-verification command sequence as the entry point.
- **D-operator-pull-skipped-before-write-side-aws-ops-2026-05-05** (NEW, process-only, no production impact) → ✅ **RESOLVED in-session by ritual update**. During the Commit 7 rev 7→11 evolution, advisor pushed multiple commits to `step-28-hardening-impl` and asked operator to apply task-defs via `file://worker-td-revN.json`. On at least one cycle, operator applied the task-def from a stale local file because they had not run `git pull origin step-28-hardening-impl` after advisor's most recent push — the apply silently used an older revision-N JSON than the one advisor had just committed. The mismatch surfaced when the deployed service did not match the SHA advisor expected. **Sister drift to** `D-stale-remote-tracking-ref-after-advisor-push-2026-05-05` (the IAM equivalent from earlier in the day). Resolution: advisor now ALWAYS tells operator to `git pull origin step-28-hardening-impl` (not just `git status`) before any AWS write-side call referencing local `file://...`. Made an invariant in the session-summary protocol. Forward-looking guard: the runbook's standing pre-flight ritual now requires both `git fetch && git pull` AND a SHA cross-check (`git rev-parse HEAD`) against the SHA advisor expects before any `aws iam put-role-policy`, `aws ecs register-task-definition`, or `aws ecs update-service` call.
- Worker DB role swap (former Commit 13 work) — packaged as Commit 4, runbook §4 — **UNBLOCKED 2026-05-03 23:56 UTC** (P3-J + P3-K + P3-G + P3-H all resolved). **Pattern N dry-run GREEN 2026-05-05 14:16 UTC** (Fargate task `33908e96941d4dbda45594f241565c3b` on `luciel-mint:2`, exitCode 0, log message `(pre-flight SSM-writable + DB connect-only PASSED)`; SSM target `/luciel/production/worker_database_url` still `ParameterNotFound` post-dry-run, confirming zero mutation). Real-run gated behind fresh `confirm_action` + new MFA TOTP.
- ~~D-prod-superuser-password-leaked-to-terminal-2026-05-03~~ — ✅ **RESOLVED** 2026-05-03 23:56:22 UTC via P3-H. RDS master pw rotated 23:18:31 UTC; SSM `/luciel/database-url` v1→v2 at 23:22:54 UTC; §4 SQLAlchemy ECS verify passed at 23:31:53 UTC; contaminated stream `migrate/luciel-backend/d6c927a05eb943b5b343ca1ddef0311c` deleted at 23:52:16 UTC; §7 final sweep 0 hits. See `docs/runbooks/step-28-p3-h-rotate-and-purge.md` (executed end-to-end with three inline runtime corrections); full audit metadata in `PHASE_3_COMPLIANCE_BACKLOG.md` P3-H.
- **D-ssm-parameter-history-retains-plaintext-2026-05-03** (NEW) — SSM `/luciel/database-url` history v1 still contains the rotated-out plaintext password. Tracked as P3-L (P2, deferred to post-Commit-4 cleanup). MFA-gated `luciel-admin` is the only principal that can read parameter history; mint-operator-role and task roles have no `GetParameterHistory` access.
- **D-mint-script-leaks-admin-dsn-via-error-body-2026-05-03** (NEW) — original mint script logged the constructed admin DSN on dry-run error path. Hardened by `2b5ff32`; full incident report at `docs/recaps/2026-05-03-mint-incident.md`. Resolved at code level; operator-side rotation is P3-H.
- **D-luciel-admin-no-mfa-2026-05-03** (NEW) — `aws iam list-mfa-devices --user-name luciel-admin` returns empty. Tracked as P3-J. ✅ **RESOLVED 2026-05-03 23:48:11 UTC** — virtual MFA `Luciel-MFA` enabled. Account-wide sweep confirms `luciel-admin` is the only IAM user; full privileged-human MFA boundary is closed.
- **D-migrate-role-conflated-with-mint-duty-2026-05-03** (NEW) — single `luciel-ecs-migrate-role` covers both Alembic migrations and mint operations. Splitting into dedicated `luciel-mint-operator-role` is P3-K.
- **D-canonical-recap-misdiagnosed-migrate-role-policy-gap-2026-05-03** (NEW, self-referential) — prior session asserted migrate role was missing `ssm:GetParameter` + `ssm:PutParameter`. Real read of `luciel-migrate-ssm-write` shows both are present; only `ssm:GetParameterHistory` is missing. P3-G rescoped P1 → P2 in `31e2b16` follow-up edit (2026-05-03 evening).
- **D-pillar-13-a3-real-root-cause-2026-05-04** (NEW) — auth middleware `app/middleware/auth.py:124` had `user_id = agent.user_id` (typo — never-read local) instead of `actor_user_id = agent.user_id`, leaving `request.state.actor_user_id = None`. Failure chain: chat turn passes `actor_user_id=None` to `MemoryService.extract_and_save` → INSERT violates Postgres D11 NOT NULL → IntegrityError swallowed by `except Exception` at `extract_and_save:116-119` (logs only `type(exc).__name__`, not `repr(exc)`) → chat returns 200 with assistant reply "I'll remember that" while zero `MemoryItem` rows are written. ✅ **RESOLVED 2026-05-04 via `81b9e5a` (Commit A)**. Forensic narrative: `docs/recaps/2026-05-04-pillar-13-a3-real-root-cause.md`. Verification: `docs/verification-reports/step28_phase2_postA_sync_2026-05-04.json` (19/19 green). Compounding observability/hygiene gaps the bug exposed are tracked separately as P3-M / P3-N / P3-O / P3-P / P3-Q.
- **D-extractor-failure-observability-2026-05-04** (NEW) — `app/services/memory_service.py` `extract_and_save:116-119` swallows save-time exceptions with a `type-only` warning. Without `repr(exc)` the IntegrityError that drove the Pillar 13 A3 silent failure was undetectable in logs. Tracked as P3-O.
- **D-preflight-degraded-without-celery-2026-05-04** (NEW) — 5-block pre-flight passes when Celery is down because the sync fallback path in ChatService takes over. Recommend pre-flight gate fails fast if `celery -A app.celery_app inspect ping` returns no responders. Tracked as P3-N. Lifts D-celery-worker-not-running-locally-2026-05-02 from a process drift to an enforceable pre-flight check.
- **D-luciel-instance-admin-delete-returns-500-2026-05-04** (NEW) — anomaly observed during 19/19 verification teardown: `DELETE /api/v1/admin/luciel-instances/354` returned 500. Non-fatal (Pillar 10 still passed); investigation deferred to Phase 3. Tracked as P3-Q.
- **D-dev-key-storage-hygiene-2026-05-04** (NEW) — `LUCIEL_PLATFORM_ADMIN_KEY` stored in operator Notepad rather than a credential manager. Tracked as P3-P.
- **D-pg-client-tools-not-on-operator-path-2026-05-04** (NEW) — `psql` and `pg_dump` not on PowerShell PATH; surfaced repeatedly during diag work. Tracked as P3-M.
- D-celery-worker-not-running-locally-2026-05-02 (codify in operator-patterns.md or pre-flight check) — superseded by D-preflight-degraded-without-celery-2026-05-04 / P3-N
- D-pillar-10-suite-internal-only-2026-05-01 (deploy-time teardown contract)
- D-cloudwatch-no-retention-policy-2026-05-01 (365-day retention cap)
- D-recon-task-role-reuses-migrate-role-2026-05-01 (dedicated `luciel-ecs-recon-role`)
- D-pillar-13-creates-residue-on-failure-2026-05-01

### Phase 3 (hygiene)
- D-luciel-instance-repo-cascade-not-autocommit-aware-2026-05-02
- D-admin-audit-log-resource-knowledge-duplicate-definition-2026-05-02
- D-memory-admin-endpoints-untested-by-pillar-2026-05-02
- D-recap-recon-private-subnet-assumption-2026-05-02
- D-cleanup-via-migration-not-precondition-task-2026-05-02
- D-mint-worker-db-script-doesnt-strip-sqlalchemy-dialect-2026-05-03
- D-pattern-o-helper-script-2026-05-01 (extract worked PowerShell template)
- D-no-tenant-hard-delete-endpoint-2026-05-01
- D-delete-endpoints-are-soft-delete-2026-05-01 (misleading verb)
- D-emit-log-key-order-ps-version-dependent-2026-05-01
- D-walker-loses-delete-error-body-2026-05-01
- D-emdash-corrupted-in-display-names-2026-05-01
- D-recap-table-name-assumptions-2026-05-01
- D-recap-memory-items-scope-shape-2026-05-01
- D-recap-task-def-naming-without-colon-2026-05-01
- D-recap-requirements-txt-assumption-2026-05-01
- D-recap-conflated-total-vs-active-residue-2026-05-01
- D-recap-undercount-phase1-progress-2026-05-01

### Phase 4 (cosmetic, single-sweep candidate)
- D-runbook-code-fences-stripped-by-ps-heredoc-2026-05-01
- D-jmespath-dash-quoting-2026-05-01
- D-jmespath-sizemb-mislabel-2026-05-01
- D-ecr-describe-images-filter-quirk-2026-05-01
- D-powershell-aws-cli-json-arg-quoting-2026-05-01
- D-powershell-selectstring-simplematch-anchors-2026-05-01
- D-powershell-heredoc-angle-bracket-after-quote-2026-05-01
- D-powershell-question-mark-in-string-interpolation-2026-05-01
- D-double-8a-commits-2026-05-01

### Open as of 2026-05-05 ~10:35 EDT (P3-S Half 2 Step 5 first-attempt cleanup)
- **D-option-3-ceremony-cannot-reach-private-rds-from-laptop-2026-05-04** — the Option 3 mint ceremony as designed assumes the operator runs the ceremony from their laptop. That assumption is incompatible with the production VPC posture (RDS is in a private subnet with no public ingress, by design). The mint script aborted at `psycopg.connect(admin_dsn)` (line 554) with `ConnectionTimeout: connection timeout expired` on the first real-run attempt. Pre-flight + outer-try-except defenses in the script worked correctly; **zero production state was mutated**. The boundary was never exercised by any prior smoke test because `--dry-run` returns at line 491 before reaching the DB connect. Resolution requires re-architecting the ceremony to run inside the VPC — see `PHASE_3_COMPLIANCE_BACKLOG.md` P3-S (P0, blocks Phase 2 close). Recommended sub-option: P3-S.a (dedicated `luciel-mint` Fargate task with its own task role). Full session forensic narrative: `docs/recaps/2026-05-04-mint-architectural-boundary-pause.md`. Forward-looking guard: any future ceremony involving DB or other VPC-private-resource access must explicitly state where it executes (laptop / Fargate / bastion / VPN) at design time and validate that path end-to-end in smoke before claiming the design is proven.
- **D-mint-script-dry-run-skips-preflight-2026-05-04** — sister drift to the above. `mint_worker_db_password_ssm.py --dry-run` returns at line 491 before the pre-flight at line 497 and the DB connect at line 554. This means dry-run validates only the AssumeRole / SSM-read / stdin-pipe / password-gen path, NOT the IAM permissions the script needs for SSM write OR the network path the script needs to reach RDS. Both gaps were caught only at real-run time today, when each attempt mutated nothing but burned an MFA TOTP. Resolution: ~10-LOC patch that calls `preflight_ssm_writable` AND attempts a connection-only `psycopg.connect(...).close()` (no SQL) before the dry-run early-return. Out of scope for the session that found it; will be folded into P3-S work or done as a standalone hardening commit.
- **D-runbook-rotation-verify-wrong-subnets-2026-05-05** (NEW, process-only, no production impact) — During the 2026-05-05 morning rotation verification, advisor composed an `aws ecs run-task` command using subnet IDs pulled from the RDS `describe-db-instances` output (`subnet-0b315ad9ad4a8efb6`, `subnet-0cd66d8e9229aa122`). These are RDS DB subnets, intentionally locked-down with no SSM VPC endpoint or NAT egress. The migrate task failed with `ResourceInitializationError: unable to pull secrets ... context deadline exceeded`, which initially read as a production-affecting VPC networking issue. After checking VPC endpoints, advisor discovered the SSM/ssmmessages/ec2messages endpoints exist in the *application* subnets (`subnet-0e54df62d1a4463bc`, `subnet-0e95d953fd553cbd1`), not the RDS subnets. Root cause: advisor inferred subnet identity from RDS metadata instead of asking which subnets application tasks actually use. **No production impact** — real web/worker/migrate runs use the correct subnets via `luciel-backend-service` config. Only the verification ceremony was affected. Forward-looking guard: before launching ad-hoc Fargate tasks, advisor must verify subnet identity by checking `aws ecs describe-services` for an existing production service, not by inferring from RDS or other unrelated infrastructure. Process patch reflected in `docs/incidents/2026-05-05-admin-dsn-disclosed-in-chat.md` § "Sub-incident".
- **D-mint-operator-role-missing-ecs-runtask-2026-05-05** (NEW, IaC-only, no production impact) — Step 4 retry of P3-S Half 2 (after the `file://` JSON-passing fix unblocked AWS CLI parser) reached the AWS API for the first time and was correctly refused with `AccessDeniedException ... User: arn:aws:sts::729005488042:assumed-role/luciel-mint-operator-role/mint-fargate-20260505-094519 is not authorized to perform: ecs:RunTask on resource: arn:aws:ecs:ca-central-1:729005488042:task-definition/luciel-mint:1`. Root cause: `luciel-mint-operator-role`'s permission policy at `infra/iam/luciel-mint-operator-role-permission-policy.json` had 5 statements scoped only for the in-VPC mint script's runtime API calls (admin DSN read + worker SSM write + KMS). Pattern N variant moves the script execution from laptop → Fargate, so the operator role now also needs to *launch* the Fargate task itself: `ecs:RunTask` (run the task), `iam:PassRole` (attach task role + execution role to the container), `ecs:DescribeTasks` + `ecs:StopTask` (helper polls + Ctrl+C cleanup), `logs:GetLogEvents` + `logs:DescribeLogStreams` (helper tails CloudWatch). **No production state was mutated** — the API call was rejected at the IAM authorization boundary; AssumeRole credentials cleared cleanly; one MFA TOTP burned. **This is the second instance of the same anti-pattern as `D-p3-k-policy-missing-worker-ssm-write-2026-05-04`** — a role's policy scoped at design time without enumerating the role's actual end-to-end runtime API surface, with the gap caught only at real-run. Resolved by this commit: policy file now has 9 statements (5 original + 4 new); pre-image preserved at `infra/iam/luciel-mint-operator-role-permission-policy.json.pre-p3-s-half-2-step-4-2026-05-05`. New statements are tightly scoped: `LaunchMintFargateTask` conditioned on `ecs:cluster=luciel-cluster` AND limited to `task-definition/luciel-mint:*`; `PassMintTaskRoles` limited to exactly the two ARNs (`luciel-ecs-mint-role` + `luciel-ecs-execution-role`) and conditioned on `iam:PassedToService=ecs-tasks.amazonaws.com`; `DescribeMintTaskAndStopTask` limited to `task/luciel-cluster/*`; `ReadMintTaskLogs` limited to `/ecs/luciel-backend:log-stream:mint/*` (cannot read backend or worker streams). **Process patch (NEW):** any new IAM role created for a ceremony MUST have its policy reviewed against the *full* end-to-end ceremony script at the API-call level (every `aws.<service>.<verb>` invocation, every SDK call), not against the design-time mental model. The reviewer enumerates each verb; the policy author shows that verb is covered by exactly one Sid; gaps are surfaced before apply, not at first real-run. This patch is in addition to the May-04 patch ("any new role's policy must be reviewed against the actual ceremony script's runtime IAM calls") which proved insufficient because it didn't explicitly require enumerating the laptop-side caller's API surface as well as the in-container script's surface. The two patches together: enumerate **both** the operator's API surface AND the in-container script's API surface against the policy.
- **D-stale-image-digest-blocked-script-patch-from-reaching-container-2026-05-05** (NEW, infra-only, no production impact) — Step 4 first attempt on the new mint task definition `luciel-mint:1` launched cleanly, but the container exited with `argparse error: the following arguments are required: --admin-db-url, --worker-host, --worker-db-name`. Root cause: the mint script's argparse signature still required those three CLI flags, while the Pattern N task-def design passes them via env-var injection (admin DSN via the SSM `secrets:` block, worker host + DB name via plain `environment:` entries). The script had been patched to read env vars as an Option A simplification, but the patched version was never built into a container image — `mint-td-rev1.json` referenced an older image digest (`sha256:e3e75dd2c82ceea5c26ca1e6213ca88ad7ad62eedeee405d0a86662a8d7c3d0e`) baked before the Option-A patch landed. **No production state mutated** — script exited 2 inside the container before any DB connect or SSM write. Resolved by ECR rebuild + push of `luciel-backend:p3-s-half-2-2026-05-05` digest `sha256:3275322b7ef80a508680dced0ed4a142de77b0d3fd984318878bc57157ff7b95`, plus `mint-td-rev2.json` swapping the digest, plus `aws ecs register-task-definition` registering `luciel-mint:2` ACTIVE (rev 1 still ACTIVE for rollback). Step 4 retry on rev 2 launched `33908e96...` cleanly to exitCode 0 with the dry-run pre-flight PASSED. Forward-looking guard: image and repo MUST be in sync — any script change requires image rebuild before next ceremony, with the digest cross-checked between `aws ecr describe-images` and the task-def's `image:` field. The new docker-rebuild step is now an explicit gate ahead of any §4 retry where the script has been edited.
- **D-mint-helper-aws-stderr-causes-native-command-error-2026-05-05** (NEW, code-only, no production impact, DEFERRED) — `scripts/mint-via-fargate-task.ps1` line 394 calls `$logsJson = aws @logArgs 2>$null` to pull CloudWatch log events for the operator after the task reaches STOPPED. PowerShell's native-command stderr handling treats any non-empty stderr from `aws.exe` as a `RemoteException`, raising `NativeCommandError` even when the command succeeded and `2>$null` was supposed to silence it. The helper bails *after* the task has already run and exited cleanly, so ceremony correctness is unaffected — only the helper's auto-readback convenience is lost. The manual workaround is `aws logs get-log-events --log-group-name /ecs/luciel-backend --log-stream-name mint/luciel-backend/<task-id> --query 'events[*].message' --output json`, which we ran successfully on Step 4 dry-run. **Decision: deferred to a standalone polish commit AFTER Step 5 GREEN.** Rationale: fixing now requires another full ceremony cycle (edit script, rebuild image, register td-rev3, re-run Step 4 to validate the helper polish) for a cosmetic gain; the manual readback adds <60 seconds to each ceremony. Step 5 will hit the same bail point and use the same workaround.
- **D-test-coverage-assumed-not-proven-mint-script-env-only-path-2026-05-05** (NEW, process + test-coverage gap, no production impact) — When the Option A simplification (env-var resolution for `--admin-db-url`, `--worker-host`, `--worker-db-name`) was patched into `mint_worker_db_password_ssm.py`, a smoke test `T2` was authored to verify the env-only invocation path. **The test was contaminated**: it passed BOTH CLI flags AND env vars to the script, so the script's argparse layer was satisfied by the flags and the env-var resolution code path was never actually exercised. Test passed locally, was assumed sufficient, and the bug only surfaced when the container ran the script with env-vars-only (no CLI flags) — exactly the production code path. Resolution: the Step 4 GREEN retry from Fargate is itself the first end-to-end exercise of the env-only path. Forward-looking guard: any smoke test for an env-var-resolution code path must NOT also pass the equivalent CLI flags; the test must isolate the path under test. Add a checklist item to the operator-patterns runbook: "if a script reads from BOTH env vars and CLI flags, write at least one test that passes ONLY env vars and at least one that passes ONLY flags."
- **D-stale-remote-tracking-ref-after-advisor-push-2026-05-05** (NEW, process-only, no production impact) — During the IAM policy apply (Step 4 attempt 2), advisor pushed commit `7514b98` to origin and asked operator to `git pull`. Operator ran `git status` first and saw "Your branch is up to date with 'origin/step-28-hardening-impl'" — a stale remote-tracking ref because the local hadn't yet fetched. Without a fetch, `git status` reports against the last-known-remote, not the actual remote HEAD. Operator then applied the IAM policy from a stale local file (5 statements instead of 9), which silently "succeeded" but left the role under-permissioned for the helper's actual API surface. The error surfaced one step later when `aws ecs run-task` was rejected with `AccessDeniedException`. Resolved in-session by `git fetch && git pull` (fast-forwarded to `7514b98`), then re-applying the policy from the fresh 9-statement file. **No production state mutated** — IAM apply is idempotent; the second apply replaced the first. Forward-looking guard: after any advisor push, operator must `git fetch` before assuming `git status` is current. The runbook §4.0 pre-mint checklist now adds `git fetch && git status` as a mandatory ritual before any IAM or task-def apply.
- **D-iam-policy-review-relies-on-mental-model-2026-05-05** (NEW, process + tooling backlog, no production impact, DEFERRED to Phase 3) — This is the third instance of the same anti-pattern as `D-p3-k-policy-missing-worker-ssm-write-2026-05-04` and `D-mint-operator-role-missing-ecs-runtask-2026-05-05`: a role's permission policy is authored against a design-time mental model of "what this role does," without enumerating the role's actual end-to-end runtime API surface (every `aws.*` and `boto3.client.*.method()` call across every script and helper that runs under the role). Each prior instance was caught at runtime, after one MFA TOTP burn and one hands-on diagnostic loop. Tracked as a Phase 3 tooling task: build a static analyzer that walks the helper scripts + the script-under-helper + the SDKs they invoke, emits a deduplicated set of `(action, resource_pattern)` pairs, and diffs against the role's live policy. Estimated 1-2 sessions; lower priority than P3-S finish but high leverage for any future role we add to the system. **No production impact this session** — the gap was caught at the IAM authorization boundary in the first try.
- **D-proxy-vs-github-remote-not-explicitly-verified-2026-05-05** (NEW, process-only, no production impact, DEFERRED to Phase 3) — Advisor pushes via `git push` configured to use a proxy remote routed through the agent's own GitHub credential (commits authored as `Aryan Singh <aryans.www@gmail.com>` per agreed convention). Operator's local `git` is configured to push directly to the GitHub remote with their own credentials. Both remotes point to the same repository, but the explicit URL has never been verified at session start. Today's `D-stale-remote-tracking-ref-after-advisor-push-2026-05-05` was easy to diagnose because the divergence was 1-commit; if the two remotes ever drifted (e.g. operator force-pushed to a branch advisor was about to push to, or the proxy remote got stuck on an outdated refspec), the resulting confusion could be much harder to unwind. Forward-looking guard: at session start, advisor verifies `git remote -v` output matches the canonical GitHub repo URL, and operator confirms their `origin` points to the same place. Add to the resumption protocol § "Step 2: Read git state". Not a Phase 2 blocker.
- **D-mint-helper-passes-json-as-inline-arg-on-powershell-2026-05-05** (NEW, code-only, no production impact) — Step 4 of the P3-S Half 2 ceremony attempted the first dry-run via `scripts/mint-via-fargate-task.ps1` and failed at the `aws ecs run-task --network-configuration $networkConfigJson` invocation with `ParamValidation: Invalid JSON: Expecting property name enclosed in double quotes`. The error body showed AWS CLI received `{awsvpcConfiguration:{subnets:[subnet-0e54df...]}}` — every double quote stripped. Root cause: well-known PowerShell-on-Windows native-command argument bug. PowerShell's `ConvertTo-Json -Compress` produced valid JSON, but when passed as a string argument to `aws.exe`, the embedded double quotes were stripped by the native-command argument processor before reaching the AWS CLI parser, leaving bare-token JSON that fails `ParamValidation`. The misleading downstream `throw` message blamed IAM (`Confirm the assumed role has ecs:RunTask + iam:PassRole...`); IAM was not the real cause — the call never reached the AWS API. **No production impact** — the AssumeRole call had completed cleanly and the `run-task` was rejected client-side; AWS state unchanged; assumed credentials cleared. **No MFA TOTP burned uselessly** since the same TOTP could have been re-used inside its 30-second window had the operator known. Resolution applied in this commit: `scripts/mint-via-fargate-task.ps1` now writes `$networkConfigJson` and `$overridesJson` to two ASCII temp files and passes `file://<path>` to `aws ecs run-task`, with cleanup in a `try/finally` block. This matches the canonical pattern already established by `recon-network.json` (Pattern O reads, `step-28-prereq-data-pattern-o.md` § "Pre-launch check 4") and the `step-27c-worker-deploy.md` worker-service create call. Forward-looking guard: any PowerShell helper that passes JSON to a native CLI MUST use `file://` argument-passing rather than inline strings; design-time review of any new helper script must check this pattern explicitly. Patch is also a candidate for inclusion in `docs/runbooks/operator-patterns.md` Pattern N (Fargate one-shot) as a known PowerShell wart.

- ~~**D-mint-script-uses-pg-authid-not-readable-on-rds-2026-05-05**~~ (RESOLVED — see Resolved section below for resolution evidence; original entry preserved verbatim for audit trail) — Step 5 first real-run on `luciel-mint:2` (task `6dc293a3be784529948e7a7dc0e73091`) launched cleanly to ECS, the Pattern N container started, the mint script reached `verify_role_state()`, and the SQL `SELECT rolpassword FROM pg_authid WHERE rolname = 'luciel_worker'` was correctly refused by Postgres with `InsufficientPrivilege: permission denied for table pg_authid`. Root cause: `pg_authid` is the system catalog that stores actual hashed passwords; on AWS RDS, only a true Postgres superuser can SELECT from it, and the `luciel_admin` master role is `rds_superuser` (NOT a true superuser, by deliberate AWS RDS hardening). The script was authored against vanilla-Postgres assumptions; the failure mode never surfaced in any prior environment because the Pattern N ceremony itself is brand-new (`luciel-mint:2` was the first container that ever ran the script against real RDS for real-mint). **Zero production state was mutated** — the SELECT failed BEFORE `ALTER ROLE`; SSM `/luciel/production/worker_database_url` is still `ParameterNotFound`; `luciel_worker` password unchanged from its bootstrap value; one MFA TOTP burned. Resolution applied (uncommitted as of this entry; will land with the next push): (1) `verify_role_state(conn)` switched from `pg_authid` to `pg_roles` (the RDS-readable, sanitized view that exposes role existence + flags but masks `rolpassword` as `'********'`), preserving the role-existence check; (2) the rotation-vs-first-mint signal previously derived from `pg_authid.rolpassword IS NULL` is replaced with a stronger SSM-presence signal — new `verify_first_mint_or_force_rotate(*, region, ssm_path, force_rotate)` calls `ssm.get_parameter(/luciel/production/worker_database_url)`: `ParameterNotFound` ⇒ first mint, allow; parameter exists + `--force-rotate` ⇒ rotation, allow; parameter exists, no flag ⇒ refuse with `MintAlreadyDoneRequireForceRotate`. Acceptable trade-off documented in this entry: an out-of-band manual SQL `ALTER USER luciel_worker WITH PASSWORD '...'` would bypass the new check, but no approved workflow does that — the new guard is strictly stronger than the old one for any in-script flow. New 11-test smoke suite at `tests/test_mint_worker_db_password_ssm.py` covers (a) SQL targets `pg_roles` not `pg_authid` (regex assertion against the function source), (b) role-exists vs role-missing, (c) SSM-presence first-mint-allow / force-rotate-allow / no-flag-refuse / boto error pass-through, (d) env-only argv path uncontaminated by CLI flags (per `D-test-coverage-assumed-not-proven-…-2026-05-05` lesson). 11/11 GREEN locally. Forward-looking guard: any future script that touches Postgres on RDS must use RDS-compatible catalog views — `pg_roles` not `pg_authid`, `pg_stat_database` is fine, `pg_stat_replication` requires `rds_replication`, `pg_ls_logdir` requires `rds_superuser`. The runbook will gain a Pattern-N pre-flight checklist item: "every SQL statement in a Pattern N script must be cross-checked against the AWS RDS PostgreSQL feature matrix at design time."
- ~~**D-dry-run-validates-subset-of-real-run-pg-authid-not-exercised-2026-05-05**~~ (RESOLVED — see Resolved section below for resolution evidence; original entry preserved verbatim for audit trail) — Sister drift to `D-mint-script-uses-pg-authid-not-readable-on-rds-2026-05-05` and a direct continuation of the `D-mint-script-dry-run-skips-preflight-2026-05-04` family. The Step 4 dry-run on `luciel-mint:2` exercised the AssumeRole + SSM-read + `preflight_ssm_writable` + `psycopg.connect-only` paths and exited 0 cleanly — but it did NOT exercise `verify_role_state()` because dry-run returns at line 653 of `main()` before `verify_role_state(conn)` is called. The pg_authid privilege failure was therefore impossible to catch in dry-run, and surfaced only in the Step 5 real-run after one MFA TOTP burn. The 2026-05-04 patch added `preflight_ssm_writable` + `psycopg.connect-only` to the dry-run path, but did NOT extend the dry-run to also execute the pre-mutation SELECTs (the catalog reads inside `verify_role_state`). This is a recurring class of process gap: each mutation-stage gets a defense pre-flight retroactively after it bites, instead of dry-run being defined upfront as "every read-only step that real-run executes, in order, up to but not including any state-mutating call." Resolution applied (uncommitted as of this entry; lands with the same push as the pg_roles patch): the dry-run path will now also run `verify_role_state(conn)` (the SELECT) and `verify_first_mint_or_force_rotate(...)` (the SSM read) — both are read-only by construction. Forward-looking process patch (added to `process patches in force` list): "dry-run is defined as: every read-only step that real-run executes, in order, up to but not including any state-mutating call. New read-only steps added to real-run must be added to dry-run in the same commit." The runbook will gain a §4.0 checklist item: "before approving any new mint-script PR, diff the real-run path against the dry-run path and confirm every read-only call in real-run also appears in dry-run."

### Resolved by Phase 2 code-only portion
- **D-mint-script-uses-pg-authid-not-readable-on-rds-2026-05-05** (NEW, code-only, no production impact) → Commit `0cd87be` (advisor) + image rebuild + Commit `65f8996` (`mint-td-rev3.json`). `verify_role_state` switched to `pg_roles`, rotation-vs-first-mint signal moved to SSM-presence via new `verify_first_mint_or_force_rotate`. Resolution proven by Step 5 GREEN on `luciel-mint:3` (task `27638cebbd8349f8bcb8d70e4c55714b`, 2026-05-05 14:51:27 UTC, exitCode 0, ALTER ROLE + SSM v1 PutParameter both committed, Pattern E redaction held). Forward-looking guard locked in: any future RDS-touching script must use RDS-compatible catalog views (`pg_roles` not `pg_authid`).
- **D-dry-run-validates-subset-of-real-run-pg-authid-not-exercised-2026-05-05** (NEW, process drift, no production impact) → Commit `0cd87be`. Pre-flight Layer 2 was extended to also run `verify_role_state(_preflight_conn)` against the connection that is opened-then-closed in BOTH dry-run and real-run paths. Dry-run = real-run minus state-mutating calls is now a structural invariant in the script's `main()`. Resolution proven by Step 4 retry on rev 3 (task `ed5fd118ce024fa8b1e1cce15552eee3`, exitCode 0) emitting the new four-stage callout `(pre-flight SSM-writable + first-mint-or-force-rotate + DB connect + role-state PASSED)` in CloudWatch. Forward-looking guard: any new read-only step added to real-run must be added to dry-run in the same commit; the runbook's pre-mint checklist will be updated to require diffing real-run vs dry-run paths before any mint-script PR approval.
- D-pillar-13-a3-real-root-cause-2026-05-04 → Commit A (`81b9e5a`); P13_DIAG instrumentation cleaned up by Commit D (`13035da`); evidence archived at `docs/verification-reports/step28_phase2_postA_sync_2026-05-04.json` (19/19 green). Forensic narrative: `docs/recaps/2026-05-04-pillar-13-a3-real-root-cause.md`. Note: this supersedes `D-pillar-13-a3-sentinel-not-extractable-content-2026-05-02` in scope — the May-02 fix correctly hardened the sentinel-extractable contract for the message-text shape, but the deeper auth-binding bug remained latent until the May-04 diagnosis.
- **D-p3-k-policy-missing-worker-ssm-write-2026-05-04** (NEW) — the Option 3 mint ceremony's first real-run attempt at 2026-05-04 ~19:59 UTC was correctly refused by `mint_worker_db_password_ssm.py`'s `preflight_ssm_writable` check (script line 283, added in `2b5ff32` as the atomicity defense for `D-mint-script-leaks-admin-dsn-via-error-body-2026-05-03`). Root cause: P3-K's original permission policy at `infra/iam/luciel-mint-operator-role-permission-policy.json` had three statements scoped for admin-DSN read only — `ReadAdminDsnFromSsm`, `DescribeAdminDsnParameter`, `DecryptAdminDsnViaSsm` — but the Option 3 ceremony runs the entire mint script INSIDE the assumed role (helper passes assumed creds via env vars to the Python subprocess). The role therefore needs `ssm:GetParameter`, `ssm:GetParameterHistory`, `ssm:PutParameter` on `/luciel/production/worker_database_url`, plus `kms:Encrypt` and `kms:GenerateDataKey` via SSM for the SecureString write. **No production state was mutated** — the pre-flight fired before `connect_admin()` was called, no Postgres connection, no `ALTER USER`, no SSM write. One MFA TOTP was burned. Resolved by this commit: policy file now has 5 statements; pre-image preserved at `infra/iam/luciel-mint-operator-role-permission-policy.json.pre-p3-k-followup-2026-05-04`; runbook adds a new §4.0.5 with verify + apply commands; prerequisite gate table grows to 5 rows including the new P3-K-followup. Operator must apply via `aws iam put-role-policy` from the laptop using their own `luciel-admin` credentials before re-attempting §4.2 mint. Note the failure shape: this is exactly the kind of "designed before the workflow was final, never reconciled" gap that audit-frame development is supposed to catch — and it did, via the script's own pre-flight. Forward-looking guard: any future role created for a ceremony must have its policy reviewed against the actual ceremony script's runtime IAM calls, not against the design-time mental model. P3-K's `step-28-p3-k-execute.md` runbook should be updated post-success to reflect the 5-statement policy as the canonical state.
- **D-runbook-mint-missing-workerhost-arg-2026-05-04** — the v2 §4.2 rewrite committed at `374912a` showed the `mint-with-assumed-role.ps1` invocation without its mandatory `-WorkerHost` argument. Caught at the operator side when the dry-run prompted interactively for `WorkerHost` instead of executing. Helper script signature is correct (mandatory parameter declared at lines 97-98); the helper's own example block at lines 73-76 already shows the canonical invocation form. Drift was strictly in the runbook — introduced because the agent rewrote §4.2 without first reading the helper's parameter signature. Resolved by this commit: §4.2 now passes `-WorkerHost "luciel-db.c3oyiegi01hr.ca-central-1.rds.amazonaws.com"` (canonical endpoint cross-checked against `mint_worker_db_password_ssm.py:166`, `step-28-p3-k-execute.md:228`, `step-28-commit-8-luciel-worker-sg.md:42`) on both the dry-run and real-ceremony invocations, plus a new "Required parameter" callout block explaining why we pass it explicitly. Forward-looking guard: any future runbook that wraps a PowerShell helper must read the helper's `param()` block first and reproduce all `Mandatory = $true` parameters in the example.
- D-gitignore-duplicate-stanzas-2026-05-01 → repo-hygiene commit (`86239ab`). Pulled forward from Phase 4 as a freebie alongside the broader hygiene audit. `.gitignore` rewritten: removed corrupted UTF-16 line 28 (`alembic/versions/__pycache__/s t e p 2 6 _ r e p o r t . j s o n`) and stray trailing quote on `_RESUME_MONDAY.md'`; consolidated 6 duplicate patterns across Step-27c-final and Step-27-deploy stanzas; alphabetized within stanzas; tightened section comments. File is now valid UTF-8 (previously binary-detected by git). Behavior verified equivalent: all original patterns still ignore the right files (`step26_report.json`, `overrides-foo.json`, `worker-log-dump.txt`, `RESUMEMONDAY.md`, `_RESUME_MONDAY.md`, `d11_sweep.py`, `mint-overrides.json` all confirmed ignored); no tracked files newly excluded. Audit of the broader repo found no genuine orphans — unreferenced runbooks (`step-27c-worker-deploy.md`, `step-28-prereq-cleanup.md`, `step-28-prereq-data-pattern-o.md`) and recaps (`2026-04-27-step-28-mid-phase-1-canonical.md`) are completed-historical and follow the same "don't delete audit history" protocol as the resolved drift register itself; root-level task-def JSONs (`migrate-td-rev12.json`, `ecs-service.json`, `recon-*.json`, `smoke-overrides.json`) are evidence files cited by commit messages and were preserved to keep the provenance chain intact.
- D-pillar-13-a3-sentinel-not-extractable-content-2026-05-02 → Commit 3 (`56bdab8`)
- D-audit-log-api-404 (Phase 2 §4.1 item 4 in v1) → Commit 2 (`75f6015`) + Commit 2b (`bfa2591`)
- D-retention-unbounded-delete-2026-05-03 (newly named at Phase 2 plan time, see Commit 8 message) → Commit 8 (`0d75dfe`)
- D-pillar-7-test-drift-2026-05-03 → Phase 2 HOTFIX (`2c7d0fb`)
- D-pillar-17-real-bug-2026-05-03 → Phase 2 HOTFIX (`2c7d0fb`)
- D-pillar-19-test-design-flaw-2026-05-03 → Phase 2 HOTFIX (`2c7d0fb`)
- D-mint-script-leaks-admin-dsn-via-error-body-2026-05-03 (code-level hardening only; operator-side rotation P3-H still open) → Commit 4 mint hardening (`2b5ff32`)
- **D-admin-dsn-disclosed-in-chat-2026-05-05** (HIGH severity at discovery, contained by rotation) — During P3-S Half 1 authoring on 2026-05-05 ~08:52 EDT, advisor asked operator to verify the `luciel` DB name by pasting `aws ssm get-parameter --name /luciel/database-url` output **with the password redacted as `<REDACTED>`**, providing an explicit redaction format example. Operator pasted the full DSN with the password unredacted in plaintext. The `luciel_admin` master password traversed the chat transport, advisor context window, browser history, and any chat-infrastructure logging. Realistic external exploitation likelihood was LOW (RDS in private VPC subnet, no bastion/VPN), but the audit-story integrity ("this secret has only ever lived in approved channels") was permanently broken. Per stated business principle ("we cannot make any compromises in our security and programmatic errors"), rotation was non-negotiable. **Resolved same session** by full rotation chain: (1) generated 48-char `[A-Za-z0-9-_]` password via Python `secrets` module on operator laptop, never traversing chat; (2) `aws rds modify-db-instance --master-user-password --apply-immediately` accepted by AWS at 08:55 EDT; (3) propagation verified via `describe-db-instances` showing `Status: available, Pending: {}` at 08:58 EDT (the `aws rds wait db-instance-available` waiter is NOT sufficient — it returns instantly on instances already `available` without checking pending modifications, real verification requires `Pending: {}`); (4) SSM `/luciel/database-url` updated to v3 via `put-parameter --type SecureString --overwrite`; (5) end-to-end verification via `luciel-migrate` Fargate task in correct application subnets confirmed alembic connected to RDS, ran `Context impl PostgresqlImpl` + `Will assume transactional DDL`, exited 0 in 28s of container runtime. **Old password is now dead in RDS; chat-leaked credential is non-functional.** Process patches: (a) advisor will never request output containing secrets in chat, even with redaction instructions — redaction-by-instruction failure modes are too numerous (missed character, autocomplete, copy of full output, forgotten edit); future config-extraction must use queries that return only the non-secret field (e.g. `aws rds describe-db-instances --query 'DBInstances[0].DBName'` instead of fetching the full DSN); (b) the chat-disclosure incident class must be added to operator-patterns.md as Pattern P ("never echo secrets, even partially"). Full incident record: `docs/incidents/2026-05-05-admin-dsn-disclosed-in-chat.md`.

### Resolved by Phase 3 prerequisites (executed alongside Phase 2)
- D-luciel-admin-no-mfa-2026-05-03 → P3-J resolved 2026-05-03 23:48:11 UTC. Virtual MFA device `arn:aws:iam::729005488042:mfa/Luciel-MFA` attached to `luciel-admin`. Account-wide IAM-user sweep confirmed `luciel-admin` is the only user; no follow-on MFA work needed for current account state. Forward-looking guard recorded in `PHASE_3_COMPLIANCE_BACKLOG.md` P3-J: every future IAM user must have MFA before first console use.
- D-migrate-role-conflated-with-mint-duty-2026-05-03 → P3-K resolved 2026-05-04 00:14:10 UTC (role create) + 00:19:22 UTC (smoke-test verified). `luciel-mint-operator-role` is the dedicated mint duty principal; trust policy locked to `luciel-admin` user with `aws:MultiFactorAuthPresent=true` and `aws:MultiFactorAuthAge<3600`; permissions limited to `ssm:GetParameter` on `/luciel/database-url` + KMS decrypt via SSM. Migrate task role does NOT receive admin-DSN read. Helper `scripts/mint-with-assumed-role.ps1` (commit `9e48098`) and mint script `--admin-db-url-stdin` flag (commit `ce66d06`) complete the Option 3 ceremony chain. Smoke-test confirmed: `aws ssm get-parameter --name /luciel/production/worker_database_url` returned `ParameterNotFound` post-dry-run, i.e. mechanism proven without executing real mint.
- D-canonical-recap-misdiagnosed-migrate-role-policy-gap-2026-05-03 → P3-G resolved 2026-05-03 ~20:09 EDT. `ssm:GetParameterHistory` added to `luciel-migrate-ssm-write`; live policy now has 6 SSM actions matching `infra/iam/luciel-migrate-ssm-write-after-p3-g.json` byte-for-byte. Original misdiagnosis (claiming `GetParameter` + `PutParameter` were missing) was self-corrected in `31e2b16`; this resolution closes the actual single-action gap.
- **D-iam-changes-applied-out-of-band-with-docs-2026-05-03** (NEW, self-referential) — P3-K execution Steps 2–5 ran on the operator side ~20:09–20:19 EDT without parallel docs-side coordination. Recon pass on 22:54–22:58 EDT confirmed live state matches design byte-for-byte (zero drift) but the resolution-evidence capture was reconstructed post-hoc rather than captured live. Forward-looking guard: when the operator decides to execute multi-step IAM runbooks independently, send a one-line message first so the agent stays in sync and can integrate verbatim outputs into resolution evidence in real time. Not a security drift; a process drift, logged here so the canonical record is honest about how P3-K actually got applied.

### Resolved by Phase 1 (cumulative)
- D-pattern-s-walker-missing-memory-items-leaf-2026-05-01 → Commit 12 (`f9f6f79`)
- D-prod-orphan-memory-items-step27-syncverify-7064-2026-05-01 → Commit 11 (`62a5783`)
- D-tenant-cascade-code-level-pre-stripe-2026-05-01 → Commit 12 (promoted from Step 28x slot)
- D-msg-txt-authoring-residue-2026-05-01 → chore commit `3d64ca9`
- D-tenant-patch-no-audit-row-2026-05-02 → Commit 12
- D-verification-probes-stale-memory-items-active-col-2026-05-02 → Commit 12
- D-pillar-13-worker-audit-attribution-2026-05-02 → Commit 14 (`2e31797`)
- D-pillar-13-spoof-wait-too-short-2026-05-02 → Commit 14
- D-shell-history-key-exposure-2026-05-01 → Pattern E codification (Commit 9, `bd9446b`)
- D-d11-unblock-is-backfill-not-cleanup-2026-05-01 → Commit 11 migration design
- D-stash-c1-cascade-fix-bleed-2026-05-02 → resolved during Commit 10 stash integration
- D-resource-deletion-order-leaf-first-2026-05-01 → codified in Pattern S

---

## Section 16 — Maintenance protocol

This document is a living artifact. Update protocol:

### When to update
- **Always at phase close** (28 Phase 2/3/4, then per-step from 29 onward)
- **When a strategic-question answer changes** (rare; revise §0.1 explicitly)
- **When a deliberate exclusion changes** (rare; revise §0.5 explicitly)
- **When a new strategic question surfaces** (add to §0.1 with status "candidate")
- **When the roadmap changes** (revise §0.3 with rationale in commit message)

### When NOT to update
- Inline session summaries (chat only)
- Per-commit drift entries resolved within the same session (commit messages only)
- Speculative ideas without commitment (§5 only if rising to "design surface")

### Update mechanism
1. Edit `docs/CANONICAL_RECAP.md`
2. Bump "Last updated" header
3. Commit with `recap(<phase or step>): <one-line change description>`
4. Push

### Source of truth precedence
1. Code (running prod = what's actually true)
2. Latest commit message (most recent durable shipping description)
3. This canonical recap (strategy, roadmap, locked answers)
4. Prior recaps (historical reference only)
5. Chat session summaries (ephemeral; subordinate to all above)

If they disagree:
- Code vs commit message → audit code, fix message in next commit
- Commit message vs canonical recap → update canonical recap (it was stale)
- Canonical recap vs prior recap → canonical recap wins (prior is historical)
- Anything vs chat summary → chat summary is wrong; do not propagate

---

## End of Canonical Recap v2.1