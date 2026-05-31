# Luciel AWS Infrastructure Teardown Record

**Date:** 2026-05-31
**Account:** `729005488042`
**Region:** `ca-central-1` (Montreal)
**Reason:** Pause cloud deployment to stop accruing costs while the product is validated
locally. This teardown removed **live billable AWS infrastructure only**. No application
code, migrations, or incomplete ARC work was touched. ARC 13 code remains untouched in the repo.

Recovery state was preserved before deletion — see `docs/RECOVERY_REFERENCE.md` and
`docs/REBUILD_MANIFEST.md`, plus the JSON exports in `recovery/`.

---

## What was DELETED

### Compute / containers
- ECS cluster `luciel-cluster` (deleted)
- ECS services `luciel-backend-service`, `luciel-worker-service` (scaled to 0, force-deleted)
- ECS worker autoscaling (scalable target + scaling policy) via CFN stack `luciel-prod-worker-autoscaling`
- 74 ECS task-definition revisions across 18 families (deregistered)
- ECR repository `luciel-backend` (100 images, ~11.4 GB) — rebuildable from `Dockerfile`

### Database / cache
- RDS instance `luciel-db` (PostgreSQL 16.13, db.t3.micro, Multi-AZ) — deletion protection
  disabled, deleted with `--skip-final-snapshot` (a fresh final snapshot was taken first, see below),
  automated backups deleted
- RDS subnet group `luciel-db-subnets`
- ElastiCache Redis replication group `luciel-redis` (node `luciel-redis-0001-001`, cache.t4g.micro)
- Redis subnet group `luciel-redis-subnets`

### Networking
- ALB `luciel-alb` + both listeners (:80 redirect, :443 forward) + target group `luciel-targets`
- 3 VPC interface endpoints (ssm, ssmmessages, ec2messages) — was ~$22/mo
- 2 Elastic IPs (released automatically when Fargate ENIs were torn down)
- VPC `luciel-vpc` (`vpc-04311aadf655620f0`, 10.0.0.0/16): 4 subnets, 5 security groups,
  1 non-main route table, internet gateway — all deleted; VPC deleted

### Storage / messaging / observability
- S3 `luciel-data-exports` (empty, deleted)
- S3 `luciel-knowledge-prod-ca-central-1` (empty, deleted — stack used Retain policy so removed manually)
- SQS `luciel-memory-tasks`, `luciel-memory-dlq` (deleted directly)
- SQS `luciel-knowledge-tasks`, `luciel-knowledge-dlq` (deleted via CFN stack)
- 4 CloudWatch log groups (`/ecs/luciel-backend`, `/ecs/luciel-worker`, `/ecs/luciel-migrations`,
  container-insights performance)
- 24 CloudWatch alarms (20 via `luciel-prod-alarms` stack + 4 standalone arc10 alarms) and 2 SNS
  topics (`luciel-prod-alerts`, `luciel-prod-pagerduty-high`) via stack deletion

### CloudFormation stacks deleted
- `luciel-prod-worker-autoscaling`
- `luciel-prod-alarms`
- `luciel-knowledge-bucket`

---

## What was PRESERVED (and why)

### Retained with minor residual cost — call these out
- **Final RDS snapshot:** `luciel-db-final-teardown-20260531-1712` (RETAINED). This is the
  point-in-time database backup for rebuild.
- **18 pre-existing manual RDS snapshots** also retained. Total manual snapshot storage **= 380 GB
  (~$8.50/mo)**. To reduce cost, prune all but the final snapshot when comfortable:
  `aws rds delete-db-snapshot --db-snapshot-identifier <name>` (keep `luciel-db-final-teardown-20260531-1712`).
- **S3 `luciel-audit-cold-archive`** — 3 audit objects, ~2 KB. Effectively $0. Copy also in `recovery/audit-cold-archive/`.
- **Website infra (near-$0 idle):** Amplify app `Luciel-Website`, CloudFront `EU5R6YVX26RPY`,
  S3 `luciel-widget-cdn-prod-ca-central-1` (5 objects) via retained CFN stack `luciel-widget-cdn`.
  Delete these too if you want the marketing site offline.

### Retained at $0
- **All SSM parameters** (34 under `/luciel/*`) — values preserved (they exist nowhere else; not in code).
- **All IAM** — 10 `luciel-*` roles, 3 customer-managed policies, OIDC provider, users
  `luciel-admin` and `luciel-sandbox-agent`. Kept to avoid lockout and preserve rebuild ability.
  Retained CFN stacks: `luciel-prod-verify-role`, `luciel-sandbox-agent-policy`.
- **ACM certificate** `api.vantagemind.ai` — free; avoids re-validation on rebuild.
- **SNS topic** `luciel-ses-events` — $0; tied to SES (out of scope for this teardown).

---

## Final zero-cost posture (verified 2026-05-31)

Cross-region sweep (us-east-1/2, us-west-1/2, ca-central-1, eu-west-1, eu-central-1):
**all clean** — 0 ECS clusters, 0 RDS instances, 0 ElastiCache, 0 ALBs, 0 EC2, 0 NAT gateways,
0 Elastic IPs, 0 VPC endpoints, 0 ECR repos, 0 non-default VPCs (in ca-central-1), 0 EBS volumes,
0 CloudWatch alarms, 0 luciel log groups.

**Residual cost by design:** RDS snapshot storage ~$8.50/mo (380 GB across 19 snapshots) +
negligible S3 (audit archive + widget CDN, well under $0.10/mo) + idle CloudFront/Amplify (~$0).
Estimated total residual: **< $9/month**, down from ~$90–105/month.

---

## SECURITY ACTION REQUIRED

The IAM access key `AKIA2TPA466VF3RNG47L` (user `luciel-sandbox-agent`, full `*:*` admin) was
stored in plaintext. **Rotate or deactivate it** in IAM now that the teardown is done:
`aws iam update-access-key --user-name luciel-sandbox-agent --access-key-id AKIA2TPA466VF3RNG47L --status Inactive`
(or delete it). Re-issue fresh scoped credentials when you rebuild.

---

## How to rebuild

See **`docs/REBUILD_MANIFEST.md`** for the full step-by-step (network → ECR → RDS restore →
Redis → SSM endpoint updates → ALB+cert → ECS → redeploy `cfn/` stacks → DNS). The Alembic
migration chain (122 files in `alembic/versions/`) defines the schema; the retained snapshot
carries the data.
