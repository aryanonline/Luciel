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
    }
  ]
}
```

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

No expansions yet. The policy at Arc 5 Commit 6 IS the baseline.

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
