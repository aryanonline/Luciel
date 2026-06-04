# Section 06 — Infrastructure-as-Code, Schema/Migrations, CI/CD, Smoke Probes, Residue

**Audit scope:** CloudFormation stacks (`cfn/*.yaml`), IAM artefacts (`infra/iam/`, `iam/`), Alembic migrations (`alembic/versions/`), CI workflows (`.github/workflows/ci.yml`, `widget-e2e.yml`), smoke-probe directory (`ci/e2e/`), and all residue files in the backend repository root.
**Source-of-truth sections read:** Architecture §4.1, §4.2, §4.3, §5.1, §5.5, §5.9, §9; ARC15_BACKEND_REPORT.md; ARC15_DRIFT_CLEANUP_REPORT.md; ARC17_LOOKUP_RECORD_AMENDMENT.md.
**Method note:** All findings are from static analysis of repo files. Live AWS control-plane state is BLOCKED-EXTERNAL (see dedicated subsection). Path-drift rule applied: marked MISSING only when functionality is absent, not just when a path differs from §8 ideal paths.

---

## 12-Line Headline Summary

1. **IaC footprint is deeply incomplete vs §4.3 doctrine** ("all infrastructure defined in CloudFormation; no console-provisioned resources in production"). Core compute (ECS task definitions/services), database (RDS), cache (ElastiCache), networking (VPC, subnets, security groups), load balancer (ALB), and Secrets Manager are all **absent from `cfn/`** — DRIFTED vs §4.3.
2. **Naming gap throughout**: every declared artefact uses `luciel-*` names; §4.1 specifies `vm-control-plane`, `vm-data-plane`, `vm-knowledge-staging`, `vm-transcript-archive`, `vm-export-bundles` — flag is AMBIGUOUS/DRIFTED; no `vm-` resource exists anywhere in the repo.
3. **Canadian data residency** is architecturally present (all cfn stacks target ca-central-1) but the **region-assertion Conditions** required by §4.2 ("CloudFormation stacks have Conditions that assert the target region is ca-central-1 and will fail deployment outside that region") are MISSING from all cfn stacks except one partial case in `luciel-widget-cdn.yaml` (which uses a Condition for OIDC, not region assertion). The `aws:RequestedRegion ≠ ca-central-1` S3 Deny policies are also MISSING from both the knowledge bucket and widget CDN bucket policies.
4. **Staging environment does NOT exist** in the repository. Zero staging CFN stacks, zero staging parameter files, zero `/luciel/staging/` SSM path prefixes. Only `/luciel/production/` SSM paths are referenced. This is DRIFTED vs §4.3 ("dev → staging → prod pipeline declared"). The founder's statement that no staging environment exists is confirmed by repo evidence.
5. **Alembic chain is linear and clean**: 130 migrations, single root (`17ab56bdd913`), single head (`arc18_conversation_budget_metering`), zero branch points, zero missing `downgrade()` functions. CONFORMS to §5.9.
6. **RLS coverage**: all 9 doc-listed §3.7.2b tables now have ENABLE ROW LEVEL SECURITY in migrations (knowledge_chunks under old name `knowledge_embeddings`; `transcripts` and `session_summaries` do not exist as DB tables — doc names differ from actual schema). CONFORMS with an AMBIGUOUS note.
7. **CI/CD** runs AST/unit tests and a widget e2e harness but contains **no ECS rollout, no smoke probe invocation, and no automatic rollback** wired into the workflow. DRIFTED vs §4.3 rollback posture and §5.5 smoke probe mandate.
8. **§5.5 smoke probes** (`widget_e2e`, `escalation_gate`, `budget_increment`, `connection_gate`, `knowledge_retrieval`, `audit_emit`) do **not exist as runnable scripts** in `ci/e2e/` or anywhere else. `ci/e2e/` contains only a widget stream assertion helper and a bash runner. MISSING.
9. **CloudWatch alarms** (`luciel-prod-alarms.yaml`) are present but the §5.1-mandated set (`DataPlane_ErrorRate_High`, `LLMPrimary_Degraded`, `BothLLMProviders_Down`, `BudgetGate_FreeCap_Anomaly`, `ConnectionHealth_Degraded`, `SmokeProbeFailed`) is MISSING — alarms present cover worker/RDS/SSM/audit-integrity but not the §5.1 DataPlane/LLM/budget/connection SLO alarms.
10. **Residue count**: 11 residue items identified — td-backend rev39/46/47/48/49 (5), td-worker rev19/33 (2), ARC report .md files in root (3), `app/domain/stubs/__init__.py` empty dir pair (2), infra/iam backup files (2), iam/ post-patch file (1). `td-backend-rev78` and `td-worker-rev34-arc11.json` are CURRENT/LIVE (referenced by ops runbook and arc11_close_audit.py respectively).
11. **S3 encryption drift**: §4.1 requires SSE-KMS on all S3 buckets; `cfn/knowledge-bucket.yaml` and `cfn/luciel-widget-cdn.yaml` both use SSE-S3 (AES256, AWS-managed key). The knowledge-bucket comment acknowledges "AWS-managed KMS at v1" — AMBIGUOUS vs §4.1 SSE-KMS spec.
12. **Status counts** — CONFORMS: 4 | DRIFTED: 6 | MISSING: 5 | RESIDUE: 11 items | AMBIGUOUS: 3 | BLOCKED-EXTERNAL: 3.

---

## Audit Table

| # | Requirement | Doc cite | Implementing artifact(s) | Status | Notes / evidence |
|---|---|---|---|---|---|
| 1 | ECS Fargate stacks for control-plane and data-plane task definitions | §4.1 | No cfn stack found; `td-backend-rev78.json` and `td-worker-rev33.json` are bare JSON task-def registers, not CFN stacks | DRIFTED | Task definitions registered manually/imperatively; CFN stack absent. Family names are `luciel-backend`, `luciel-worker` — not `vm-control-plane`, `vm-data-plane` as §4.1 requires |
| 2 | RDS PostgreSQL Multi-AZ + pgvector CFN stack | §4.1 | Absent from `cfn/` | DRIFTED vs §4.3 | No `AWS::RDS::DBInstance` or `DBCluster` resource anywhere in cfn/. Alarm yaml references an RDS DBInstanceIdentifier (`Default: luciel-postgres`) as a parameter, confirming RDS exists live but was provisioned outside CFN |
| 3 | ElastiCache Redis Multi-AZ CFN stack | §4.1 | Absent from `cfn/` | DRIFTED vs §4.3 | No `AWS::ElastiCache::*` resource in cfn/. Redis existence inferred from SSM `/luciel/production/REDIS_URL` in task defs |
| 4 | VPC / networking / ALB CFN stacks | §4.1, §4.3 | Absent from `cfn/` | DRIFTED vs §4.3 | `cfn/luciel-twilio-webhook-routing.yaml` explicitly acknowledges: "existing production ALB… provisioned in an earlier arc / the console, NOT in CloudFormation" (line 17). Console-provisioned ALB contradicts §4.3 "no console-provisioned resources in production" |
| 5 | Secrets Manager CFN stack | §4.1, §4.3 | Absent from `cfn/` | DRIFTED vs §4.3 | Secrets referenced via SSM ARNs in task defs; no CFN stack |
| 6 | S3 buckets: vm-knowledge-staging, vm-transcript-archive, vm-export-bundles with SSE-KMS + versioning + block-public | §4.1 | `cfn/knowledge-bucket.yaml` (`luciel-knowledge-prod-ca-central-1`); no CFN for transcript-archive or export-bundles | DRIFTED | (a) Bucket names use `luciel-*` not `vm-*` prefixes. (b) `cfn/knowledge-bucket.yaml:87` uses `SSEAlgorithm: AES256` (SSE-S3) not SSE-KMS as §4.1 specifies. (c) vm-transcript-archive and vm-export-bundles have no CFN stack at all. (d) knowledge-bucket versioning is Suspended (comment at line 14 explains the intent but §4.1 says versioning enabled on transcript-archive and export-bundles — those stacks don't exist) |
| 7 | CloudFront widget CDN | §4.1 | `cfn/luciel-widget-cdn.yaml` | CONFORMS | CloudFront distribution + S3 bucket + OAC + CI deploy role. CloudFront is global service; origin bucket in ca-central-1. SSE-S3 not SSE-KMS (see row 6 note) |
| 8 | KMS key per environment | §4.2, §3.8.3 | No cfn KMS stack | BLOCKED-EXTERNAL | §4.2 states "customer-managed KMS key (one key per environment)". No `AWS::KMS::Key` in cfn/. Whether a console-provisioned KMS key exists cannot be verified without AWS credentials |
| 9 | CloudFormation region Conditions asserting ca-central-1 (fail outside region) | §4.2 §1 | `cfn/*.yaml` — none have an `IsCanadaCentral` or equivalent Condition block | MISSING | §4.2 clause 1: "CloudFormation stacks have Conditions that assert the target region is ca-central-1 and will fail deployment outside that region." Reviewed all 8 CFN stacks. Only `luciel-widget-cdn.yaml` has a `Conditions:` block but it guards OIDC provider creation, not region assertion. All stacks use `Default: ca-central-1` parameters (soft default, not an enforcement Condition). No `AWS::CloudFormation::Guard` or `Rules` block found anywhere |
| 10 | S3 bucket Deny on aws:RequestedRegion ≠ ca-central-1 | §4.2 §2 | `cfn/knowledge-bucket.yaml:118–136`, `cfn/luciel-widget-cdn.yaml:275–303` | MISSING | Both bucket policies contain only a `DenyInsecureTransport` (non-TLS) statement. No `StringNotEquals: aws:RequestedRegion: ca-central-1` Deny statement exists in either bucket policy. §4.2 clause 2 explicitly requires this |
| 11 | No cross-region replication on any customer-data bucket | §4.2 §3 | `cfn/knowledge-bucket.yaml`, `cfn/luciel-widget-cdn.yaml` | CONFORMS | No `ReplicationConfiguration` block in any cfn stack. Neither bucket declares cross-region replication |
| 12 | dev → staging → prod pipeline declared | §4.3 | No staging stack, config, or SSM path in repo | MISSING | **Zero staging environment artefacts exist in the repo.** No cfn stack with a staging parameter, no `.env.staging`, no `/luciel/staging/` SSM prefix. Only `/luciel/production/` is referenced. Founder's statement confirmed: staging does not exist as a deployed environment or as declared IaC |
| 13 | All infrastructure in CloudFormation; no console-provisioned resources | §4.3 | `cfn/*.yaml` (partial) | DRIFTED | Core resources (ECS services, RDS, ElastiCache, VPC, ALB, Secrets Manager, ACM cert) are console-provisioned or imperatively registered (task defs via JSON). `cfn/luciel-twilio-webhook-routing.yaml:17` explicitly states the ALB "was provisioned in an earlier arc / the console, NOT in CloudFormation." Eight CFN stacks exist for peripheral/operational concerns (alarms, autoscaling, CDN, agent-policy, SES, verify-task-role, knowledge-bucket) — insufficient to satisfy §4.3 |
| 14 | Naming: vm-control-plane, vm-data-plane, vm-knowledge-staging, etc. | §4.1 | All artefacts use `luciel-*` prefix | AMBIGUOUS | §4.1 specifies `vm-*` names for task definitions and buckets; every repo artefact (task defs, cfn, ECR, SSM paths) uses `luciel-*`. Likely a brand/product rename (VantageMind → Luciel) that was not back-ported to §4.1 naming. No live `vm-*` resource exists or is referenced anywhere |
| 15 | Alembic single head | §5.9 | `alembic/versions/` — 130 migrations | CONFORMS | Single root (`17ab56bdd913`), single head (`arc18_conversation_budget_metering`), zero branch points (verified programmatically). Chain length = 130 |
| 16 | Expand-contract discipline | §5.9.1 | `alembic/versions/arc15_c_drop_system_prompt_additions.py` et al | CONFORMS | ARC15_DRIFT_CLEANUP_REPORT.md documents a live upgrade→downgrade→upgrade round-trip on the arc15_c migration. Column drop preceded by deploy that stopped reading it (ARC 15 WU1). Pattern is followed in reviewed migrations |
| 17 | Downgrade functions present in every migration | §5.9.1 | `alembic/versions/*.py` | CONFORMS | Programmatic scan of all 130 migration files: 0 missing `def downgrade()`. Every migration has a reversal |
| 18 | RLS on all §3.7.2b tenant tables | §3.7.2b | Various `arc9_*`, `arc11_*`, `arc12_*`, `arc14_*`, `arc15_*` migrations | CONFORMS (with note) | Tables confirmed: `knowledge_sources` (arc11_d1), `knowledge_chunks` (arc9_c3_3 — old name `knowledge_embeddings`; renamed via arc11_b), `instance_connections` (arc15_b), `sibling_call_grants` (arc12_wu4), `admin_audit_logs` (arc9_c3_1), `escalation_events` (arc14_u2), `leads` (arc14_u4), `sessions` (arc9_c3_2e), `instances` (arc9_c3_5d). Note: §3.7.2b lists `transcripts` and `session_summaries` — these table names do not exist in the migration chain; the conceptual data lives in `sessions`/`messages` under different names. FORCE ROW LEVEL SECURITY applied globally in arc9_c10_a |
| 19 | admin_audit_log append-only grants | §5.3, §5.9.1 | `arc9_c6_2_admin_audit_immutability.py`, `arc10_gap6_archiver_insert_grant.py` | CONFORMS | `arc9_c6_2` explicitly REVOKEs UPDATE and DELETE on `admin_audit_logs` from `luciel_app`. Arc10 gap6 grants INSERT only to `luciel_audit_archiver`. Confirmed in grep: "REVOKE UPDATE, DELETE ON admin_audit_logs FROM luciel_app" |
| 20 | CI/CD workflow exists and gates main branch | §4.3 | `.github/workflows/ci.yml` | CONFORMS (partial) | CI workflow runs on push to `main`/`arc*`/`step-*` branches and on PRs to `main`. Runs AST tests, unit tests, health check, shape guards, widget build+size gate, backend image build+push to ECR. Branch protection enforced (ci.yml comments confirm no PR merges with red CI) |
| 21 | Smoke probes (§5.5) exist as code and are invoked post-deploy | §5.5, §4.3 | `ci/e2e/` (README.md, assert_widget_stream.py, run_widget_e2e.sh) | MISSING | The six named probes (`widget_e2e`, `escalation_gate`, `budget_increment`, `connection_gate`, `knowledge_retrieval`, `audit_emit`) with their SLAs do not exist as runnable scripts anywhere. `ci/e2e/` contains only a widget stream E2E harness. No probe invocation exists in CI workflows. No rollback-on-failure trigger is wired in any CI job. The `widget-e2e.yml` workflow exercises widget SSE scenarios against a live backend but is not the §5.5 synthetic post-deploy probe set |
| 22 | §5.1 alarm set in `cfn/luciel-prod-alarms.yaml` | §5.1 | `cfn/luciel-prod-alarms.yaml` | DRIFTED | Alarms present: `luciel-worker-no-heartbeat`, `luciel-worker-unhealthy-task-count`, `luciel-worker-error-log-rate`, `luciel-rds-connection-count`, `luciel-rds-cpu`, `luciel-rds-free-storage`, `luciel-ssm-getparameter-failures`, `luciel-audit-log-integrity-breach`, `luciel-guc-leak-guard-violation`, `luciel-ops-role-connect-velocity`, `luciel-admin-audit-write-velocity`, `luciel-per-admin-http-5xx`, `luciel-per-admin-http-p99-latency`, `luciel-per-admin-zero-row`. **Not present**: `DataPlane_ErrorRate_High` (>1% 5xx over 5 min), `LLMPrimary_Degraded` (LLMFallbackRate >10%), `BothLLMProviders_Down` (ErrorRate=100% on both), `BudgetGate_FreeCap_Anomaly` (Free-tier spike), `ConnectionHealth_Degraded` (>5% connections error/expired), `SmokeProbeFailed` (any post-deploy probe failure triggers rollback). The §5.1 alarm set is focused on DataPlane SLO + LLM health + smoke-probe trigger; the deployed set focuses on worker liveness + RDS health + audit-integrity |
| 23 | `luciel-sandbox-agent-policy.yaml` — scoped IAM | Arc 9 D8.1 | `cfn/luciel-sandbox-agent-policy.yaml` | CONFORMS | Policy replaces AdministratorAccess with named-action scope per Doctrine D8.1. Explicit `DenySelfPrivilegeEscalation` (line 310) and `DenyAdminPolicyAttachment` (line 337) statements present. Write actions scoped to `luciel-*` ARNs; `Resource: "*"` appears only for list/read/iam-allow operations. CI test `tests/infra/test_c8_3_sandbox_agent_policy_shape.py` locks the shape at PR time. The task brief flags this as "broad agent policy" — the claim is inaccurate post-D8.1; it is scoped |
| 24 | Rollback posture: smoke probe failure → automatic ECS rollback | §4.3, §5.5 | No CI automation found | MISSING | Neither `ci.yml` nor `widget-e2e.yml` contains an `aws ecs update-service` rollback step, ECS deployment circuit breaker wiring, or any post-deploy smoke probe invocation. `worker-deployment-config.json` defines `deploymentCircuitBreaker.enable=true, rollback=true` for the Celery worker service — partial coverage for worker but absent for backend |

---

## CONFLICTS

### C1: §4.1 SSE-KMS vs. knowledge-bucket SSE-S3
**Evidence:** §4.1 table states "SSE-KMS encryption on all [S3 buckets]." `cfn/knowledge-bucket.yaml:87` declares `SSEAlgorithm: AES256` (SSE-S3 / AWS-managed encryption). `cfn/luciel-widget-cdn.yaml:182` also uses `AES256`. The knowledge-bucket comment (line 13) reads "SSE-S3 (AES256) — Architecture v1 §4.2: AWS-managed KMS at v1" — this is a developer interpretation not a doc-approved deferral. The widget CDN bucket is not a customer-data bucket so §4.1 SSE-KMS may not apply. **Finding**: §4.1 vs. implemented encryption is a genuine conflict for the knowledge bucket; no explicit §9 authored commitment covers this deferral.

### C2: §4.3 "all stacks version-controlled" vs. console-provisioned ALB/RDS/ECS
**Evidence:** §4.3 states "All infrastructure is defined in CloudFormation stacks… no console-provisioned resources in production." `cfn/luciel-twilio-webhook-routing.yaml:17-29` explicitly documents the ALB was provisioned "in an earlier arc / the console, NOT in CloudFormation." The ARC15 cleanup report makes no mention of closing this gap. This is a known architectural debt that pre-dates the audit but has not been remediated or formally deferred with §9 tracking.

### C3: §4.2 clause 1 vs. actual CFN Conditions
**Evidence:** §4.2 clause 1 says stacks "will fail deployment outside [ca-central-1]." No CFN stack contains a `Rules:` or `Conditions:` block that asserts `AWS::Region = ca-central-1`. All stacks use a `Default: ca-central-1` parameter — a soft default that can be overridden at deploy time, not a hard enforcement. This gap has no §9 authored commitment tracking it.

---

## §9 TOUCHED (Authored Commitments in this slice)

| §9 # | Commitment | Authored value | Found value |
|---|---|---|---|
| 1 | AWS production region | ca-central-1 | MATCHES — all cfn stacks, task defs, SSM paths, ECR registry target ca-central-1 (unratified) |

---

## RESIDUE DETAIL

### R1–R5: td-backend-rev39/46/46-asregistered/47/48/49.json
**What they are:** Historical snapshots of the `luciel-backend` ECS task definition at earlier revision numbers (rev39 through rev49). Generated by incremental build scripts (`scripts/build_rev49_taskdef.py` references rev48→rev49).
**Current registered TD:** `td-backend-rev78.json` is the current production-candidate task def (referenced in `ops/arc13_golive_runbook.md:15,44,312,349` as the live backend TD). Revisions 39/46/47/48/49 are stale historical snapshots.
**CI/script references:** `scripts/build_rev49_taskdef.py` reads `td-backend-rev48.json` as input — this script is itself a historical build aid, not a pipeline step. No CI workflow references any of these files. No `scripts/arc11_close_audit.py` reference.
**Dependency impact:** Safe to remove rev39/46/46-asregistered/47/48/49 from the repo. Git history preserves them. `scripts/build_rev49_taskdef.py` should be removed alongside (or moved to an archive branch) since its output (rev49) would also be gone.
**Safe-to-delete:** YES (rev39/46/47/48/49 + build script) — git-history-recoverable, no CI reads them.

### R6: td-backend-rev78.json
**What it is:** The current/live backend task definition registered at ECS revision 78.
**Status:** LIVE — referenced by `ops/arc13_golive_runbook.md` as the production task def. NOT residue.
**Note:** The family name `luciel-backend` diverges from §4.1's `vm-control-plane` — see AMBIGUOUS flag in row 14.

### R7–R8: td-worker-rev19.json, td-worker-rev33.json
**What they are:** Historical snapshots of the `luciel-worker` Celery task definition.
**Current registered TD:** No explicit "rev-current" marker in the repo for the worker. `td-worker-rev33.json` is the most recent non-arc11 worker TD in the root. `td-worker-rev34-arc11.json` is referenced by `scripts/arc11_close_audit.py:551` to validate KNOWLEDGE_S3_BUCKET wiring — it is LIVE/FUNCTIONAL.
**CI/script references:** rev19 and rev33 have zero references in scripts/ or .github/.
**Dependency impact:** Rev19 and rev33 are stale snapshots. Safe to remove. Rev34-arc11 must be retained while arc11_close_audit.py is in use.
**Safe-to-delete:** td-worker-rev19.json YES, td-worker-rev33.json YES — git-history-recoverable, zero live readers.

### R9: td-prod-ops-rev3.json
**What it is:** Task definition for `luciel-prod-ops` (one-shot DB migration runner).
**CI/script references:** `scripts/arc3_ecs_oneshot.ps1` explicitly reads `td-prod-ops-rev3.json` as a template to generate `td-prod-ops-rev4.json` at deploy time. This script is an Arc 3 one-shot; the arc it served is long closed.
**Current registered TD:** No evidence of a live `luciel-prod-ops` service. The migration-runner pattern was superseded; production migrations now use the `luciel-verify` task role.
**Dependency impact:** The arc3_ecs_oneshot.ps1 script is itself residue (Arc 3 is closed). Both the script and the JSON are historical. Safe to remove together.
**Safe-to-delete:** YES (td-prod-ops-rev3.json + scripts/arc3_ecs_oneshot.ps1) — git-history-recoverable.

### R10: td-worker-rev34-arc11.json
**What it is:** Current functional artefact. `scripts/arc11_close_audit.py:551` reads it to assert KNOWLEDGE_S3_BUCKET is wired.
**Status:** LIVE — NOT residue. Must be retained while arc11_close_audit.py is used.

### R11: verify-td.json
**What it is:** Task definition for `luciel-verify` — the one-shot Pattern N verification harness. `cfn/luciel-verify-task-role.yaml` provisions the IAM role it uses.
**Status:** LIVE — not residue. The ops runbook and verify infrastructure depend on it.

### R12: worker-deployment-config.json
**What it is:** A 154-byte JSON snippet: `deploymentCircuitBreaker: {enable: true, rollback: true}, maximumPercent: 200, minimumHealthyPercent: 100`. Provides deployment configuration for the worker ECS service rolling deploy.
**CI/script references:** No direct reference found in .github/ or scripts/. Likely used in a manual `aws ecs update-service --deployment-configuration file://worker-deployment-config.json` step.
**Dependency impact:** Low-risk residue but may be relied on in a manual ops step not captured in version-controlled CI. Should be investigated with the founder before removal. If the worker service now uses `luciel-prod-worker-autoscaling.yaml` (which it does), this config may be superseded.
**Safe-to-delete:** UNCERTAIN — no CI reference but possible manual ops dependency.

### R13–R14: infra/iam/luciel-mint-operator-role-permission-policy.json.pre-p3-k-followup-2026-05-04 and .pre-p3-s-half-2-step-4-2026-05-05
**What they are:** Backup snapshots of `luciel-mint-operator-role-permission-policy.json` taken before two "Phase 3" permission boundary iterations (May 4 and May 5, 2026). The current live policy is `infra/iam/luciel-mint-operator-role-permission-policy.json`.
**CI/script references:** Zero references in scripts/ or .github/. Pure on-disk backups.
**Dependency impact:** Git history preserves the diffs. The backup files are redundant with git. Safe to remove.
**Safe-to-delete:** YES — git-history-recoverable, zero live readers.

### R15: iam/LucielSESSendEmail-post-patch-2026-05-22.json
**What it is:** A standalone IAM policy document allowing `ses:SendEmail` / `ses:SendRawEmail` on `luciel-default` config set. Named "post-patch" with a date stamp (May 22, 2026), suggesting it was captured after a live policy change.
**CI/script references:** Zero references in scripts/ or .github/.
**Dependency impact:** Appears to be a point-in-time snapshot of an applied IAM policy, not a declarative source of truth. Safe to remove if the live policy state is managed via the AWS console or a future CFN stack.
**Safe-to-delete:** UNCERTAIN — confirm no deploy script regenerates the policy from this file before removing.

### R16: iam/luciel-ecs-verify-role-permissions.json
**What it is:** IAM permissions document for the `luciel-ecs-verify-role`, mirroring `cfn/luciel-verify-task-role.yaml`.
**Status:** AMBIGUOUS — the role is declared in CFN (verify-task-role.yaml). This JSON may be a reference snapshot or a separate apply path. Not referenced in scripts/ or .github/.
**Dependency impact:** Low — CFN is the source of truth. Safe to remove if verified the role is managed exclusively via CFN.

### R17: ARC15_BACKEND_REPORT.md, ARC15_DRIFT_CLEANUP_REPORT.md, ARC17_LOOKUP_RECORD_AMENDMENT.md (root-level .md files)
**What they are:** Implementation reports committed to the repo root by the implementing agent at arc closeout.
**CI/script references:** `app/tools/implementations/lookup_record_tool.py:comment` references `ARC17_LOOKUP_RECORD_AMENDMENT.md` by name — a documentation cross-reference in a docstring comment, not a file read.
**Dependency impact:** These are load-bearing institutional memory. The ARC15 report documents drift cleanup rationale; ARC17 amendment is cross-referenced in production code comments. Removing them would erase audit-trail context. **NOT safe to delete without founder decision.** They could be moved to a `docs/arc-reports/` directory but should not be silently purged. They are AMBIGUOUS as "residue" — they are not doc-justified IaC but they record ratified architectural decisions.

### R18–R19: app/domain/__init__.py and app/domain/stubs/__init__.py (empty)
**What they are:** Two empty `__init__.py` files in `app/domain/` and `app/domain/stubs/`. Zero bytes. No code imports from `app.domain.stubs` anywhere in the repo. Comments in repository files explicitly state "Domain-agnostic: no imports from app/domain/."
**Doc ruling:** Arc 5 Path A eliminated the Domain layer. ARC15_DRIFT_CLEANUP_REPORT.md removed domain-layer vestiges but did not remove these empty namespace files.
**Dependency impact:** Python package namespace only; removal leaves no broken imports (confirmed via repo-wide grep). Safe to remove.
**Safe-to-delete:** YES — zero imports, zero functionality, git-history-recoverable.

---

## BLOCKED-EXTERNAL

| # | What cannot be verified | What is needed |
|---|---|---|
| BE-1 | Live AWS control-plane state: whether ECS services (`luciel-backend`, `luciel-worker`) are running, current task definition revision registered in ECS, actual RDS/ElastiCache instance health and configuration (Multi-AZ, encryption, backup window) | AWS credentials (`ecs:DescribeServices`, `ecs:DescribeTaskDefinition`, `rds:DescribeDBInstances`, `elasticache:DescribeReplicationGroups`) — founder-controlled |
| BE-2 | Whether the declared region guardrail gap (MISSING from C-1, row 9) causes actual cross-region deployment risk: without CloudTrail or CFN stack history, it cannot be determined if a past deployment targeted a region other than ca-central-1 | AWS CloudTrail access (`cloudtrail:LookupEvents`) or CFN stack event history |
| BE-3 | Whether `cfn/luciel-prod-alarms.yaml` is actually deployed and the 14 present alarms are in `OK` or `ALARM` state; whether the §5.1 alarms gap has been addressed via console provisioning outside CFN | CloudWatch console access (`cloudwatch:DescribeAlarms`) |
