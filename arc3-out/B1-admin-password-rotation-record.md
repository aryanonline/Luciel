# B.1 — luciel-db Admin Password Rotation Record

**Date:** 2026-05-22 00:25 EDT  
**Operator:** Aryan Singh (paired with Computer)  
**Drift closed:** `D-luciel-admin-password-leaked-to-local-scrollback-2026-05-21`

## Trigger

Prior session printed the old `luciel_admin` password to local PowerShell scrollback during an SSM scout. Treated as compromised; rotation mandatory.

## Method

Manual rotation (RDS instance has `MasterUserSecret=None`, no Secrets Manager managed rotation).

1. Generated new 64-char hex pwd locally → `.\arc3-out\.secrets-temp\new-admin-pwd.txt` (sha256: 26B1A1386DE9AF1B915AE886273CBB34D25959912B926E7CF8DFF9B41CF05C96)
2. `aws rds modify-db-instance --apply-immediately` rotated master pwd at RDS
3. Composed new `DATABASE_URL` in `.\arc3-out\.secrets-temp\new-database-url.txt`
4. `aws ssm put-parameter --value file://new-database-url.txt` updated `/luciel/database-url` to v3
5. `get-parameter` round-trip + sha256 match: D667F2BAC066F104B7CFEDB6C6E44370A8B4C187DF2901833813C405A6DE070C
6. Probe via `luciel-prod-ops:7` RunTask (image sha256 `b4c145eb…`, same as live backend) returned `PROBE_RESULT=1` exit 0 — auth verified end-to-end against rotated pwd
7. `update-service luciel-backend-service --force-new-deployment` cycled task `1a18884…` → `c3ce028…`, rolloutState=COMPLETED in 3 min
8. New task healthy on ALB target group `luciel-targets` at 10.0.10.108:8000

## Verification

| Check | Pre | Post |
|---|---|---|
| RDS instance state | available | available, no pending mods |
| SSM `/luciel/database-url` version | 2 | **3** (sha-verified) |
| Backend task-id | 1a18884… | **c3ce028…** (fresh SSM read) |
| ALB target | 10.0.11.137 healthy | **10.0.10.108 healthy**, old draining |
| ECS deployment | COMPLETED on :78 | **COMPLETED on :78** (forced new) |
| Worker service | unchanged (luciel_worker user) | unchanged HEALTHY |

## Standards Adopted

1. SSM secret puts MUST use `--value file://`
2. SSM puts MUST be followed by `get-parameter` + sha256 round-trip match
3. Network-config / overrides for AWS CLI MUST use `file://` JSON pattern (PS quoting collides)
4. JMESPath keys with hyphens MUST be parsed PS-side via `ConvertFrom-Json` + `.'key-with-hyphen'`
5. Future SSM SecureString scouts SHOULD run inside ECS Exec, not local box

## Cleanup

- `.\arc3-out\.secrets-temp\new-admin-pwd.txt` — zero-overwritten + deleted
- `.\arc3-out\.secrets-temp\new-database-url.txt` — zero-overwritten + deleted
- `.\arc3-out\.secrets-temp\netcfg.json` — deleted (non-secret, removed for hygiene)
- `.\arc3-out\.secrets-temp\overrides.json` — deleted

## Open Follow-ups (carry into Arc 7/8)

- `D-database-url-shared-across-6-taskdefs-2026-05-21` — Arc 7 consolidation
- `D-backend-runs-as-rds-master-user-2026-05-21` — Arc 8 (create luciel_app scoped role)
- `D-database-url-ssm-path-inconsistent-backend-vs-worker-2026-05-21` — Arc 7
- `D-luciel-grant-check-rev4-image-digest-stale-2026-05-22` — Arc 7 (purge or pin to live image)
