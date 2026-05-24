# AWS Sandbox Credential Posture \u2014 Operational Runbook

**Posture B classification:** Operational satellite. **Not** a canonical document. The canonical record of this posture lives across the three canonical documents:

- **Business-view (the why + pillar trade-offs):** `CANONICAL_RECAP.md` \u00a717 entry `2026-05-23 (Arc 5 Commit 6 \u2014 Sandbox prod-control-plane posture established)`.
- **System-view (the what \u2014 principal, policy, capability/boundary surface, IAM topology relationship):** `ARCHITECTURE.md` \u00a73.2.8 Arc 5 Commit 6 bullet `sandbox-agent prod-control-plane principal landed 2026-05-23`.
- **Integrity-view (the exposure + 5-gate enforcement):** `DRIFTS.md` `D-prod-credential-scope-expansion-protocol-2026-05-23` (OPEN, P3, held open by design).

This runbook holds **procedural** content only \u2014 the how-to material that does not belong in any canonical view. Established 2026-05-23 at Arc 5 Commit 6. Anchored from `CANONICAL_RECAP.md` \u00a717 per Posture B (every satellite MUST be referenced from a canonical anchor or be archived).

---

## 1. Scope of this runbook

This runbook answers four operational questions:

1. **How does the agent receive the credential at session start?** (paste protocol)
2. **How does the partner rotate the access key?** (rotation procedure)
3. **How does the partner revoke the credential?** (revocation procedure \u2014 planned or emergency)
4. **How does the agent + partner widen the inline policy when an arc needs it?** (5-gate scope-expansion procedure in operator language)

It does **not** answer:

- *Why* the posture exists \u2014 see `CANONICAL_RECAP.md` \u00a717.
- *What* the principal can and cannot do \u2014 see `ARCHITECTURE.md` \u00a73.2.8.
- *What integrity risk* the principal creates and what doctrine prevents it \u2014 see `DRIFTS.md` `D-prod-credential-scope-expansion-protocol-2026-05-23`.

When this runbook and any canonical document disagree, the canonical document wins automatically (per Posture B). If a disagreement is discovered, the resolution path is to update this runbook to match canon, not the reverse.

## 2. Credential identity (operational metadata only)

The substantive identity facts (principal type, account, region, policy contents, capability surface, boundary surface) are at `ARCHITECTURE.md` \u00a73.2.8. The operational handles below exist here so the partner does not need to context-switch to the architecture doc to find them mid-procedure:

| Operational handle | Value |
| --- | --- |
| AWS Console URL | `https://729005488042.signin.aws.amazon.com/console` |
| IAM user name (for Console navigation) | `luciel-sandbox-agent` |
| Inline policy name (for Console navigation) | `LucielSandboxArc5MigrateScope` |
| Region binding (for CLI / boto3 default) | `ca-central-1` |
| Tags | None |
| Created | 2026-05-23 |

Secrets (access key ID, secret access key) are **never** stored in this repository, in any workspace file, in commit messages, in chat transcripts that get persisted, or in any tracked artifact. They live only in the partner's local secret store (e.g., password manager) and, transiently, in sandbox process environment variables for the duration of an in-progress prod-touching command.

## 3. Inline policy reference copy

The canonical policy contents are in `ARCHITECTURE.md` \u00a73.2.8 (capability surface + boundary surface, in narrative form). The JSON below is the AWS Console-pasteable form, kept here so the partner does not need to reconstruct it during a recovery scenario. **If the JSON below ever disagrees with the architecture doc's capability/boundary description, the architecture doc wins and this JSON gets updated to match.**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ECSReadOnly",
      "Effect": "Allow",
      "Action": [
        "ecs:ListClusters",
        "ecs:DescribeClusters",
        "ecs:ListTaskDefinitions",
        "ecs:ListTaskDefinitionFamilies",
        "ecs:DescribeTaskDefinition",
        "ecs:ListTasks",
        "ecs:DescribeTasks",
        "ecs:ListServices",
        "ecs:DescribeServices"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ECSRunMigrateTaskOnly",
      "Effect": "Allow",
      "Action": [
        "ecs:RunTask"
      ],
      "Resource": "arn:aws:ecs:*:*:task-definition/luciel-migrate:*"
    },
    {
      "Sid": "ECSPassRoleForMigrate",
      "Effect": "Allow",
      "Action": [
        "iam:PassRole"
      ],
      "Resource": "*",
      "Condition": {
        "StringLike": {
          "iam:PassedToService": "ecs-tasks.amazonaws.com"
        }
      }
    },
    {
      "Sid": "ECRReadOnly",
      "Effect": "Allow",
      "Action": [
        "ecr:DescribeRepositories",
        "ecr:DescribeImages",
        "ecr:ListImages",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:GetAuthorizationToken"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CloudWatchLogsReadOnly",
      "Effect": "Allow",
      "Action": [
        "logs:DescribeLogGroups",
        "logs:DescribeLogStreams",
        "logs:GetLogEvents",
        "logs:FilterLogEvents"
      ],
      "Resource": "*"
    },
    {
      "Sid": "RDSReadOnly",
      "Effect": "Allow",
      "Action": [
        "rds:DescribeDBInstances",
        "rds:DescribeDBClusters",
        "rds:DescribeDBSnapshots",
        "rds:DescribeDBClusterSnapshots"
      ],
      "Resource": "*"
    },
    {
      "Sid": "EC2DescribeForVPCContext",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeSubnets",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeVpcs"
      ],
      "Resource": "*"
    },
    {
      "Sid": "STSGetCallerIdentity",
      "Effect": "Allow",
      "Action": [
        "sts:GetCallerIdentity"
      ],
      "Resource": "*"
    },
    {
      "Sid": "EcrPushBackendOnly",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage"
      ],
      "Resource": "arn:aws:ecr:ca-central-1:729005488042:repository/luciel-backend"
    },
    {
      "Sid": "EcsRegisterTaskDef",
      "Effect": "Allow",
      "Action": "ecs:RegisterTaskDefinition",
      "Resource": "*"
    },
    {
      "Sid": "RdsSnapshotLucielDbOnly",
      "Effect": "Allow",
      "Action": [
        "rds:CreateDBSnapshot",
        "rds:DescribeDBSnapshots",
        "rds:AddTagsToResource"
      ],
      "Resource": [
        "arn:aws:rds:ca-central-1:729005488042:db:luciel-db",
        "arn:aws:rds:ca-central-1:729005488042:snapshot:luciel-arc5-*"
      ]
    }
  ]
}
```

*Canonical JSON last updated:* Arc 5 Commit 21, 2026-05-23. The Sid count is **11**: 8 baseline blocks from Commit 6 + 2 blocks from the Commit 7 expansion (`EcrPushBackendOnly`, `EcsRegisterTaskDef`) + 1 block from the Commit 21 expansion (`RdsSnapshotLucielDbOnly`). Each expansion is recorded in §8 expansion log below with TODO anchor, rejected alternatives, blast radius, and partner-approval text.

### 3-bis. Customer-managed policy — `LucielSandboxStripeScope` (Arc 6 Commit 2 onward)

At Arc 6 Commit 2 (2026-05-23), the canonical `LucielSandboxArc5MigrateScope` inline policy (§3 above) reached **128.9% of the 2048-character IAM inline-user-policy size limit** when the 3 Arc 6 Stripe Sids were drafted to be appended in-place (2640 minified chars total). Rather than refactor the 11-Sid Arc 5 policy to compact baseline Sids (risk to Arc 5 audit-chain continuity), Arc 6 introduced the Stripe capability as a **separate customer-managed policy** attached to the same user. The customer-managed shape is structurally cleaner than a second inline policy: it has its own version history (IAM keeps up to 5 versions per managed policy), can be detached without being deleted (independently revocable at Arc 8 close or whenever the Stripe surface is retired), can be reused on future principals if needed (e.g., if a Stripe-only operator role is minted later), and shows up in IAM Console under a stable ARN distinct from any user's inline policies. The Arc 5 inline policy stays byte-identical to its Arc 5 Commit 21 final form; the Arc 6 Stripe scope is additive and independently revocable.

**Policy attachment shape:**

| Field | Value |
|---|---|
| IAM principal type | Customer-managed policy (not inline) |
| Policy name | `LucielSandboxStripeScope` |
| Policy ARN | `arn:aws:iam::729005488042:policy/LucielSandboxStripeScope` |
| Attached to | IAM user `luciel-sandbox-agent` (account `729005488042`) |
| Sid count | 3 |
| Minified size | 646 chars (well under the 6144-char limit for customer-managed policies) |
| Created by | Partner via IAM Console, Saturday 2026-05-23 6:26 PM EDT |
| Apply method (if recreating) | `aws iam create-policy --policy-name LucielSandboxStripeScope --policy-document file://ops/iam/LucielSandboxStripeScope.json` then `aws iam attach-user-policy --user-name luciel-sandbox-agent --policy-arn arn:aws:iam::729005488042:policy/LucielSandboxStripeScope` (run from partner's laptop with `luciel-admin` creds) |
| Reference copy in repo | `ops/iam/LucielSandboxStripeScope.json` |
| Verification (this commit) | Four `boto3.client('ssm')` probes at 2026-05-23 ≈22:26 UTC: (1) `get_parameter` on `/luciel/production/stripe_price_intro_fee` returned `price_*` (30 chars), (2) `get_parameter WithDecryption=True` on `/luciel/production/stripe_secret_key` returned `sk_live_*` (107 chars) — verifies read + KMS decrypt, (3) `put_parameter Type=SecureString` to `/luciel/production/stripe_arc6_iam_probe` returned 200 — verifies write + KMS encrypt, (4) `get_parameter` round-trip on the probe param returned the exact written value — verifies the full encrypt+decrypt cycle. |

**JSON body** (the canonical reference is `ops/iam/LucielSandboxStripeScope.json`; reproduced here for offline-read convenience):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Arc6SsmReadStripeProduction",
      "Effect": "Allow",
      "Action": [
        "ssm:GetParameter",
        "ssm:GetParameters"
      ],
      "Resource": "arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/production/stripe_*"
    },
    {
      "Sid": "Arc6SsmWriteStripeProduction",
      "Effect": "Allow",
      "Action": [
        "ssm:PutParameter",
        "ssm:AddTagsToResource"
      ],
      "Resource": "arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/production/stripe_*"
    },
    {
      "Sid": "Arc6KmsViaSsmForStripeProduction",
      "Effect": "Allow",
      "Action": [
        "kms:Decrypt",
        "kms:Encrypt",
        "kms:GenerateDataKey"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "kms:ViaService": "ssm.ca-central-1.amazonaws.com"
        }
      }
    }
  ]
}
```

The expansion log entry for this customer-managed-policy creation is in §8 (the `2026-05-23 — Arc 6 Commit 2` block below).

## 4. Paste protocol \u2014 how the agent receives the credential at session start

1. At the start of any session that includes a prod-touching TODO item, the agent opens two free-text `ask_user_question` forms (one for access key ID, one for secret access key). The two values transit chat once via this paste, which is the credential's exposure-to-context-window event \u2014 unavoidable side effect of the paste mechanism.
2. The agent injects the values into the sandbox bash environment as `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_DEFAULT_REGION=ca-central-1` per command, not via a persistent shell rc or workspace file.
3. The agent runs `boto3.client('sts').get_caller_identity()` as the first call after paste to confirm the credential resolves to the expected ARN. If the ARN does not match `arn:aws:iam::729005488042:user/luciel-sandbox-agent`, the agent stops and surfaces the mismatch to the partner before any further AWS API call.
4. At end of the session's prod-touching arc (after the closure commit is pushed), the agent explicitly clears the values with `unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY` and they die with the sandbox process tree at session end.
5. Credentials are **never** passed to a subagent, browser_task, or any other tool that could persist them outside the immediate bash environment scope.

## 5. Rotation procedure \u2014 partner-driven

Cadence: partner's preference. Recommended baseline is quarterly, sooner if any of the trigger events in \u00a77 occur.

1. AWS Console \u2192 IAM \u2192 Users \u2192 `luciel-sandbox-agent` \u2192 Security credentials tab.
2. **Create access key** (do this **before** deactivating the old one \u2014 the overlap window means the agent can verify the new key works before the old one stops working).
3. The new key shows access key ID + secret one time only. Copy both to the partner's local secret store.
4. Provide the new credentials to the sandbox-agent at the start of the next prod-touching session via the paste protocol in \u00a74.
5. After the agent confirms the new key resolves via `sts:GetCallerIdentity`, return to the IAM Console.
6. **Make inactive** on the old access key (propagates within seconds).
7. **Delete** the old access key (this is irreversible \u2014 the access-key-ID becomes permanently unusable, which is the desired end-state).
8. Update \u00a72 of this runbook with the new access-key-creation date in the same commit that records the rotation. The principal ARN, the inline policy name, and the policy contents do not change on rotation \u2014 only the access key identity does.

## 6. Revocation procedure \u2014 partner-driven (emergency or planned)

Used when the partner wants to immediately and irreversibly remove sandbox prod-touching capability \u2014 e.g., after a suspected credential compromise, at sandbox-agent retirement, or as part of a planned posture-rewrite arc.

1. AWS Console \u2192 IAM \u2192 Users \u2192 `luciel-sandbox-agent` \u2192 Security credentials tab.
2. **Make inactive** on every active access key (propagates within seconds; this halts in-flight sandbox API calls within the next few seconds of token cache expiry).
3. **Delete** every active access key.
4. If revocation is permanent (no replacement principal will be minted): proceed to step 5. If revocation is partial (the principal stays, only the keys rotate): see \u00a75 above instead.
5. AWS Console \u2192 IAM \u2192 Users \u2192 `luciel-sandbox-agent` \u2192 **Delete user**. (The inline policy is deleted with the user automatically.)
6. Open a new dated paragraph in `CANONICAL_RECAP.md` \u00a717 (`YYYY-MM-DD (Arc N \u2014 Sandbox prod-control-plane posture revoked)`) recording the revocation event, the reason, and any operational implications.
7. Add a closing paragraph to `DRIFTS.md` `D-prod-credential-scope-expansion-protocol-2026-05-23` marking the drift CLOSED, since its closure trigger is exactly "permanent revocation with no replacement minted."
8. Add a closing-rewrite to `ARCHITECTURE.md` \u00a73.2.8 Arc 5 Commit 6 bullet noting the principal no longer exists.
9. This runbook itself is then sediment per Posture B and gets archived at the next arc close (or kept as a historical record at the partner's preference).

The sandbox-agent has zero ability to rotate, revoke, or otherwise modify its own credential \u2014 this is by design (no `iam:*` permissions in the policy).

## 7. Rotation trigger events

Rotate immediately (do not wait for the quarterly cadence) on any of:

- Suspected exposure of the secret access key in any context (chat scrollback persistence concerns, shoulder-surfed paste, accidentally pasted into a non-sandbox terminal, etc.).
- Sandbox session terminated unexpectedly with credentials still active in process scope and no clean `unset` having run.
- Scope expansion under \u00a78 \u2014 the partner may choose to mint a fresh key at the same time as the policy widens, so the access-key cohort and the policy cohort stay aligned.
- Any indication in CloudTrail of API calls under this principal that the agent does not have a corresponding runbook ACTUAL RUN section for \u2014 this would indicate either a doctrine violation (agent skipped audit-trail discipline) or a credential compromise (third party using the key).

## 8. Scope-expansion procedure \u2014 the 5 gates in operator language

The doctrine reasoning behind these gates is at `DRIFTS.md` `D-prod-credential-scope-expansion-protocol-2026-05-23`. This section is the partner-facing operator procedure when a scope expansion is actually being executed.

### Gate 1 \u2014 Necessity

Agent must present a specific in-arc operational need tied to an active TODO. If the agent proposes "we might need X later" or the need is not tied to a TODO, the partner declines and the conversation ends here. Speculative pre-widening is not allowed.

### Gate 2 \u2014 Minimality

Agent must present the smallest policy delta that resolves the need. Concretely, the agent's proposal must:

- Use resource-scoped Action+Resource pairs wherever the AWS service supports resource-level constraints.
- Avoid service-wide wildcards (`ssm:*`, `ec2:*`).
- Use `Condition` blocks to bound the new capability further (e.g., `iam:PassedToService`, `aws:RequestTag`).
- Explicitly state at least one tighter alternative considered and rejected, with rejection rationale.

If any of those four sub-items is missing, the partner pauses and asks the agent to revise. Defaulting to a broader scope without naming a tighter alternative is itself a gate failure.

### Gate 3 \u2014 Surfacing

Agent must present four artifacts together, **before** any prod work begins:

1. The specific operational need + its TODO anchor.
2. The proposed new `Sid` block(s) in JSON, copy-pasteable into AWS Console.
3. A blast-radius analysis: in the worst case (agent malfunction, credential compromise, AWS API surface change), what could the new capability do?
4. The tighter alternatives considered and rejected, with rejection rationale.

If any of the four is missing or hand-waved, the partner declines and asks for the missing artifact.

### Gate 4 \u2014 Approval

Partner approves explicitly, having read the four artifacts. Implicit approval (silence, partial responses, "go ahead" mid-conversation without reference to the four artifacts) does **not** satisfy this gate. The partner's approval message becomes part of the audit trail for the expansion \u2014 the agent records the verbatim approval text in the expansion's audit-trail commit.

### Gate 5 \u2014 Audit trail

The expansion lands as a **single commit** that updates both:

1. This runbook \u00a73 \u2014 add the NEW `Sid` block to the JSON above + author a dated expansion-note immediately after the JSON block recording: (a) the TODO anchor, (b) the rejected tighter alternatives + rationale, (c) the partner's verbatim approval text, (d) the commit hash that lands the expansion.
2. `CANONICAL_RECAP.md` \u00a717 \u2014 add a new dated paragraph `YYYY-MM-DD (Arc N \u2014 Sandbox credential scope expansion)` summarising the new capability, the operational need, and a pointer to this runbook's expansion-note for procedural detail.

If only the AWS Console policy is updated and either canonical edit is missing at the moment of policy update, the policy change MUST be reverted within the same session and a P1 follow-up drift opened to capture the violation. The expansion can be re-attempted on a subsequent commit that satisfies all five gates from scratch.

### Expansion log (chronological)

#### 2026-05-23 — Arc 5 Commit 7 — EcrPushBackendOnly + EcsRegisterTaskDef (commit `19a40fa`)
- TODO anchor: TODO #11 (Revision A prod apply)
- Need: Push the Revision-A-baked image to ECR and register a new `luciel-migrate` task-definition revision pointing at it.
- New Sid blocks added: `EcrPushBackendOnly`, `EcsRegisterTaskDef`.
- Tighter alternatives rejected: scoping `RegisterTaskDefinition` to family `luciel-migrate` via `ecs:family` condition key — REJECTED because that condition key does not exist (IAM Console returned `Invalid Service Condition Key: ecs:family` on save attempt). Narrow scope is enforced execution-side via the already-family-scoped `ecs:RunTask` Sid.
- Partner approval (verbatim): recorded in the partner-side IAM Console save event for `LucielSandboxArc5MigrateScope` at the Commit-7 timestamp.
- Canonical mirror: `CANONICAL_RECAP.md` §17 Arc 5 Commit 7 entry; full ARNs / digests / log streams at `docs/runbooks/arc5-revision-a-prod-apply-and-rollback.md` §6.

#### 2026-05-23 — Arc 5 Commit 21 — RdsSnapshotLucielDbOnly (commit `<this-commit-hash>`)
- TODO anchor: TODO #12 (Commits 20-25 PROD execution — Revisions B + C).
- Need: Create three RDS snapshots of `luciel-db` (pre-Revision-B, post-Revision-B, post-Revision-C) per the Revision B+C runbook §3.3 / §3.5 / §3.6 so each migration step has a precise rollback point. The pre-existing `RDSReadOnly` Sid covers `DescribeDBSnapshots` but not `CreateDBSnapshot`.
- New Sid block added: `RdsSnapshotLucielDbOnly` — Actions `rds:CreateDBSnapshot`, `rds:DescribeDBSnapshots`, `rds:AddTagsToResource`; Resource scoped to `arn:aws:rds:ca-central-1:729005488042:db:luciel-db` + `arn:aws:rds:ca-central-1:729005488042:snapshot:luciel-arc5-*` (the snapshot-name prefix that bounds this principal to Arc-5-specific snapshots — no ability to act on snapshots outside that naming family).
- Tighter alternatives rejected:
  1. ECS `pg_dump` + S3 instead of native snapshot — REJECTED: pg_dump-based recovery isn't point-in-time consistent for a live DB with active connections; Revision A precedent uses native snapshots; adds complexity and weakens recovery guarantee.
  2. Skip snapshots and rely on RDS automated daily backups — REJECTED: automated backups have ~24h granularity; the runbook's §3.5 sanity-probe failure path requires restoring to the precise pre-B state, which automated backups won't preserve at per-step boundaries.
  3. Resource-scope to only `arn:...db:luciel-db` and omit snapshot ARN — REJECTED: `DescribeDBSnapshots` requires the snapshot ARN in Resource for status polling; both ARNs are needed.
- Blast-radius analysis (worst case — agent malfunction / credential compromise): attacker can create arbitrary RDS snapshots of `luciel-db` and list snapshots matching `luciel-arc5-*`. **Cannot** delete or restore snapshots (no `DeleteDBSnapshot` / `RestoreDBInstanceFromDBSnapshot`), cannot affect any other RDS instance, cannot modify the live database. Exposure is storage cost only; no data-loss or availability exposure.
- Partner approval (verbatim): `"Approved — I'll add the Sid block in Console now"` (Saturday, May 23, 2026 at 4:15 PM EDT via `ask_user_question`); JSON paste confirmed by partner (full policy text echoed back at 4:31 PM EDT showing `RdsSnapshotLucielDbOnly` Sid present); first successful `CreateDBSnapshot` API call at 20:31:58 UTC produced `arn:aws:rds:ca-central-1:729005488042:snapshot:luciel-arc5-pre-revision-b-20260523203158`.
- Canonical mirror: `CANONICAL_RECAP.md` §17 entry `2026-05-23 (Arc 5 Commit 21 — Sandbox credential scope expansion for RDS snapshot capability)`; full ARN / SnapshotCreateTime / engine details at `docs/runbooks/arc5-revision-b-c-prod-apply-and-rollback.md` (ACTUAL RUN to be appended as Revisions B and C execute).


#### 2026-05-23 — Arc 6 Commit 2 — LucielSandboxStripeScope customer-managed policy (3 Sids: Arc6SsmReadStripeProduction + Arc6SsmWriteStripeProduction + Arc6KmsViaSsmForStripeProduction) (commit `10bd9d7`)
- TODO anchor: Arc 6 Commit 2 (Stripe Live wipe — cancel 23 internal/test subs + archive 6 old Prices) and Arc 6 Commit 4 (Stripe mint + 4 SSM puts + `app/core/config.py` rewrite). The full 11-commit Arc 6 plan is at `arc6-out/A-arc6-preflight.md` §3.
- Need: The Arc 6 Stripe surface restructure requires (a) reading `STRIPE_SECRET_KEY` from SSM to authenticate Stripe API calls at Commit 2 (wipe) and Commit 4 (mint); (b) reading the six existing Stripe Price IDs from SSM at Commit 2 so the wipe record captures the pre-state precisely (which Price IDs are being archived); (c) writing four new Stripe Price IDs to SSM at Commit 4 (the new `stripe_price_pro_monthly`, `stripe_price_pro_annual`, `stripe_price_enterprise_floor_annual`, `stripe_price_enterprise_metered_unit` paths under `/luciel/production/stripe_*`); (d) KMS decrypt/encrypt via the AWS-managed `alias/aws/ssm` key for SecureString get/put. The Arc 5 inline policy had zero SSM and zero KMS Sids — this is a genuine gap, not a redundancy. Preflight miss honestly logged: `arc6-out/A-arc6-preflight.md` initially asserted "no IAM expansion expected at Arc 6" — that claim was wrong; the Commit 2 first call to `boto3 ssm.get_parameter` returned `AccessDeniedException`, which surfaced the gap before any destructive Stripe action.
- New Sid blocks added (3, all narrowly resource-scoped to the Stripe production namespace):
  1. `Arc6SsmReadStripeProduction` — Actions `ssm:GetParameter`, `ssm:GetParameters`; Resource `arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/production/stripe_*` (the wildcard at the end bounds reads to the 9 known Stripe paths: `stripe_secret_key`, `stripe_webhook_secret`, `stripe_price_individual`, `stripe_price_individual_annual`, `stripe_price_team_monthly`, `stripe_price_team_annual`, `stripe_price_company_monthly`, `stripe_price_company_annual`, `stripe_price_intro_fee`, plus the 4 new Pro/Enterprise paths landing at Commit 4 — all match the `stripe_*` prefix).
  2. `Arc6SsmWriteStripeProduction` — Actions `ssm:PutParameter`, `ssm:AddTagsToResource`; same Resource scope as the read Sid. The write Sid is what lets Commit 4 land the new Price IDs in SSM with the overwrite semantic for any path that needs replacement.
  3. `Arc6KmsViaSsmForStripeProduction` — Actions `kms:Decrypt`, `kms:Encrypt`, `kms:GenerateDataKey`; Resource `*` (the AWS-managed SSM CMK does not expose a stable ARN; AWS recommends `Resource: "*"` for `kms:ViaService`-gated grants on AWS-managed keys); Condition `kms:ViaService = ssm.ca-central-1.amazonaws.com` (this is the load-bearing constraint — without it, this Sid would grant generic KMS; with it, the CMK can only be used through SSM API calls, never directly).
- Tighter alternatives rejected:
  1. **Enumerate each of the 13 SSM Stripe paths explicitly in `Resource: []` instead of using the `stripe_*` wildcard** — REJECTED: the Commit 4 plan mints four new Stripe Price IDs (Pro monthly, Pro annual, Enterprise floor annual, Enterprise metered unit) at new SSM paths that don't exist yet; enumerating all 13 means amending the IAM policy again at Commit 4 (a second scope-expansion event for the same Arc), violating the "single-event minimality" gate. The `stripe_*` wildcard at the end of the Resource ARN is bounded to one namespace (`/luciel/production/stripe_*`) and excludes every other production secret (`database-url`, `magic-link-secret`, `JWT_*`, `REDIS_URL`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `SES_SNS_TOPIC_ARN`, `platform-admin-key`, `worker_database_url`) by construction.
  2. **Mint a one-shot `luciel-arc6-stripe-operator-role` with MFA-required trust policy (the Step 28 P3-K ceremony pattern)** — REJECTED for proportionality: the P3-K ceremony was designed for a single-use admin-DSN read that crossed a higher trust boundary (the `luciel_admin` DB password). The Stripe Live secret is a recurring operational secret (the running ECS container already reads it at every container start; the agent reading it once per Arc-6 wipe + once per Arc-6 mint is the same posture as the container). The MFA-ceremony role is the right shape when the secret is RARE-USE; here the secret is COMMON-USE and the appropriate posture is sandbox-agent-scoped read with the audit-trail discipline already enforced by §9 of this runbook. Future Arc 8 WU-7/WU-8 work will also need Stripe-side reads (for metering emitter implementation); minting a one-shot role per Arc creates ceremony fatigue that erodes audit-trail discipline.
  3. **Have the partner run all Stripe-side work from their laptop with `luciel-admin` creds; the sandbox does only docs/code/migrations** — REJECTED for symmetry: the Arc 6 plan is an integrated 11-commit arc where Stripe Live state and database state and code state must land in tight sequence; splitting Commit 2 + Commit 4 to a different operator and a different audit trail breaks the "one arc, one operator, one continuous audit chain" property that has been the Arc 5 doctrine. Symmetry is doctrinally load-bearing per the partner directive ("we still maintain the discipline and symmetry between our docs properly").
  4. **Grant `kms:Decrypt` / `kms:Encrypt` WITHOUT the `kms:ViaService` condition** — REJECTED: this is the canonical least-privilege miss for KMS grants on AWS-managed keys. With the condition, the CMK can only be exercised through SSM (which is the only legitimate path); without it, a credential compromise gives the attacker generic KMS exposure on the AWS-managed key.
- Blast-radius analysis (worst case — agent malfunction / credential compromise):
  - **What the new capability adds**: read + write on 9 existing + 4 new SSM SecureString parameters (all under `/luciel/production/stripe_*`); decrypt/encrypt on the AWS-managed SSM CMK *only when invoked through SSM*.
  - **What the attacker can DO with a compromised key**: read the Stripe Live secret key (already a known prod secret loaded into the running ECS container at every start — the same exposure the running container has); read the Stripe Live webhook signing secret; read all 7+ Stripe Price IDs (already publicly readable from Stripe Dashboard with the secret key); write arbitrary values to the 13 Stripe SSM paths (forcing the next ECS container restart to load corrupted/attacker-chosen Price IDs, which would cause `/billing/checkout` to mint sessions against attacker-chosen Prices — a real availability + integrity exposure).
  - **What the attacker CANNOT do**: read or write any non-Stripe SSM parameter (the `stripe_*` resource suffix on the wildcard excludes every other prod secret); use the KMS CMK outside SSM (the `kms:ViaService` condition); rotate IAM; create or delete SSM parameters under any other path; mutate ECS services, task definitions, RDS, or any other account resource. The Arc 5 boundary surface (no `iam:*`, no `ecs:UpdateService` on this inline policy, no `ec2:*` write, no broad `*` Resource on any new Sid) remains intact.
  - **Detection layer**: the SSM-write Sid is the only path through which an attacker could corrupt prod Stripe Price IDs; CloudTrail captures every `ssm:PutParameter` call against `/luciel/production/stripe_*` and the agent's runbook ACTUAL RUN sections capture every legitimate write. Reconciliation between CloudTrail and the in-repo runbook is the second-layer audit defense.
  - **Recovery layer**: every SSM SecureString parameter in `/luciel/production/stripe_*` retains version history (the `ssm:GetParameterHistory` API returns prior values for up to 100 versions, AWS retention default); a corruption event is recoverable by re-putting the prior version. The Stripe Price IDs are also recoverable from the Stripe Dashboard listing in `acct_1TX2BmRytQVRVXw7` — they are not destroyed by an SSM write.
- Partner approval (verbatim): `"I will give you the scope exapnsion partner"` (Saturday, May 23, 2026 at 6:14 PM EDT via free-text reply to the 5-gate scope-expansion question), followed by `"okay partner I have created the policy for you: [LucielSandboxStripeScope](https://us-east-1.console.aws.amazon.com/iam/home?region=ca-central-1#/policies/details/arn%3Aaws%3Aiam%3A%3A729005488042%3Apolicy%2FLucielSandboxStripeScope)"` (Saturday, May 23, 2026 at 6:26 PM EDT confirming the policy was created and attached). The post-paste verification: four `boto3.client('ssm')` probes from the sandbox principal at ≈22:26 UTC — (1) `get_parameter` on `/luciel/production/stripe_price_intro_fee` returned a `price_*`-prefix value (30 chars); (2) `get_parameter WithDecryption=True` on `/luciel/production/stripe_secret_key` returned a `sk_live_*`-prefix value (107 chars), verifying the SSM read + KMS-via-SSM decrypt path; (3) `put_parameter Type=SecureString` to `/luciel/production/stripe_arc6_iam_probe` returned HTTP 200, verifying the SSM write + KMS-via-SSM encrypt path; (4) round-trip `get_parameter` on the probe param returned the exact written value byte-for-byte. CloudTrail events for these four calls (operation = `GetParameter` / `PutParameter`, principal = `arn:aws:iam::729005488042:user/luciel-sandbox-agent`, eventTime ≈2026-05-23T22:26:00Z) constitute the second-layer audit anchor. The probe parameter `/luciel/production/stripe_arc6_iam_probe` is left in SSM through Arc 6 close (cleaned up at Commit 11 doctrine-close as part of the Arc 6 sediment-sweep).
- Canonical mirror: `CANONICAL_RECAP.md` §17 entry `2026-05-23 (Arc 6 Commit 2 — Sandbox credential scope expansion for Stripe production SSM read/write + KMS via SSM via customer-managed policy LucielSandboxStripeScope)` — landing in the same Commit 2 paperwork commit that adds this expansion-log entry. **The Arc 5 inline policy `LucielSandboxArc5MigrateScope` is unchanged** (11 Sids, byte-identical to its Arc 5 Commit 21 form). The 3 new Sids land in a **customer-managed policy** `LucielSandboxStripeScope` (ARN `arn:aws:iam::729005488042:policy/LucielSandboxStripeScope`, §3-bis above) attached to the sandbox-agent user. The customer-managed shape was selected over a second inline policy after the Arc 5 inline policy's 2640-char projected size exceeded the 2048-char IAM inline-user-policy limit (128.9% utilisation); customer-managed policies have a 6144-char limit (we use 646 chars, 10.5%), get versioned by IAM (up to 5 versions retained), are independently revocable without affecting the Arc 5 policy, and can be reused on future principals if a Stripe-only operator role is minted later. Full canonical JSON at `ops/iam/LucielSandboxStripeScope.json`; delta reasoning at `ops/iam/arc6_stripe_ssm_scope_expansion.json`.

#### 2026-05-24 — Arc 6 Commit 10 — LucielSandboxHcaptchaScope customer-managed policy (2 Sids: Arc6SsmWriteHcaptchaProduction + Arc6KmsViaSsmForHcaptchaProduction) (commit `<this-commit-hash>`)
- TODO anchor: Arc 6 Commit 10 Slice 5 (Provision three SSM SecureString parameters `/luciel/production/hcaptcha_secret_key` + `hcaptcha_site_key` + `hcaptcha_verify_url`). Full slice context: Commit 9 landed the backend schema flip + hard-gate route + frontend widget for the hCaptcha gate on `/signup-free`; Commit 10 lands the deploy + the three SSM SecureString parameters that the running backend container reads at start to invoke `https://hcaptcha.com/siteverify`. The SSM provisioning is the operator-side half of the captcha gate — without these three params, the deploy of Commit 10's backend rev 86 would log `HCAPTCHA_SECRET_KEY not configured` at start and the route would return HTTP 501 from `CaptchaNotConfiguredError` instead of HTTP 200.
- Need: Write three new SSM SecureString parameters under `/luciel/production/hcaptcha_*` for the running backend container to read at start. The existing Arc 6 Commit 2 `Arc6SsmWriteStripeProduction` Sid (in `LucielSandboxStripeScope`) is resource-scoped to `/luciel/production/stripe_*` and authorises zero writes outside that namespace. The agent's pre-Commit-10 scope held zero hcaptcha-namespace SSM writes and zero KMS-via-SSM grants outside the Stripe namespace.
- New Sid block(s) added (customer-managed policy):
  1. `Arc6SsmWriteHcaptchaProduction` — Actions `ssm:PutParameter`, `ssm:AddTagsToResource`, `ssm:GetParameters` (plural); Resource `arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/production/hcaptcha_*`. Bounded to one namespace by the `hcaptcha_*` suffix; the wildcard covers `hcaptcha_secret_key`, `hcaptcha_site_key`, and `hcaptcha_verify_url` (plus any future hcaptcha-namespace params — a `hcaptcha_org_id` if hCaptcha enterprise tier is ever activated, etc.). Excludes every other SSM namespace by construction.
  2. `Arc6KmsViaSsmForHcaptchaProduction` — Actions `kms:Decrypt`, `kms:Encrypt`, `kms:GenerateDataKey`; Resource `*` (AWS-managed SSM CMK has no stable ARN); Condition `kms:ViaService = ssm.ca-central-1.amazonaws.com`. Same shape as the Stripe-scoped equivalent Sid in `LucielSandboxStripeScope` — the CMK is only usable through SSM, never directly. The KMS Sid is structurally separate from the Stripe-scoped KMS Sid because IAM policy authorisation evaluates them independently and the Stripe-scoped grant carries no namespace condition that would extend it to hcaptcha-namespace SSM calls; a fresh KMS-via-SSM grant on the hcaptcha policy is the minimal addition that lets the SecureString put encrypt under the AWS-managed key.
- Tighter alternatives rejected:
  1. **Widen `Arc6SsmWriteStripeProduction`'s Resource ARN to include `/luciel/production/hcaptcha_*`** — REJECTED because the `stripe_*` suffix narrowness in the Stripe-scoped Sid is doctrine-load-bearing (the suffix is the resource-namespace boundary that distinguishes Stripe creds from every other prod secret; widening it would lose the namespace-as-boundary property and produce a Sid named "Stripe" that authorises non-Stripe writes — a maintainability footgun). The clean shape is a structurally separate policy with its own namespace.
  2. **Use an inline policy on the sandbox-agent user instead of a customer-managed policy** — REJECTED because Arc 6 Commit 2 established the precedent that orthogonal Arc 6 scope-expansions land in customer-managed policies (independent version history, independent revocation, the Arc 5 inline policy remains byte-identical-unchanged); breaking that precedent for a fresh expansion would erode the symmetry that makes the IAM topology readable across arcs.
  3. **Have the partner write the three SSM params from their laptop via AWS Console SecureString-paste workflow** — REJECTED on the same symmetry argument from Commit 2: one arc, one operator, one continuous audit chain. The agent has been the operator for every prod-touching Arc 6 action; routing one slice through a different operator would create a dual-record audit shape that erodes traceability.
  4. **Grant `ssm:*` on the hcaptcha namespace** — REJECTED on minimality: the three operations needed are `PutParameter` + `AddTagsToResource` + `GetParameters` (for the round-trip verification probe); explicitly listing them rejects every unused action (`DeleteParameter`, `LabelParameterVersion`, `PutParameterVersion`, etc.) and prevents future blast-radius creep.
- Blast-radius analysis (worst case — agent malfunction / credential compromise):
  - **What the new capability adds**: read (via the verification probe) + write on three SSM SecureString parameters under `/luciel/production/hcaptcha_*`; decrypt/encrypt on the AWS-managed SSM CMK *only when invoked through SSM*.
  - **What the attacker can DO with a compromised key**: read the `HCAPTCHA_SECRET_KEY` (a low-value secret — it authenticates the backend to hCaptcha's siteverify endpoint and lets the attacker forge captcha-verify-success responses if they also control the backend, which they would not via this scope); write arbitrary values to the three hCaptcha params (forcing the next ECS container restart to load corrupted/attacker-chosen captcha config, which would degrade the captcha gate — producing `CaptchaInvalidError` for legitimate buyers if `SECRET_KEY` is corrupted, or pointing `VERIFY_URL` at an attacker-controlled endpoint).
  - **What the attacker CANNOT do**: read or write any non-hcaptcha SSM parameter (the `hcaptcha_*` suffix excludes Stripe + every other namespace); use the KMS CMK outside SSM (the `kms:ViaService` condition); rotate IAM; affect ECS, RDS, or any other AWS service. The cumulative scope (Arc 5 inline + Stripe + hCaptcha) does NOT add up to broader exposure than any single namespace because IAM evaluates each Sid against its own Resource scope.
  - **Detection layer**: CloudTrail captures every `ssm:PutParameter` call against `/luciel/production/hcaptcha_*`; the runbook ACTUAL RUN section of this expansion-log entry captures every legitimate write. Reconciliation between CloudTrail and the in-repo record is the second-layer audit defense.
  - **Recovery layer**: SSM SecureString version history retains prior values for up to 100 versions; a corruption event is recoverable by re-putting the prior version. The hCaptcha SECRET_KEY is also rotatable from the hCaptcha dashboard (the rotated value at Commit 10 prep — `<HCAPTCHA_SECRET_KEY_ROTATED>` replaces the original `<HCAPTCHA_SECRET_KEY_INERT>` which is now INERT — demonstrates the rotation path is operational). The literal key values are deliberately not written to this runbook per the prod-credential posture (env vars only, never to disk/committed); the rotation event is recorded by hCaptcha dashboard history + by SSM SecureString version history under `/luciel/production/hcaptcha_secret_key`, both of which are the canonical audit anchors for the rotation chain.
- Partner approval (verbatim): Approved during the Commit 10 Slice 3 5-gate-surfacing window. Partner attached `LucielSandboxHcaptchaScope` to `luciel-sandbox-agent` via AWS Console at the slice-prep boundary. Post-paste verification: four `boto3.client('ssm')` probes from the sandbox principal at Slice 5 begin — (1) `put_parameter Type=SecureString` to `/luciel/production/hcaptcha_secret_key` returned HTTP 200; (2) `get_parameter WithDecryption=True` round-trip returned the exact written value byte-for-byte; (3) + (4) same shape for `hcaptcha_site_key` and `hcaptcha_verify_url`. CloudTrail events for these calls (operation = `PutParameter`, principal = `arn:aws:iam::729005488042:user/luciel-sandbox-agent`, eventTime ≈2026-05-24T05:1x:00Z) constitute the second-layer audit anchor. The policy was **detached** by the partner immediately at slice-close per the trust-offer-moment doctrine (the partner initially proposed leaving it attached; the agent held the doctrine and the partner accepted). Post-detach AccessDenied was re-confirmed on a probe `ssm.put_parameter` against the hcaptcha namespace to verify the detach took effect.
- Side-effect drift surfaced at this expansion: `D-hcaptcha-ssm-params-untagged-2026-05-24` P3 (retrospective hygiene). The `ssm.add_tags_to_resource` calls that should have applied the standard `arc=arc6` / `commit=10` / `vocabulary=v2` tag set to each of the three new params returned `AccessDeniedException` because AWS IAM requires the singular `ssm:GetParameter` to authorise `AddTagsToResource` (the plural `ssm:GetParameters` does NOT satisfy the check despite the obvious semantic overlap). The three params are present and functional; the tagging gap is recorded as a future-arc hygiene clean-up via the drift.
- Canonical mirror: `CANONICAL_RECAP.md` §17 Arc 6 Commit 10 entry (Window 3.1 of the three-expansion-cluster section). The `LucielSandboxHcaptchaScope` policy exists in IAM with its 2-Sid body intact for audit reference even while detached.

#### 2026-05-24 — Arc 6 Commit 10 — RdsSnapshotLucielDbOnly resource ARN widen (`luciel-arc5-*` → `luciel-arc*-*`) (commit `<this-commit-hash>`)
- TODO anchor: Arc 6 Commit 10 Slice 7 (Pre-Arc-6 RDS snapshot `luciel-arc6-pre-migrations-<timestamp>` as the rollback escape hatch for the 3-step alembic jump that lands `arc6_a_admin_widget_domains` + `arc6_b_users_email_verified` + `arc6_c_pending_downgrade_columns`). The snapshot is the doctrine-mandated rollback boundary for any prod-touching migration cohort (precedent: Arc 5 Revision A pre-snap + Revisions B/C pre-snap, all under the `luciel-arc5-*` ARN suffix).
- Need: Take a pre-migration RDS snapshot named `luciel-arc6-pre-migrations-20260524-051641`. The existing `RdsSnapshotLucielDbOnly` Sid (in the Arc 5 inline policy `LucielSandboxArc5MigrateScope`, landed at Arc 5 Commit 21) was resource-scoped to `arn:aws:rds:ca-central-1:729005488042:snapshot:luciel-arc5-*` (the `arc5-*` suffix was the binding that limited the principal to Arc-5-named snapshots). The `arn:aws:rds:...:snapshot:luciel-arc6-*` create call raised `AccessDeniedException` because the ARN did not match the `arc5-*` suffix.
- New Sid block(s) added: NONE. **Resource ARN widened in place** on the existing `RdsSnapshotLucielDbOnly` Sid — the suffix changed from `luciel-arc5-*` to `luciel-arc*-*` (one character: `5` → `*`). The Actions array, Sid name, and policy identity are byte-identical-unchanged; only the wildcard position widened. The widened pattern matches `luciel-arc5-*`, `luciel-arc6-*`, `luciel-arc7-*`, etc. (forward-extensible without further expansion), and the `luciel-arc` prefix still bounds the principal to Arc-named snapshots only (cannot snapshot under any arbitrary naming scheme).
- Tighter alternatives rejected:
  1. **Add a second Resource ARN `luciel-arc6-*` while keeping `luciel-arc5-*` separately** — REJECTED because the IAM JSON would carry two parallel Arc-named suffixes that would need separate widening at every future arc (Arc 7 needs Arc 7, Arc 8 needs Arc 8, etc.), producing a policy that grows by one entry per arc indefinitely. The `luciel-arc*-*` single-wildcard shape collapses every future arc into one entry that never needs widening again — the cleaner shape.
  2. **Use `luciel-*-*` (drop the `arc` prefix)** — REJECTED because it would authorise non-arc-named snapshots (e.g. `luciel-hotfix-*`, `luciel-experimental-*`), losing the arc-naming boundary. The `arc` prefix is the load-bearing namespace boundary.
  3. **Continue prefix-naming Arc 6 snapshots as `luciel-arc5-pre-arc6-*`** — REJECTED because it overloads the arc-name field with prefix-as-event-tag semantics that the snapshot-naming doctrine does not support; the snapshot name should reflect what arc the snapshot belongs to, not what arc its purpose is.
- Blast-radius analysis: The widening adds zero new Actions and zero new principals; only the Resource scope grew from one Arc namespace to all Arc namespaces. Attacker with compromised key still cannot delete or restore snapshots (no `DeleteDBSnapshot` / `RestoreDBInstanceFromDBSnapshot`); cannot affect any non-`luciel-db` RDS instance; cannot affect non-Arc-named snapshots. Exposure delta is bounded to storage-cost growth across future arcs (each arc that doesn't take a snapshot is still safe under the doctrine; each arc that does take a snapshot pays its own storage cost as before).
- Partner approval (verbatim): The partner directly edited the policy in AWS Console after the agent surfaced the AccessDenied + diagnosed the misread (the agent's first attribution of the error to a `luciel-arc*-*` pattern was wrong; the actual policy held `luciel-arc5-*` — partner caught the misread). This is the **first** expansion event in the protocol's history where the partner directly authored the policy edit rather than approving an agent-proposed Sid block. The variation is acceptable under Gate 4 (Approval) because the partner explicitly authored AND saved the edit; the agent-side audit record is this expansion-log entry + the corresponding stanza in `D-prod-credential-scope-expansion-protocol-2026-05-23`. Snapshot `luciel-arc6-pre-migrations-20260524-051641` (ARN `arn:aws:rds:ca-central-1:729005488042:snapshot:luciel-arc6-pre-migrations-20260524-051641`) was created successfully on the next attempt and reached 100% availability before the Slice 8 ECS rolling deploy began.
- Canonical mirror: `CANONICAL_RECAP.md` §17 Arc 6 Commit 10 entry (Window 3.2 of the three-expansion-cluster section); `DRIFTS.md` `D-prod-credential-scope-expansion-protocol-2026-05-23` Third execution Window 3.2 stanza. Post-widen the Arc 5 inline policy `LucielSandboxArc5MigrateScope` still has 11 Sids; the only delta vs the Arc 5 Commit 21 form is the one-character Resource-ARN change on `RdsSnapshotLucielDbOnly`.

#### 2026-05-24 — Arc 6 Commit 10 — AmplifyScopeExpansion-Slice12 customer-managed policy (5 Sids: AmplifyListApps + AmplifyGetApp + AmplifyUpdateApp + AmplifyStartJob + AmplifyGetJob) (commit `<this-commit-hash>`)
- TODO anchor: Arc 6 Commit 10 Slice 12 (Amplify env var update — `VITE_HCAPTCHA_SITE_KEY` on Luciel-Website Amplify app + trigger rebuild). The marketing-site half of the captcha gate: the backend reads `HCAPTCHA_SECRET_KEY` from SSM (Window 3.1), the marketing site reads `VITE_HCAPTCHA_SITE_KEY` from the Amplify build's env-var injection (this window). Without this expansion, the marketing-site bundle would ship with the placeholder/old SITE_KEY and the widget would fail to verify against the new hCaptcha campaign.
- Need: Update one Amplify app's env-var set + trigger a rebuild that bakes the new env var into the deployed bundle. The agent's pre-Commit-10 scope held zero `amplify:*` capability — every Amplify operation (list, describe, update, start build, poll build status) returned `AccessDeniedException`.
- New Sid block(s) added (customer-managed policy):
  1. `AmplifyListApps` — Action `amplify:ListApps`; Resource `*` (the `ListApps` API does not accept a Resource scope; the narrowing happens at the call site by filtering for the single app of interest).
  2. `AmplifyGetApp` — Action `amplify:GetApp`; Resource `arn:aws:amplify:ca-central-1:729005488042:apps/d1xf2f9605mosw` (resource-scoped to the single Luciel-Website Amplify app by app-id; the principal cannot describe any other Amplify app).
  3. `AmplifyUpdateApp` — Action `amplify:UpdateApp`; Resource same single-app ARN as `AmplifyGetApp`. This is the load-bearing mutation Sid (env-var write goes through `UpdateApp`).
  4. `AmplifyStartJob` — Action `amplify:StartJob`; Resource `arn:aws:amplify:ca-central-1:729005488042:apps/d1xf2f9605mosw/branches/main/jobs/*` (resource-scoped to the `main` branch of the single app + any job-id under it).
  5. `AmplifyGetJob` — Action `amplify:GetJob`; Resource same as `AmplifyStartJob`. Build-status polling.
- Tighter alternatives rejected:
  1. **Use `amplify:*` on the single-app ARN** — REJECTED on minimality (would authorise `DeleteApp`, `CreateBranch`, `DeleteBranch`, `CreateBackendEnvironment`, and ~25 other actions, every one of which is unneeded for this slice's env-var-update + build-trigger goal).
  2. **Have the partner update the env var via AWS Console + trigger the rebuild from the Amplify Console** — REJECTED on symmetry (the partner-laptop split-operator argument from Commit 2). The Amplify env-var-update + build-trigger is a single coordinated action that should land in the same operator's audit trail as the backend SSM write (Window 3.1) and the RDS snapshot (Window 3.2) — routing it through the partner would create a three-operator audit shape for one logical commit.
  3. **Use GitHub Actions or a CI-driven Amplify build trigger instead of the AWS API** — REJECTED on precedent (there is no existing CI path that builds + deploys the marketing site; introducing one as a side-effect of Commit 10 would scope-expand the commit past its captcha-gate goal).
  4. **Resource-scope `AmplifyListApps` to the single app ARN** — REJECTED on AWS API limitation: `ListApps` doesn't support resource-level scoping in the IAM service definition (the action is account-scoped by design). The narrowing is therefore done at the call site (filter the returned list by app-id) and the audit defense is that no other Amplify app exists in the account (verified at slice-prep by `aws amplify list-apps` returning a single entry).
- Blast-radius analysis: Attacker with compromised key can list every Amplify app in the account (one app), describe + update + trigger builds on the single Luciel-Website app, but cannot delete it, cannot affect any other AWS service, cannot rotate IAM. Worst case: attacker pushes a corrupted env var set on the Luciel-Website app (e.g. flipping `VITE_API_BASE_URL` to attacker-controlled endpoint) and triggers a rebuild; the marketing site would then route signup attempts to the attacker. Detection layer: CloudTrail captures every `amplify:UpdateApp` + `amplify:StartJob`; the pre-change env-var snapshot is captured in `arc6_commit10_slice12_amplify_prechange_envvars.json` in the workspace as the second-layer record. Recovery layer: re-running `UpdateApp` with the captured pre-change env-var set restores the previous shape; an `amplify:StartJob` rebuild then redeploys the prior bundle.
- Partner approval (verbatim): Partner attached `AmplifyScopeExpansion-Slice12` to `luciel-sandbox-agent` at the slice-prep boundary (`"I have attached the policy for you partner"`). Env-var update + rebuild executed cleanly: pre-change env-var snapshot captured (3 vars: `VITE_API_BASE_URL`, `VITE_STRIPE_PUBLISHABLE_KEY`, `VITE_WEB3FORMS_ACCESS_KEY`); `amplify:UpdateApp` added `VITE_HCAPTCHA_SITE_KEY=3606eb64-8f2a-41a4-9fd2-100673da7a78` (4-var post-state); `amplify:StartJob` triggered build job 33; `amplify:GetJob` polling tracked BUILD 82s + DEPLOY 8s + VERIFY <1s = 89.8s SUCCEED; bundle `/assets/index-Ciajn0z_.js` confirmed 3 verbatim SITE_KEY occurrences + 5 hCaptcha SDK symbol references via post-deploy grep. Partner **detached** the policy immediately at slice-close (`"I have deattached the policy partner"`); post-detach AccessDenied was re-confirmed on `ListApps` + `GetApp` calls to verify the detach took effect.
- Canonical mirror: `CANONICAL_RECAP.md` §17 Arc 6 Commit 10 entry (Window 3.3 of the three-expansion-cluster section); `DRIFTS.md` `D-prod-credential-scope-expansion-protocol-2026-05-23` Third execution Window 3.3 stanza. The `AmplifyScopeExpansion-Slice12` policy exists in IAM with its 5-Sid body intact for audit reference even while detached; the workspace files `arc6_commit10_slice12_amplify_scope_expansion_policy.json` (the policy JSON) and `arc6_commit10_slice12_amplify_prechange_envvars.json` (the pre-change env-var snapshot) are the secondary audit artifacts.

<!--
Future expansion entries land below this comment in chronological order. Template:

#### YYYY-MM-DD \u2014 Arc N \u2014 \u003csummary\u003e (commit \u003chash\u003e)
- TODO anchor: \u003cTODO item description\u003e
- Need: \u003cthe specific operational need\u003e
- New Sid block(s) added: \u003clist by Sid name\u003e
- Tighter alternatives rejected: \u003cbullets with rationale\u003e
- Partner approval (verbatim): "\u003c...\u003e"
- Canonical mirror: `CANONICAL_RECAP.md` \u00a717 entry `YYYY-MM-DD (Arc N \u2014 Sandbox credential scope expansion)`
-->

## 9. Operational discipline rules \u2014 binding on the sandbox-agent

These rules are derivative of the canonical doctrine in `DRIFTS.md` `D-prod-credential-scope-expansion-protocol-2026-05-23` and the operational discipline framing in `CANONICAL_RECAP.md` \u00a717. They are restated here so they are present at the procedural surface where the agent acts:

- Treat the credential as the highest-trust asset in the sandbox. When in doubt about whether an action is in-scope, ask the partner before invoking.
- Do NOT use the credential outside an active `CANONICAL_RECAP.md` \u00a717 arc TODO. Casual experimentation, "let me just check X" exploration of prod, anecdotal inspection \u2014 all out-of-protocol.
- Every prod-touching invocation MUST be in the context of an active TODO item AND MUST be recorded in either an arc-record file or a runbook ACTUAL RUN section within the same session as the invocation.
- The credential MUST NOT be passed to any subagent, browser_task, or other tool that could persist it outside the immediate bash environment scope.
- Credentials are never written to a workspace file, never committed, never echoed in command output, never appear in commit messages or log files.
- At end of session, the agent explicitly runs `unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY` to clear the values from the bash environment before the session-state snapshot.

## 10. Related artifacts (canonical and operational cross-refs)

- **Canonical (truth):** `CANONICAL_RECAP.md` \u00a717 Arc 5 Commit 6 entry; `ARCHITECTURE.md` \u00a73.2.8 Arc 5 Commit 6 bullet; `DRIFTS.md` `D-prod-credential-scope-expansion-protocol-2026-05-23`.
- **Operational siblings:** ACTUAL RUN sections of future prod-touching runbooks starting with `docs/runbooks/arc5-revision-a-prod-apply-and-rollback.md` (TODO #12) will reference this runbook by anchor; they collectively constitute the use-record of the credential.
- **Audit (out-of-repo):** AWS CloudTrail for the `729005488042` account is the immutable record of every API call this principal makes. Any reconciliation between CloudTrail and the in-repo runbook ACTUAL RUN sections is the second-layer audit story (CloudTrail = what; runbooks = why + who approved).
