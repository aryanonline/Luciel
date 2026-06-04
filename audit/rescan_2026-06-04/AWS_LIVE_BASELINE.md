# AWS Live Baseline (read-only sweep) — 2026-06-04

**Identity:** `arn:aws:iam::729005488042:user/luciel-sandbox-agent` · region `ca-central-1`.
**Credentials:** Space-file keys, founder-authorized for use this session. Policy is `*/*` (account-wide).

## DECISIVE FINDING: production was decommissioned on 2026-05-31 (4 days before this scan).

CloudFormation deletion history (`list-stacks`, ca-central-1):
- `luciel-knowledge-bucket`       DELETE_COMPLETE 2026-05-31T17:21Z
- `luciel-widget-cdn`             DELETE_COMPLETE 2026-05-31T17:50Z
- `luciel-prod-worker-autoscaling`DELETE_COMPLETE 2026-05-31T17:15Z
- `luciel-prod-alarms`            DELETE_COMPLETE 2026-05-31T17:21Z
Surviving stacks: `luciel-sandbox-agent-policy` (agent access), `luciel-prod-verify-role` (IAM role only).

## Live resource inventory — EMPTY across ALL 17 enabled regions:
- ECS clusters / services / task-definition families: **0**
- RDS instances: **0**
- ECR repositories: **0**
- S3 buckets: **0**  (the SSM-named `luciel-knowledge-prod-ca-central-1` does NOT exist)
- Secrets Manager secrets: **0**  (docs say connection creds live here — none exist)
- ALB / target groups: **0**
- CloudWatch log groups: **0**

## What survives: dangling config in SSM Parameter Store (/luciel/production/*)
Real config values that point at deleted infra, e.g.:
- `app_database_url` → host `luciel-db.c3oyiegi01hr.ca-central-1.rds.amazonaws.com` (RDS instance GONE)
- `knowledge_s3_bucket` → `luciel-knowledge-prod-ca-central-1` (bucket GONE)
- Stripe price IDs, JWT signing keys, platform-admin key, hcaptcha, magic-link secret (still present)

## Interpretation
Possibility (a) — deliberate teardown — is confirmed by the dated DELETE_COMPLETE events. The system
was real, ran in ca-central-1 (residency doctrine honored), and its prod footprint was torn down
2026-05-31. The repo's `cfn/*.yaml` + `td-*.json` are the IaC that built it.

## Implications for the task
1. "Never touch production" is trivially satisfied — there is no running production.
2. "STAGING ONLY" = build the environment from repo IaC, tagged staging. Empty account => zero collision
   risk; anything created is the only thing there.
3. Infra "in-sync" invariants (deployed-vs-branch, image-vs-build, schema-on-server-vs-migration) are
   currently VACUOUS — nothing is deployed. They become verifiable only after staging is stood up.
4. Manifest TIER-infra items (missing core CFN stacks for ECS/RDS/ALB/ElastiCache/Secrets/S3,
   ca-central-1 guardrails, §5.1 alarms, §5.5 smoke probes) are now the literal build list for staging.
5. The dangling SSM params are RESIDUE candidates (point at deleted infra) — but DO NOT delete: they are
   the recovery blueprint for re-provisioning, and some (JWT keys, Stripe IDs) are reusable. Re-point, not delete.
