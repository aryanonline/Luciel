# Step 28 Phase 1 - Commit 8b-prereq-cleanup Operator Runbook

## Purpose

This runbook documents the operator procedure for cleaning up active
verification residue tenants from prod. It mirrors the Pattern S contract
in `docs/runbooks/operator-patterns.md`. The contract describes what and
why; this file describes how.

The original scope of Commit 8b-prereq-data was to apply pending Alembic
migrations to prod RDS. Pattern O recon (8b-prereq-data commit, 9ff2690)
surfaced 29 verification residue tenants in `tenant_configs`, of which 18
were `active: true`. Applying constraint changes (D11:
`memory_items.actor_user_id NOT NULL` flip) on top of active residue
would produce a brokerage-DD-indefensible audit state. The right
sequencing is: clean active residue first (this commit), then backfill
the lone NULL row and apply migrations (next commit).

## Prerequisites

- Working tree on `step-28-hardening` branch, HEAD at or after `9ff2690`.
- `Pattern S` walker script at `scripts/cleanup_residue_tenant.ps1`.
- Prod admin key minted in SSM at `/luciel/production/platform-admin-key`
  (existed since 2026-04-27).
- Local env: PowerShell 5.1+, AWS CLI configured for ca-central-1 +
  account 729005488042, Python venv with verification suite deps.
- Prod backend reachable at `https://api.vantagemind.ai` (ALB
  `luciel-alb`, internet-facing, active).

## Pre-flight
1. AWS identity
aws sts get-caller-identity --query Account --output text

expect: 729005488042
2. Source prod admin key from SSM into env (Pattern E)
$env:LUCIEL_PROD_ADMIN_KEY = (
aws ssm get-parameter
--name /luciel/production/platform-admin-key
--with-decryption
--query Parameter.Value
--output text
)

Sanity (do not echo):
$env:LUCIEL_PROD_ADMIN_KEY.StartsWith("luc_sk_") # True
$env:LUCIEL_PROD_ADMIN_KEY.Length # ~50

3. Prod backend health
curl.exe -sS -o NUL -w "%{http_code}" https://api.vantagemind.ai/health

expect: 200
4. Auth chain works end-to-end
curl.exe -sS -o NUL -w "%{http_code}" -H "Authorization: Bearer $env:LUCIEL_PROD_ADMIN_KEY" https://api.vantagemind.ai/api/v1/admin/tenants

expect: 200
5. Verification suite baseline
python -m app.verification

expect: 16/17 pillars green, Pillar 13 pre-existing red.

## Identifying active residue
$tenants = Invoke-RestMethod -Method Get
-Headers @{ "Authorization" = "Bearer $env:LUCIEL_PROD_ADMIN_KEY" }
-Uri "https://api.vantagemind.ai/api/v1/admin/tenants"

$residuePatterns = @(
"^step24-5b-",
"^step26-verify-",
"^step27-prodgate-",
"^step27-syncverify-"
)

$activeResidue = $tenants | Where-Object { $_.active } | Where-Object {
$tid = $_.tenant_id
$residuePatterns | Where-Object { $tid -match $_ }
}
$activeResidue | Select-Object tenant_id, id, created_at

## Procedure

For each active residue tenant, the per-resource walker handles the full
cleanup. Run dry-run first, review the action plan, then commit to the
real run.
1. Dry-run all candidates (no mutations)
foreach ($t in $activeResidue.tenant_id) {
.\scripts\cleanup_residue_tenant.ps1 -TenantId $t -DryRun
}

2. Real run with transcript capture
$logFile = "prod-cleanup-$(Get-Date -Format yyyy-MM-dd).json"
$transcript = "$logFile.transcript.txt"
Start-Transcript -Path $transcript -Force | Out-Null
foreach ($t in $activeResidue.tenant_id) {
.\scripts\cleanup_residue_tenant.ps1 -TenantId $t
Start-Sleep -Milliseconds 500
}
Stop-Transcript | Out-Null

Extract canonical JSON log
Get-Content $transcript |
Where-Object { $_ -match '^{."action".}$' } |
Set-Content -Path $logFile -Encoding ascii
Remove-Item $transcript

3. Verify final state
$tenants = Invoke-RestMethod -Method Get
-Headers @{ "Authorization" = "Bearer $env:LUCIEL_PROD_ADMIN_KEY" }
-Uri "https://api.vantagemind.ai/api/v1/admin/tenants"
$stillActiveResidue = $tenants | Where-Object {
$.active -and ($residuePatterns | Where-Object { $.tenant_id -match $_ })
}
$stillActiveResidue.Count # expect 0

4. Re-run verification suite
python -m app.verification

expect 16/17 (Pillar 13 still pre-existing red).
5. Idempotency check (optional but recommended)
foreach ($t in $activeResidue.tenant_id) {
.\scripts\cleanup_residue_tenant.ps1 -TenantId $t
}

expect: every walk emits only "skipped-already-inactive" lines,
walk-complete teardown_passed=true, exit 0.

## Walker output reference

Per-tenant walk emits one JSON line per action. Action types:

| action                    | when                                        |
| ------------------------- | ------------------------------------------- |
| walk-start                | tenant walk begins                          |
| would-delete              | DryRun mode, would-act on a child resource  |
| would-patch               | DryRun mode, would-act on the tenant row    |
| deleted                   | child resource DELETE returned 200/204      |
| patched                   | tenant PATCH returned 200                   |
| skipped-already-inactive  | resource already `active: false`            |
| delete-failed             | DELETE returned non-2xx                     |
| patch-failed              | PATCH returned non-200                      |
| get-failed                | GET endpoint returned non-200               |
| tenant-not-found          | tenant not in `/admin/tenants` list         |
| walk-complete             | walk finished, includes teardown-integrity  |
| walk-failed               | uncaught exception, walker aborted          |

Exit codes: `0` success, `2` missing/malformed admin key, `4` post-walk
teardown still has violations, `5` caught exception.

## Worked example: this commit (2026-05-01)

| Phase | Action | Result |
| ----- | ------ | ------ |
| Canary | `step27-syncverify-7064`, 4 manual surgical DELETEs + 1 PATCH | teardown passed |
| Walker dev | 5 iterations of debugging (curl quoting, IRM array handling, [void] swallowing Emit-Log, Write-Host vs success stream) | walker v5 in scripts/ |
| Batch dry-run | 17 active residue tenants | 53 would-deletes + 17 would-patches, 0 unexpected |
| Batch real | Same 17 tenants | 53 deletes + 17 patches, 17 walks passed, 30.8s wall-clock |
| Idempotency | 18 tenants (canary + batch) re-run | 0 mutations, 77 skipped-already-inactive, 18 passed |

Audit artifacts:
- `prod-cleanup-2026-05-01.json` (idempotency-run snapshot, committed
  with this commit).
- `admin_audit_logs` rows in prod RDS (70 from the real run, 0 from
  idempotency re-run).
- CloudTrail events for each API call.
- This runbook, the operator-patterns.md Pattern S section, and the
  commit message together form the documented procedure.

## Resumption note

The next residue cleanup will be after a verification suite run that
creates new prod-side test tenants (e.g., Pillar 13 fix work, future
regression suites). The walker is the canonical tool. Rerun this
procedure, with the day's date in the log filename. No new tooling
needed unless the resource tree shape changes (new resource types added
under tenants).

D11 unblock (`memory_items.actor_user_id NOT NULL` flip) is **not**
addressed by this commit. The lone NULL row in `memory_items` belongs to
`step27-syncverify-7064` whose tenant_configs row is now `active: false`
but whose `memory_items` row persists (Pattern S is soft-delete on
`live`-aware tables only). D11 unblock requires either a backfill (next
commit) or a hard-delete endpoint (future Phase 3 hardening).