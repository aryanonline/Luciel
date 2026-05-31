# Luciel Environment Rebuild Manifest

Region: `ca-central-1` · Account: `729005488042`
Captured from live infra on 2026-05-31, immediately before teardown.

This manifest describes the **non-IaC core layer** (VPC, ALB, ECS cluster/services, RDS,
Redis) that was created imperatively and is NOT reproducible from CloudFormation templates
in `cfn/`. Use it together with the repo's `cfn/`, `td-*.json`, `infra/iam/`, and the JSON
exports in `recovery/` to stand the environment back up.

The 6 CloudFormation stacks (alarms, autoscaling, knowledge-bucket, widget-cdn,
sandbox-agent-policy, verify-role) ARE reproducible — redeploy templates in `cfn/`.

---

## 1. Network topology

**VPC** `luciel-vpc` — CIDR `10.0.0.0/16` (was `vpc-04311aadf655620f0`)

| Subnet name | CIDR | AZ | Role |
|---|---|---|---|
| luciel-public-1a | 10.0.10.0/24 | ca-central-1a | public (MapPublicIpOnLaunch) |
| luciel-public-1b | 10.0.11.0/24 | ca-central-1b | public |
| luciel-private-1a | 10.0.1.0/24 | ca-central-1a | private |
| luciel-private-1b | 10.0.2.0/24 | ca-central-1b | private |

- **IGW** attached to VPC; public subnets route 0.0.0.0/0 → IGW.
- **No NAT gateway** — Fargate tasks run in public subnets with `assignPublicIp: ENABLED`.
- **3 VPC interface endpoints** (cost ~$22/mo — main reason for teardown): `ssm`, `ssmmessages`,
  `ec2messages`, all on SG `luciel-vpc-endpoint-sg`. These let private tasks reach SSM without NAT.
  Re-create only if you move tasks to private subnets; with public-subnet tasks they may be optional.

**Security groups** (rules detailed in `recovery/security-groups.json`):
- `luciel-ecs-sg` — Fargate service SG (backend + worker share it)
- `luciel-worker-sg` — worker SG
- `luciel-db-sg` — RDS; inbound 5432 from ECS/worker SGs
- `luciel-redis-sg` — Redis; inbound 6379 from ECS/worker SGs
- `luciel-vpc-endpoint-sg` — inbound 443 from VPC CIDR

## 2. Application Load Balancer

- `luciel-alb` — internet-facing, application, public subnets (1a + 1b)
- **Listener :80 HTTP** → redirect to HTTPS
- **Listener :443 HTTPS** → forward to target group `luciel-targets`
  - Cert: `api.vantagemind.ai` (ACM `...f09cb8cc...` — RETAINED, reuse it)
- **Target group `luciel-targets`** — target type `ip`, port 8000 HTTP, health check `GET /health`

## 3. ECS

- Cluster `luciel-cluster`, Fargate, Container Insights enabled.
- **backend service** `luciel-backend-service`: desired 1, public subnets, SG `luciel-ecs-sg`,
  public IP enabled, behind `luciel-targets`. Task def family `luciel-backend` (latest rev128 in
  `recovery/task-defs/luciel-backend.json`; also `td-backend-rev*.json` in repo).
- **worker service** `luciel-worker-service`: desired 1, autoscale 1–4 (CPU target tracking via
  `cfn/luciel-prod-worker-autoscaling.yaml`). Task def family `luciel-worker` (rev63).
- Other task-def families (ops/migrate/mint/verify/probe/recon/smoke/e2e) are one-shot jobs;
  key ones exported to `recovery/task-defs/`, repo has `td-*.json`.

Rebuild: register task defs → create cluster → create services with the network config above.

## 4. RDS

- `luciel-db` — PostgreSQL **16.13**, **db.t3.micro**, **20 GB gp2**, **Multi-AZ**,
  not publicly accessible, port 5432, param group `default.postgres16`,
  subnet group `luciel-db-subnets`, SG `luciel-db-sg`, backup retention 7 days.
- **Restore from final snapshot** (name in `recovery/FINAL_SNAPSHOT_NAME.txt`):
  ```
  aws rds restore-db-instance-from-db-snapshot \
    --region ca-central-1 \
    --db-instance-identifier luciel-db \
    --db-snapshot-identifier <FINAL_SNAPSHOT_NAME> \
    --db-instance-class db.t3.micro \
    --db-subnet-group-name luciel-db-subnets \
    --vpc-security-group-ids <new luciel-db-sg id> \
    --no-publicly-accessible --multi-az
  ```
  Consider **single-AZ** on rebuild to halve cost until you need HA.
- Schema is defined by 122 committed Alembic migrations in `alembic/versions/`; the snapshot
  carries the data. After restore, update `/luciel/*database_url*` SSM params to the new endpoint.

## 5. ElastiCache Redis

- `luciel-redis` (node `luciel-redis-0001-001`) — Redis **7.1.0**, **cache.t4g.micro**,
  subnet group `luciel-redis-subnets`, SG `luciel-redis-sg`.
- Redis is a cache (no durable data to preserve). Recreate fresh; update
  `/luciel/production/REDIS_URL` SSM param to the new endpoint.

## 6. ECR

- Repo `luciel-backend`. Images rebuildable from repo `Dockerfile`. Lifecycle policy in
  `infra/ecr/luciel-backend-lifecycle-policy.json`. Rebuild: `docker build` → `docker push`.

## 7. Rebuild order (suggested)

1. VPC → subnets → IGW → routes → SGs (from §1)
2. ECR repo → build & push backend image
3. RDS restore from snapshot (§4); Redis create (§5)
4. SSM: update DB/Redis URL params to new endpoints
5. ALB + target group + listeners, reuse ACM cert (§2)
6. Register task defs → ECS cluster → services (§3)
7. Redeploy CFN stacks in `cfn/` (alarms, autoscaling, knowledge-bucket, widget-cdn, roles)
8. Re-point DNS for `api.vantagemind.ai` at the new ALB
