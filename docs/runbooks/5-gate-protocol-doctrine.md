# 5-Gate Protocol — Canonical Doctrine

**Status:** CANONICAL doctrine doc formalised at Arc 7 C10. The 5-gate protocol has been used five times across Arc 6 and Arc 7 ad-hoc; this document captures it as the standing posture for every future sandbox-driven prod-mutation that requires temporary IAM scope expansion.

**Anchor:** `docs/ARCHITECTURE.md` §3.2 (sandbox-agent prod-control-plane principal); `docs/DRIFTS.md` `D-prod-credential-scope-expansion-protocol-2026-05-23` (the standing-guardrail drift this doctrine satisfies); CANONICAL_RECAP §17 (per-execution records).

**This document is canon.** When operational runbooks disagree with this doctrine, the runbook updates to match — not the other way around.

---

## 1. When the 5-gate protocol applies

The 5-gate protocol is required whenever **all four** of these conditions hold:

1. The action mutates production state (Stripe Live, prod RDS, prod SSM SecureString, prod ECS service config, prod IAM topology).
2. The sandbox-agent IAM principal (`luciel-sandbox-agent`, ARN `arn:aws:iam::729005488042:user/luciel-sandbox-agent`) does **not** currently hold the IAM permission required to execute the action.
3. The action is one-shot or arc-bounded (i.e., the permission is not needed perpetually — perpetual permissions belong in the baseline inline policy via a different doctrine).
4. The blast radius of the permission, if it were left attached perpetually, exceeds the steady-state principle-of-least-privilege posture the sandbox-agent maintains.

**When the protocol does NOT apply:**

- The action is read-only and the existing baseline policy already covers it.
- The action mutates dev / sandbox resources only (no prod-axis state change).
- The action requires permissions the sandbox-agent **already** holds inline (just execute — no gate cadence needed).
- The action requires permissions the sandbox-agent will need perpetually (add to the baseline inline policy as a regular Arc commit, not under the 5-gate protocol).

## 2. The five gates

### Gate 1 — Attach

**Operator action** (sandbox-agent cannot self-attach by design — `iam:AttachUserPolicy` and `iam:PutUserPolicy` are denied):

1. Partner opens AWS Console → IAM → Users → `luciel-sandbox-agent` → Permissions tab.
2. Add permissions → Attach policies directly (for a pre-authored managed policy) **or** Add permissions → Create inline policy (for a one-off inline expansion).
3. Confirm the Permissions tab now lists the new policy.

**Why operator-driven:** keeps the attach event in the partner's Console audit trail (CloudTrail) and prevents the agent from ever holding the credential to expand its own scope. This is the strongest possible guardrail short of MFA.

**Policy authoring rule:** the policy MUST be Resource-scoped to the smallest possible ARN glob, MUST NOT contain `*` resource patterns unless the action genuinely cannot be Resource-scoped (e.g., `ec2:DescribeInstances`), and MUST be reviewed by the agent for blast-radius before partner attaches it. The blast-radius review lands in an arc-out record before Gate 1 begins.

### Gate 2 — Run

**Agent action** with credentials inline-only (never `export`, never written to disk, never committed):

```bash
AWS_ACCESS_KEY_ID='...' \
AWS_SECRET_ACCESS_KEY='...' \
AWS_DEFAULT_REGION='ca-central-1' \
python scripts/_<arc>_<action>.py > arc<N>-out/<commit>-poststate.json
```

The script MUST:

- Use idempotency keys for any Stripe Live mutation (re-runnable safely).
- Use `Overwrite=True` on SSM puts only when the target key is expected to pre-exist.
- Emit a structured JSON post-state record to `arc<N>-out/` for later verification.
- NOT prompt for confirmation — the partner-approval is implicit in Gate 1.

### Gate 3 — Verify

**Agent action** — read-only probes that prove the Gate 2 mutation landed:

```bash
# For Stripe: list active prices on the product, assert the new price IDs are active
# For SSM: get-parameter --with-decryption, assert the value matches the post-state
# For ECS: describe-services, assert task-def revision matches the new family ARN
# For RDS: connect via the sandbox agent role and assert alembic head matches expected
```

Verify writes its findings to the same arc-out record as Gate 2. A Gate-3 mismatch is a HARD STOP — partner must triage before Gate 4. Do not detach a policy when verification fails; investigation may require re-running mutations under the attached scope.

### Gate 4 — Detach

**Operator action** (mirror of Gate 1):

1. Partner opens AWS Console → IAM → Users → `luciel-sandbox-agent` → Permissions tab.
2. For attached managed policy: Detach.
3. For inline policy: Remove.
4. Confirm the Permissions tab no longer lists the policy.

**Detach MUST happen before the next commit lands.** The steady-state posture between commits is "no expansion attached". This makes the credential-blast-radius window minimal and CloudTrail-auditable.

### Gate 5 — Record

**Agent action** — write the closure record to `arc<N>-out/<commit>-record.md` documenting:

- The exact policy ARN attached at Gate 1.
- The exact Gate 2 script invocation (with credentials redacted).
- The Gate 3 verification output.
- The Gate 4 detach confirmation (timestamp from Console, or CloudTrail event ID).
- The blast-radius analysis (what the agent could have done with the scope but did not — explicit non-actions are doctrine-strong evidence of intentional scope respect).
- CANONICAL_RECAP §17 cross-ref entry.

The record commits in the same arc-commit as the action. The record is the artifact that lets a future operator (or auditor) reconstruct exactly what happened, why, and what guardrails held.

## 3. Cumulative blast-radius rule

When two or more 5-gate expansions are scheduled within the same arc, the cumulative scope MUST NOT exceed the per-expansion individual scopes summed naively. If a planned cumulative scope would grant capabilities greater than any individual scope (e.g., one policy grants ECR push, another grants ECS task-def register, the combination grants effective control-plane deploy ability), the doctrine REQUIRES that the agent surface the cumulative-scope finding to the partner before Gate 1 of the second expansion.

This protects against "expansion creep" where individually-reasonable scopes combine into an unreasonably-large effective scope.

## 4. Managed policy vs inline policy

The sandbox-agent's IAM principal has the AWS per-user inline-policy ceiling at **2048 non-whitespace characters**. Arc 5 Commit 27 hit this ceiling and resolved by introducing the managed-policy approach (`LucielSandboxArc5EcsRollingDeploy`).

**Going forward (canonical):**

- Inline policy is preferred for one-off Sids that fit inside the remaining inline-budget.
- Managed policy is preferred for any Sid block that would push the inline policy over the 2048 ceiling.
- Managed policy is also preferred for re-usable scopes (`LucielSandboxStripeScope` re-used in Arc 6 + Arc 7).
- Managed policies attach/detach atomically (one Console click); inline policies require edit-and-resave (slower, error-prone).

## 5. Standing managed policies (re-usable across arcs)

| Policy ARN | Purpose | Resource scope | First minted |
| --- | --- | --- | --- |
| `LucielSandboxStripeScope` | Stripe Live + SSM `/luciel/production/stripe_*` write | 9-key `stripe_*` namespace + KMS via SSM only | Arc 6 |
| `LucielSandboxArc5EcsRollingDeploy` | ECS `UpdateService` + `DescribeServices` | exactly 2 service ARNs (`-backend-service`, `-worker-service`) | Arc 5 Commit 27 |
| `LucielSandboxRdsSnapshotLucielDbOnly` | RDS snapshot create + describe | only `db:luciel-db` and snapshot ARN glob | Arc 5 Commit 21 |
| `LucielSandboxEcrPushBackendOnly` | ECR push + GetAuthorizationToken | only `repository/luciel-backend` | Arc 5 Commit 7 |
| `LucielSandboxArc5MigrateScope` | Inline policy — `ecs:RunTask` family-scoped, baseline reads | family `luciel-migrate:*`, broad describe reads | Arc 5 baseline (inline) |

Re-use of any policy above does NOT require a new authoring step. Re-use under the 5-gate protocol uses the existing policy ARN.

## 6. Doctrine: when partner approves a 5-gate expansion

Partner approval is captured verbatim in the arc-record, including:

- The exact policy JSON (or ARN if re-using).
- The exact action to be executed under that scope.
- The blast-radius analysis (what becomes possible, what stays impossible).
- The detach criterion (after this single execution? after this commit? after this arc?).

The partner phrase "go ahead partner" against a clearly-laid-out 5-gate expansion plan IS the approval. The agent does not need to re-confirm at Gate 2 if the plan at Gate 1 is unchanged.

If the plan changes between approval and Gate 2 (e.g., a third SSM key is added to the put list mid-flight), the agent MUST pause and re-confirm with the partner before executing.

## 7. Doctrine: when the 5-gate protocol fails

**Gate 1 fails** (partner cannot attach — e.g., policy ARN typo, Console permission error): pause, debug with partner, no agent action required.

**Gate 2 fails** (script raises): the policy is attached; the failure may have left partial state (Stripe price minted but SSM put failed, for example). The agent MUST diagnose against the post-state JSON before any next action. Recovery may require re-running parts of the script (idempotency keys make this safe) or surgical SSM/Stripe cleanup. Do not detach until verification (Gate 3) confirms the world is in an expected state — either fully-applied or fully-reverted.

**Gate 3 fails** (verification mismatch): HARD STOP. Surface to partner with the exact discrepancy. Do not auto-recover. The partner decides whether to roll forward, roll back, or extend the script to converge.

**Gate 4 fails** (detach fails — Console error, IAM eventual-consistency lag): partner retries detach. The agent treats the scope as still-attached until the partner confirms detach. No further commits land until detach is confirmed.

**Gate 5 fails** (record write fails — disk full, etc.): trivial recovery, the action's effects are already verified.

## 8. Execution history (canonical record)

| Arc / Commit | Date | Policy | Action | Outcome |
| --- | --- | --- | --- | --- |
| Arc 5 Commit 7 | 2026-05-23 | `LucielSandboxEcrPushBackendOnly` + `LucielSandboxArc5RegisterTaskDef` | First sandbox-driven ECR push + task-def register | ✅ |
| Arc 5 Commit 21 | 2026-05-23 | `LucielSandboxRdsSnapshotLucielDbOnly` | Sandbox-driven RDS pre-migrate snapshot | ✅ |
| Arc 5 Commit 27 | 2026-05-23 | `LucielSandboxArc5EcsRollingDeploy` | ECS rolling deploy for V2 schema cutover | ✅ |
| Arc 6 Commit X | 2026-05-23 | `LucielSandboxStripeScope` (NEW) | Stripe Live SKU mint + SSM SecureString puts | ✅ |
| Arc 7 Commit 1 Slice 2-3 | 2026-05-24 | `LucielSandboxStripeScope` (re-use) | enterprise_monthly + enterprise_annual mint + SSM | ✅ |

Six clean executions, zero scope-leak incidents, zero credential-leak incidents. The protocol holds.

## 9. Cross-refs

- `D-prod-credential-scope-expansion-protocol-2026-05-23` — the standing guardrail drift this doctrine satisfies (remains OPEN by design as a perpetual reminder).
- `docs/ARCHITECTURE.md` §3.2 — sandbox-agent prod-control-plane principal definition.
- `arc7-out/arc7-commit1-slice2-slice3-handoff.md` §5-gate-protocol-partner-steps — first per-execution writeup of the cadence (this doctrine doc generalises that).
- `docs/runbooks/aws-sandbox-credential-posture.md` — operational credential-handling rules (companion to this doctrine).
- CANONICAL_RECAP §17 per-arc entries — execution records for each 5-gate invocation.
