# 2026-05-04 — Session end: Pillar 13 A3 fix complete, mint ceremony paused at architectural boundary

**Author:** session-end recap, written immediately at pause point.
**Branch:** `step-28-hardening-impl`
**Last commit at pause:** `e1154bd` (P3-K-followup IAM policy patch)
**Production state:** stable, no mutations from today's mint attempt (see §3 below)

---

## 1 — What landed today (in commit order)

| Commit | Type | Summary |
|---|---|---|
| `81b9e5a` | code | Commit A: one-line auth-middleware fix `actor_user_id = agent.user_id` at `app/middleware/auth.py:124` + 5-test regression guard. Resolves D-pillar-13-a3-real-root-cause-2026-05-04. |
| `55a36b4` | code | Commit D: removed P13_DIAG instrumentation (-58 lines auth.py + chat_service.py + deleted diag_p13_repro.py -269 lines); archived 19/19 verification to `docs/verification-reports/`; recap v1.5 + 5 new P3 entries (P3-M..Q). |
| `86239ab` | chore | Repo hygiene: rewrote `.gitignore` (1785→1606 bytes; was binary-detected by git due to UTF-16 corruption on line 28; removed stray quote, 6 duplicates). Resolves D-gitignore-duplicate-stanzas-2026-05-01. |
| `374912a` | docs | Runbook §4 v2 rewrite: Option 3 ceremony as canonical mint flow; removed obsolete §4.7 (P3-H already done); SSM path lowercased; §4.0 pre-mint checklist + 4-row prerequisite gate table. |
| `6d596f7` | docs | Runbook §4.2 -WorkerHost fix: added mandatory parameter that v2 had omitted. Drift caught by operator on first dry-run. Resolves D-runbook-mint-missing-workerhost-arg-2026-05-04. |
| `e1154bd` | iam | P3-K-followup mint role policy: added `ReadWorkerSsmForPreflightAndMint` + `EncryptWorkerSsmSecureStringViaSsm` statements (3→5). Pre-image preserved as `.pre-p3-k-followup-2026-05-04` artifact. Live policy applied via `aws iam put-role-policy` and verified post-image (5 statements byte-for-byte match IaC). Resolves D-p3-k-policy-missing-worker-ssm-write-2026-05-04. |

**Pillar 13 A3 status:** ✅ resolved and verified live. 19/19 green test suite passes against base_url 127.0.0.1:8000 with the deployed Commit A fix.

---

## 2 — What did NOT land today: Commit 4 mint ceremony

**Final attempt outcome:** mint script aborted at `psycopg.connect(admin_dsn)` (line 554) with `ConnectionTimeout: connection timeout expired`. The Pattern E redaction worked correctly — no DSN leakage in the error. The script's outer try/except caught the connect failure cleanly, returned exit code 1, and the helper cleared assumed credentials.

**Root cause:** Option 3 ceremony as designed has a load-bearing assumption — "operator runs the ceremony from their laptop" — that is incompatible with the production VPC posture, "RDS is in a private subnet with no public ingress." This boundary was never exercised by any prior smoke test:

- P3-K smoke test (2026-05-04 00:19:22 UTC): used `--dry-run`, which returns at line 491 of `mint_worker_db_password_ssm.py` BEFORE the DB connect at line 554. Never tested DB reachability.
- All previous "Option 3 ceremony" claims in canonical recap: based on dry-run validation only.
- This session's two prior dry-runs: same dry-run-early-return path, same gap.

**The runbook itself warned about this** at §4.1 (recon section): "Run via Pattern N one-shot (luciel-migrate:N) — do NOT add temporary IAM ingress to RDS for psql from a laptop." That guidance was correct for recon, and it should have been generalized to the mint ceremony from the start. It wasn't. That is the architectural error this session uncovered.

---

## 3 — Production state assessment (verified zero mutations today)

| Component | State | Evidence |
|---|---|---|
| Postgres `luciel_worker` password | Unchanged (still old value) | Mint script aborted at `psycopg.connect()` line 554, BEFORE `verify_role_state` (570) or `alter_role_password` (571). Outer try/except caught the timeout exception at line 555. |
| SSM `/luciel/production/worker_database_url` | Empty / `ParameterNotFound` | Confirmed via `aws ssm get-parameter` Step B; no `put-parameter` ever executed. |
| Mint role IAM policy | 5 statements (post-P3-K-followup) | Verified live via Step F.3 `aws iam get-role-policy`. |
| MFA TOTPs burned | 4 (Step C, Step D, Step C-redo, Step D-redo) | No security impact — TOTP codes are single-use anyway. Logged for honest cost accounting. |
| Pillar 13 A3 fix | Live and verified | uvicorn PID 17708 running clean post-Commit-D; 19/19 green archived. |

---

## 4 — Drift register additions

### Resolved this session
- `D-pillar-13-a3-real-root-cause-2026-05-04` → `81b9e5a` + `55a36b4`
- `D-gitignore-duplicate-stanzas-2026-05-01` → `86239ab` (pulled forward from Phase 4)
- `D-runbook-mint-missing-workerhost-arg-2026-05-04` → `6d596f7`
- `D-p3-k-policy-missing-worker-ssm-write-2026-05-04` → `e1154bd`

### Newly logged, NOT yet resolved
- **`D-option-3-ceremony-cannot-reach-private-rds-from-laptop-2026-05-04`** — the architectural boundary documented in §2 above. Resolution requires re-architecting the mint ceremony to run inside the VPC (Pattern N variant). Logged as Phase-3 backlog item P3-S (this commit).
- **`D-mint-script-dry-run-skips-preflight-2026-05-04`** — `mint_worker_db_password_ssm.py`'s `--dry-run` path returns at line 491 before the pre-flight at line 497 and the DB connect at line 554. This means dry-run does NOT validate either the IAM permissions (caught by Step D) OR the network reachability (caught by Step D-redo). Both could have been caught earlier with a more thorough dry-run. Resolution: ~10-line patch to call `preflight_ssm_writable` AND attempt a connection-only psycopg connect (followed by close, no SQL) before the dry-run early return. Out of scope for this session.

---

## 5 — Phase 3 backlog additions

- **P3-R** (logged earlier this session, P2): MFA TOTP echoes in PowerShell terminal during mint ceremony. Recommend `Read-Host -AsSecureString` with try/finally zero. ~10 LOC fix.
- **P3-S** (logged this commit, P0 for Phase 2 close): mint ceremony architectural rework — replace laptop-direct-DB-connect with Pattern N variant (Fargate one-shot task running mint script in-VPC). Larger work item. Blocks Commit 4 and therefore blocks Phase 2 close.

---

## 6 — Recommended next session structure

When resuming, do NOT immediately retry the mint. Instead:

1. **Read this recap first.** Then read `docs/runbooks/operator-patterns.md` Pattern N to understand the canonical in-VPC execution shape.
2. **Design P3-S deliberately.** Two sub-options to choose between:
   - 6.a. **Mint task variant** — new `luciel-mint:1` task definition with new task role; helper rewritten to invoke `aws ecs run-task` and tail CloudWatch Logs back to the operator. Cleanest audit story.
   - 6.b. **Mint via existing migrate task variant** — overload `luciel-migrate:N` with mint command override. Quickest. Audit story is muddier ("why did the migrate role mint a worker password?").
   - Recommendation: 6.a, even though it's more work. Multi-tenant SaaS audit posture requires single-purpose ceremonies.
3. **Build it carefully.** Likely a 60-90 minute session. Will add:
   - New IAM role `luciel-mint-task-role` with the same 5 statements that `luciel-mint-operator-role` has (still scoped to admin-DSN read + worker-SSM write + KMS via SSM). The mint operator role still exists for the AssumeRole-to-pass-credentials path; the task role is what the running task uses.
   - New task definition `luciel-mint` (no `:N` semantics needed, single-purpose).
   - Helper rewrite: `mint-with-assumed-role.ps1` becomes `mint-via-fargate-task.ps1` (or v2 of the same file). Same MFA + AssumeRole prelude on the laptop, but the body becomes `aws ecs run-task` + CloudWatch tail.
   - New runbook section §4.0.6 "Pattern N mint architecture" + supersession of §4.2's laptop-direct invocation.
   - Drift `D-option-3-ceremony-cannot-reach-private-rds-from-laptop-2026-05-04` resolution closure note.
4. **Smoke test the full path.** Crucially, the smoke test must include a real (non-dry-run) connection to RDS, even if the mint itself is dry-run. The bug we just found existed because every previous smoke skipped this layer.
5. **Then run the real mint.**

---

## 7 — Honest accounting of session quality

This session was net positive — Pillar 13 A3 is fixed and verified, repo hygiene improved, three IAM/runbook drifts caught and closed, and the architectural error in the mint ceremony was discovered without mutating any production state. The pre-flight + outer-try-except defenses in the mint script worked exactly as designed.

The mint ceremony's architectural error itself is on me (the agent). I designed Option 3 without checking whether the laptop could actually reach RDS, and I didn't catch the gap during P3-K's smoke test (which I helped author). The runbook §4.1 was telling me directly with its "do NOT add temporary IAM ingress" warning, and I didn't generalize the lesson.

The right response to that error is what we just did: name it, log it, fix the IAM policy that was *also* wrong, preserve all evidence, and pause for fresh judgment rather than push through. That's the discipline the user asked for at the start of the session ("we cannot make any compromises in our security and programmatic errors").

---

## 8 — Resumption checklist

When you next resume:

- [ ] `cd /home/user/workspace/Luciel-work && git pull origin step-28-hardening-impl` (confirm `e1154bd` or this commit is HEAD)
- [ ] Read this recap (`docs/recaps/2026-05-04-mint-architectural-boundary-pause.md`)
- [ ] Read `docs/runbooks/operator-patterns.md` Pattern N
- [ ] Read `docs/PHASE_3_COMPLIANCE_BACKLOG.md` P3-S (when added)
- [ ] Decide between Pattern N sub-options 6.a and 6.b above
- [ ] Design and execute. Do NOT skip the smoke-test-with-real-DB-connect step from §6.4.

Production state remains stable until that work completes. Phase 1 protections (audit-log append-only by API, async memory extraction, etc.) are all live and unaffected.
