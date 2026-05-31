# Luciel AWS Recovery Reference

Generated during infrastructure teardown on 2026-05-31.
Account: `729005488042` · Region: `ca-central-1` (Montreal)

This file documents the **names and structure** of stateful/secret resources so the
environment can be rebuilt. It deliberately contains **no secret values**. The actual
values for SSM SecureString parameters were left in AWS SSM Parameter Store (NOT deleted)
so secrets survive the teardown. See `docs/INFRA_TEARDOWN.md` for what was deleted vs. kept.

---

## Final RDS snapshot (retained)

- Snapshot name recorded in `recovery/FINAL_SNAPSHOT_NAME.txt`
- Source instance: `luciel-db` (PostgreSQL 16.13, db.t3.micro, 20 GB gp2, Multi-AZ)
- Restore with: `aws rds restore-db-instance-from-db-snapshot` (see rebuild manifest)

Pre-existing manual snapshots (18) were **retained** as well — newest pre-teardown was
`luciel-db-pre-arc12b-20260529-1905`. These carry ~$8.50/mo storage cost; prune when ready.

---

## SSM Parameter Store — RETAINED (values preserved in AWS)

All 34 parameters under `/luciel/*` were kept (standard tier = $0 cost). Full machine-readable
inventory in `recovery/ssm-parameter-inventory.json`. Names/types:

### Secrets / keys (SecureString)
- `/luciel/anthropic-api-key`
- `/luciel/openai-api-key`
- `/luciel/database-url`
- `/luciel/production/app_database_url`
- `/luciel/production/worker_database_url`
- `/luciel/production/audit_archiver_db_url`
- `/luciel/production/audit_archiver_password`
- `/luciel/production/jwt_signing_keys_json`
- `/luciel/production/magic_link_secret`
- `/luciel/production/platform-admin-key`
- `/luciel/production/stripe_secret_key`
- `/luciel/production/stripe_webhook_secret`
- `/luciel/production/hcaptcha_secret_key`
- `/luciel/production/hcaptcha_site_key`
- `/luciel/production/hcaptcha_verify_url`
- `/luciel/production/stripe_arc6_iam_probe`

### Stripe price IDs (SecureString)
- `/luciel/production/stripe_price_individual`
- `/luciel/production/stripe_price_individual_annual`
- `/luciel/production/stripe_price_pro_monthly`
- `/luciel/production/stripe_price_pro_annual`
- `/luciel/production/stripe_price_team_monthly`
- `/luciel/production/stripe_price_team_annual`
- `/luciel/production/stripe_price_company_monthly`
- `/luciel/production/stripe_price_company_annual`
- `/luciel/production/stripe_price_enterprise_monthly`
- `/luciel/production/stripe_price_enterprise_annual`
- `/luciel/production/stripe_price_enterprise_floor_annual`
- `/luciel/production/stripe_price_intro_fee`

### Plain (String)
- `/luciel/production/REDIS_URL`
- `/luciel/production/SES_SNS_TOPIC_ARN`
- `/luciel/production/knowledge_s3_bucket`
- `/luciel/production/jwt_active_kid`
- `/luciel/production/jwt_grace_kid`
- `/luciel/prod/pagerduty/integration_url`

> NOTE: `/luciel/production/REDIS_URL` and the `*_database_url` params point at the
> torn-down Redis/RDS endpoints. On rebuild, **update these** to the new endpoints.

---

## Secrets Manager

None. (No Secrets Manager secrets existed; all secrets live in SSM.)

---

## ACM Certificate — RETAINED

- Domain: `api.vantagemind.ai`
- ARN: `arn:aws:acm:ca-central-1:729005488042:certificate/f09cb8cc-2099-4dd6-8312-fe34a967033d`
- Status: ISSUED — kept (free; avoids re-validation on rebuild)

---

## Exported live configs (in `recovery/`)

Because the core network/compute layer was **created imperatively (PowerShell/CLI), not via
declarative IaC**, its live configuration was exported to JSON before teardown:

| File | Resource |
|---|---|
| `vpc.json`, `subnets.json`, `security-groups.json`, `route-tables.json`, `igw.json`, `vpc-endpoints.json`, `elastic-ips.json` | VPC + networking |
| `alb.json`, `target-groups.json`, `listeners.json` | Application Load Balancer |
| `ecs-cluster.json`, `ecs-services.json` | ECS cluster + service definitions |
| `rds-instance.json`, `rds-subnet-group.json` | RDS config |
| `redis-cluster.json`, `redis-replication-group.json`, `redis-subnet-groups.json` | ElastiCache Redis |
| `task-defs/*.json` | Latest ACTIVE task definitions (backend rev128, worker rev63, + ops/migrate/mint/verify) |
| `cfn-templates/*.json` | Defensive copies of the 6 CloudFormation stack templates |
| `sqs-*.json`, `sns-topics.json` | Queue/topic config |
| `ssm-parameter-inventory.json` | SSM names/types (no values) |
