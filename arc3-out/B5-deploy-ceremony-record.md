# Arc 3 Work-Unit C — Deploy Ceremony Record

**Date:** 2026-05-22 (02:14 → 02:36 EDT)
**Operator:** Aryan (partner)
**Co-pilot:** Computer
**Cadence:** Step-by-step dictation (paired ceremony)
**Outcome:** ✅ GREEN — backend + worker both deployed, bit-identical image, zero rollback

## Image identity

- **New (live):** `sha256:b53b76225ff39e4592bc8934260a2caaba5a163e35a66f4e3d14175ba08e4e44`
- **Old (drained):** `sha256:b4c145eb3f876f30fec947e7d58080c570eabf5ddce587815eb28d98214c4dff`
- **ECR repo:** `729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend`
- **ECR tag:** `arc3-prod-ops-c` → new digest `b53b762...`
- **Code commit baked into image:** `ba59e86` (`arc3 work-unit C: email-shape gate at TierProvisioningService.premint_for_tier`)
- **Doc-truthing commit (not in image, repo-only):** `984e4ea` (`arc-3-doc-truthing: full mirror sweep (DRIFTS + ARCH + E2E)`)
- **Bit-identical hygiene:** Backend and worker both pinned to the same digest. Worker has no direct dependency on the Work-Unit C code path, but is deployed alongside to preserve the invariant that backend and worker run from the same image build at all times.

## Timeline

| Phase | Start (EDT) | Duration | Outcome |
|---|---|---|---|
| Docker build | 02:14 | ~3 min | manifest digest `b53b762...`; `email_validator` baked in (`pip show` inside container confirmed) |
| ECR auth + push | 02:17 | ~30s | same digest `b53b762...` landed in ECR under tag `arc3-prod-ops-c` |
| Backend task-def 78 → 79 register | 02:18 | ~2 min (incl. BOM rework) | revision `luciel-backend:79` ACTIVE |
| Backend update-service (force-new-deployment) | 02:20:37 | 1m 57s | COMPLETED, 0 failed tasks |
| Backend smoke (`/health`, `/api/v1/billing/me`, `/api/v1/health/version`) | 02:22:33 | <30s | 200 / 401 / 401 (third 401 opened a new drift — see below) |
| Worker task-def 33 → 34 register | 02:26 | ~1 min | revision `luciel-worker:34` ACTIVE; no BOM (Standard #10 applied first try) |
| Worker update-service (force-new-deployment) | 02:27:47 | 2m 13s | COMPLETED, 0 failed tasks |
| Worker steady-state event | 02:29:58 | — | `(service luciel-worker-service) has reached a steady state.` |
| Worker stability verify (CloudWatch) | 02:31 → 02:36 | ~5 min (incl. probe rework) | `celery@... ready.` observed at 02:27:59; 33 heartbeat touches; 0 errors across 61 events |

## Production state after deploy

- **Backend service:** `luciel-backend-service` on task-def `luciel-backend:79`, image digest `b53b762...`
- **Worker service:** `luciel-worker-service` on task-def `luciel-worker:34`, task `b91968805a674fc3bd041bcda9e614d2`, same image digest
- **ALB DNS:** `luciel-alb-1617994381.ca-central-1.elb.amazonaws.com` → public hostname `api.vantagemind.ai`
- **ALB target group:** `arn:aws:elasticloadbalancing:ca-central-1:729005488042:targetgroup/luciel-targets/c77038fb075247ba` (healthy)
- **Account / Region / Cluster:** 729005488042 / ca-central-1 / `luciel-cluster`
- **Log groups:** `/ecs/luciel-backend` (prefix `ecs`), `/ecs/luciel-worker` (prefix `worker`)

## Smoke results

| Endpoint | Result | Verdict |
|---|---|---|
| `/health` | 200 `{"status":"ok","service":"Luciel Backend"}` | ✅ pass |
| `/api/v1/billing/me` | 401 (no auth) | ✅ expected — endpoint is auth-gated, no JWT presented |
| `/api/v1/health/version` | 401 (no auth) | ⚠️ NEW DRIFT — see `D-health-version-endpoint-gated-by-apikey-auth-2026-05-22` |

## Worker boot evidence

Celery 5.6.3 (recovery) launched on `ip-10-0-11-33.ca-central-1.compute.internal`, container `luciel-worker`. Boot to ready in **7 seconds** (02:27:52 EDT task-start → 02:27:59 EDT ready banner). Heartbeat thread running 15s interval, 33 consecutive `/tmp/celery_alive` touches observed before close-out. SQS broker connected (`Connected to sqs://localhost//` — the `localhost` is a Celery cosmetic; SDK handles real routing). Beat scheduler started (`beat: Starting...`). Tasks registered: `app.worker.tasks.memory_extraction.extract_memory_from_turn`, `app.worker.tasks.retention.run_retention_purge`. Queue bound: `luciel-memory-tasks`. Error-pattern scan (`Traceback|ImportError|ModuleNotFoundError|OperationalError|AccessDenied|ERROR|CRITICAL|Connection refused`) over 61 events: **0 hits**.

## Drift impact

**Resolved this deploy:**

- `D-tier-provisioning-email-validator-deploy-pending-2026-05-22` — code from `ba59e86` is now live on `luciel-backend:79`. Closure evidence: image digest on the running task hashes to `b53b762...`, which was built from `ba59e86` HEAD. The repo-side gate (`_validate_email_shape`, `TierProvisioningValidationError`) is now defence-in-depth at the tier-provisioning boundary in production.

**New drifts opened during deploy (logged to DRIFTS.md §3 in this same commit):**

1. `D-health-version-endpoint-gated-by-apikey-auth-2026-05-22` — `/api/v1/health/version` returns 401 because the path is not in `SKIP_AUTH_PATHS` in `app/middleware/auth.py`. The endpoint exists at `app/api/v1/health.py:7` and should be publicly readable so operators can verify which build is live without holding a JWT. P3 hygiene. Arc 8.
2. `D-version-endpoint-hardcoded-not-build-sha-2026-05-22` — `/api/v1/health/version` returns hardcoded `{"version": "0.1.0"}` rather than the build SHA / image digest. Even once unguarded, this endpoint provides no operational value until it reports the real build identity. P3 traceability. Arc 8. Natural pair with the gating-fix above.
3. `D-worker-runs-as-root-in-container-2026-05-22` — Celery boot log emits `SecurityWarning: You're running the worker with superuser privileges` (uid=0, euid=0). The Dockerfile has no `USER` directive after the `pip install` layer, so the worker process runs as root. P3 security hygiene. Arc 8. Adjacent to `D-backend-runs-as-rds-master-user-2026-05-22` (sibling least-privilege drift on the backend container).

## Standards adopted

**Standard #10 — PS-emitted JSON for AWS CLI ingest.** Use `[IO.File]::WriteAllText` with `New-Object System.Text.UTF8Encoding $false`. Never `Out-File -Encoding utf8` — PowerShell 5.x's `utf8` encoding writes a UTF-8 BOM that AWS CLI rejects with `Invalid JSON received`. Discovered during backend task-def 79 register (Step 3c.6); applied successfully on worker task-def 34 register (Step 6) on first try.

```powershell
# WRONG (writes BOM, AWS CLI rejects)
$json | Out-File -Encoding utf8 -FilePath td-candidate.json

# RIGHT (BOM-free)
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[IO.File]::WriteAllText((Resolve-Path .).Path + '\td-candidate.json', $json, $utf8NoBom)
```

## Operator lessons (self-noted by Computer)

1. **`/health` path was guessed wrong initially.** Claimed `/healthz`; actual route is `/health` at `app/main.py:142`. Should have grepped the codebase before dictating the smoke command. Took the blame, corrected mid-step.
2. **CloudWatch log stream prefix wrong initially.** Assumed `ecs/luciel-worker/<task-id>` from generic ECS documentation; actual `awslogs-stream-prefix` in `luciel-worker:34`'s `logConfiguration.options` is `worker`, so the real stream is `worker/luciel-worker/<task-id>`. Should have read the task-def's `logConfiguration` before constructing stream names. Recovered via `describe-log-streams --order-by LastEventTime --descending`.
3. **Local-clock millisecond math in Step 8c.** Initial CloudWatch probe used `Get-Date` minus five minutes converted to Unix ms locally, which returned events from the prior worker task (`29201c7136...`, stopped at 02:28:36) rather than the new task. Avoided in Step 8f by using `start-from-head` on the targeted stream instead of time-window filtering.
4. **Ready-banner regex bug in Step 8f.** Used `ready\\.` in a single-quoted PowerShell string, which became literal `\\` in the regex and matched nothing. Printed `Ready-banner hits:` as blank. Cosmetic only — the ready banner was directly visible in the timeline output above the scanner.
5. **`storedBytes: 0` is not a reliable signal of "empty stream"** in CloudWatch — the metadata lags asynchronously, especially on low-volume streams. Trust `firstEvent` / `lastEvent` timestamps instead.

## Files in workspace (transient ceremony artifacts on operator machine)

These four task-def JSON snapshots exist on the operator's local Windows machine at `C:\Users\aryan\Projects\Business\Luciel\arc3-out\` but are not staged into the repo — they capture pre-deploy and candidate revisions and have no archival value beyond this record:

- `task-def-78-live.json` (backend pre-deploy snapshot)
- `task-def-79-candidate.json` (backend re-emitted BOM-free, used for register)
- `worker-td-33-live.json` (worker pre-deploy snapshot)
- `worker-td-34-candidate.json` (worker register payload)

The audit-relevant facts (revision numbers, digests, timestamps, rollout durations) are captured in this record and in `docs/DRIFTS.md`. The JSON snapshots can be regenerated at any time with `aws ecs describe-task-definition --task-definition luciel-backend:79`.

---

**Closing tag for Arc 3:** `arc-3-backend-hygiene-auth-hardening-ses-posture-doc-truthing`
