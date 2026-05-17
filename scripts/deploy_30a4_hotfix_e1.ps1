# scripts/deploy_30a4_hotfix_e1.ps1
# Step 30a.4 hotfix E1 -- widen CORS allow_methods (commit 4a650c5)
#
# What this deploys:
#   - One Python line change in app/main.py adding PATCH + DELETE to
#     CORSMiddleware allow_methods. No DB migration, no schema change,
#     no worker change, no website change.
#
# What this does NOT do:
#   - No alembic step. Migrations e7b2c9d4a18f (Step 30a.4 user_invites
#     table) and b4d8a2e7c1f3 (D2 owner-scope backfill) are already
#     applied in prod -- nothing new to upgrade.
#
# Idempotent: re-running on the same SHA re-registers an identical
# task-def (new revision number) and force-redeploys.
#
# Container-name convention: backend task-def container is "luciel-backend"
# (NOT "web" -- prior deploy scripts had this bug).

$ErrorActionPreference = "Stop"

# ----- Constants -----
$AwsRegion     = "ca-central-1"
$AccountId     = "729005488042"
$EcrRepo       = "$AccountId.dkr.ecr.$AwsRegion.amazonaws.com/luciel-backend"
$Cluster       = "luciel-cluster"
$WebService    = "luciel-backend-service"
$ContainerName = "luciel-backend"

# ----- Preflight: clean workspace + correct commit -----
Write-Host "==> [0/4] Preflight: clean workspace + HEAD = 4a650c5 (or descendant)" -ForegroundColor Cyan
$dirty = git status --porcelain
if ($dirty) {
    Write-Host "ERROR: workspace has uncommitted changes. Commit or stash first." -ForegroundColor Red
    git status --short
    exit 1
}
$head = (git rev-parse --short HEAD).Trim()
Write-Host "    HEAD: $head" -ForegroundColor Gray

# Sanity: confirm the CORS fix line is present in app/main.py
$mainPy = Get-Content (Join-Path (git rev-parse --show-toplevel).Trim() "app/main.py") -Raw
if ($mainPy -notmatch '"GET",\s*"POST",\s*"PATCH",\s*"DELETE",\s*"OPTIONS"') {
    Write-Host "ERROR: app/main.py does NOT contain the widened allow_methods list." -ForegroundColor Red
    Write-Host "       Are you on the right commit? Expected 4a650c5 or descendant." -ForegroundColor Red
    exit 1
}
Write-Host "    app/main.py allow_methods: includes PATCH + DELETE (verified)" -ForegroundColor Gray

# ----- Resolve image tag -----
$Tag      = "step30a4-$head"
$FullUri  = "${EcrRepo}:$Tag"
Write-Host "    ECR tag:  $Tag" -ForegroundColor Gray
Write-Host "    full uri: $FullUri" -ForegroundColor Gray

# ============================================================
# [1/4] ECR login + docker build + push
# ============================================================
Write-Host ""
Write-Host "==> [1/4] ECR login, docker build, docker push" -ForegroundColor Cyan
aws ecr get-login-password --region $AwsRegion | docker login --username AWS --password-stdin $EcrRepo
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: ECR login failed" -ForegroundColor Red; exit 1 }

docker build -t "luciel-backend:$Tag" .
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: docker build failed" -ForegroundColor Red; exit 1 }

docker tag "luciel-backend:$Tag" $FullUri
docker push $FullUri
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: docker push failed" -ForegroundColor Red; exit 1 }

# ============================================================
# [2/4] Fetch current task-def, swap image, register new revision
# ============================================================
Write-Host ""
Write-Host "==> [2/4] Register new backend task-definition revision" -ForegroundColor Cyan

# Pull the current task-def the service is running.
$currentTdArn = aws ecs describe-services `
    --cluster $Cluster `
    --services $WebService `
    --region $AwsRegion `
    --query "services[0].taskDefinition" `
    --output text
Write-Host "    current task-def: $currentTdArn" -ForegroundColor Gray

$tdJsonPath = Join-Path $env:TEMP "luciel-backend-td-source.json"
aws ecs describe-task-definition `
    --task-definition $currentTdArn `
    --region $AwsRegion `
    --query "taskDefinition" `
    --output json | Out-File -Encoding utf8 $tdJsonPath

# Strip read-only fields + swap image. Done inline in Python to avoid
# jq dependency on a Windows box. Hard-fails if the named container is
# not present in the source task-def (the bug deploy_27c/30a2/30a3 had).
$patcher = @"
import json, sys
src_path  = r'$tdJsonPath'
new_image = r'$FullUri'
container = r'$ContainerName'
out_path  = r'${tdJsonPath}.patched.json'
with open(src_path, 'r', encoding='utf-8-sig') as fh:
    td = json.load(fh)
for k in ('taskDefinitionArn','revision','status','requiresAttributes',
         'compatibilities','registeredAt','registeredBy'):
    td.pop(k, None)
found = False
for c in td.get('containerDefinitions', []):
    if c.get('name') == container:
        c['image'] = new_image
        found = True
if not found:
    sys.stderr.write(f'container {container!r} not found in source task-def\n')
    sys.exit(2)
with open(out_path, 'w', encoding='utf-8') as fh:
    json.dump(td, fh)
print(out_path)
"@
$patchedPath = (python -c $patcher).Trim()
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: task-def patcher failed" -ForegroundColor Red; exit 1 }
Write-Host "    patched task-def written: $patchedPath" -ForegroundColor Gray

$BackendNewArn = aws ecs register-task-definition `
    --cli-input-json "file://$patchedPath" `
    --region $AwsRegion `
    --query "taskDefinition.taskDefinitionArn" `
    --output text
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: register-task-definition failed" -ForegroundColor Red
    exit 1
}
Write-Host "    new task-def: $BackendNewArn" -ForegroundColor Gray

# ============================================================
# [3/4] update-service -> force a new deployment
# ============================================================
Write-Host ""
Write-Host "==> [3/4] update-service (force-new-deployment) onto $BackendNewArn" -ForegroundColor Cyan
$updatedTd = aws ecs update-service `
    --cluster $Cluster `
    --service $WebService `
    --task-definition $BackendNewArn `
    --force-new-deployment `
    --region $AwsRegion `
    --query "service.taskDefinition" `
    --output text
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: update-service failed" -ForegroundColor Red; exit 1 }
Write-Host "    service now pinned to: $updatedTd" -ForegroundColor Gray

# ============================================================
# [4/4] Wait for rollout to stabilize
# ============================================================
Write-Host ""
Write-Host "==> [4/4] Waiting for service to stabilize (this can take ~3 min)..." -ForegroundColor Cyan
aws ecs wait services-stable `
    --cluster $Cluster `
    --services $WebService `
    --region $AwsRegion
if ($LASTEXITCODE -ne 0) {
    Write-Host "WARN: services-stable wait timed out or errored; check service events:" -ForegroundColor Yellow
    aws ecs describe-services --cluster $Cluster --services $WebService --region $AwsRegion --query "services[0].events[:5]" --output table
    exit 1
}

Write-Host ""
Write-Host "==> DONE. Service stable on $BackendNewArn (image $Tag)." -ForegroundColor Green
Write-Host "    Verify in browser: click Revoke on /app/team -- F12 Network should show" -ForegroundColor Gray
Write-Host "    the OPTIONS preflight returning 200 (was 400), then DELETE going through." -ForegroundColor Gray
