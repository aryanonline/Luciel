# scripts/deploy_arc8.ps1
# Arc 8 Work-Units 2 + 3 -- paired non-root + version-endpoint rollout
#
# What this deploys (paired backend + worker):
#   * WU-2 (D-worker-runs-as-root-in-container-2026-05-22):
#       Dockerfile useradd luciel (uid=10001) + USER luciel directive
#       before the final CMD. Both the backend (uvicorn) and worker
#       (celery) processes inherit the non-root posture because they
#       share the same image.
#   * WU-3 (D-version-endpoint-hardcoded-not-build-sha-2026-05-22):
#       Dockerfile BUILD_GIT_SHA ARG/ENV chain + app/core/build_info.py
#       singleton + app/api/v1/health.py reads from the singleton. The
#       /api/v1/version endpoint reports the deployed commit's short
#       SHA after this rollout.
#
# What this does NOT deploy:
#   * No Alembic migration (Arc 8 is code-only on the schema axis;
#     Arc 5 owns the schema migration).
#   * No SSM change, no IAM change, no CFN change.
#   * No customer-visible surface change beyond the /api/v1/version
#     payload superset (legacy three keys preserved verbatim).
#
# Idempotency:
#   Re-running with the same SHA is a no-op for the ECR push (the tag
#   already exists) and re-registers identical task-defs (new revision
#   numbers, same image+command). The two service-updates at the end
#   are what actually trigger placement.
#
# Sequence (paired, single image cycle):
#   [0/7]  Preflight: clean workspace + correct branch
#   [1/7]  Build image with --build-arg BUILD_GIT_SHA=<short-sha>
#   [2/7]  ECR login + push + resolve digest
#   [3/7]  Register luciel-backend:NEW (image swap)
#   [4/7]  Register luciel-worker:NEW (image swap, same digest)
#   [5/7]  Update backend service + wait for stable
#   [6/7]  Update worker service + wait for stable
#   [7/7]  Paired smoke (id=10001 on worker, /version returns $Sha)

$ErrorActionPreference = "Stop"

# ----- Constants -----
$AwsRegion       = "ca-central-1"
$AccountId       = "729005488042"
$EcrRepo         = "$AccountId.dkr.ecr.$AwsRegion.amazonaws.com/luciel-backend"
$Cluster         = "luciel-cluster"
$WebService      = "luciel-backend-service"
$WorkerService   = "luciel-worker-service"
$BackendContainer = "luciel-backend"
$WorkerContainer  = "luciel-worker"

# ----- Preflight [0/7]: clean workspace + branch sanity -----
Write-Host "==> [0/7] Preflight: confirm workspace is clean + on main" -ForegroundColor Cyan
$dirty = git status --porcelain
if ($dirty) {
    Write-Host "ERROR: workspace has uncommitted changes. Commit or stash first." -ForegroundColor Red
    git status --short
    exit 1
}

$branch = (git rev-parse --abbrev-ref HEAD).Trim()
if ($branch -ne "main") {
    Write-Host "ERROR: not on main branch (currently '$branch'). Switch to main first." -ForegroundColor Red
    exit 1
}

# Confirm Dockerfile carries the WU-2+WU-3 directives we expect.
$dfContent = Get-Content (Join-Path (git rev-parse --show-toplevel).Trim() "Dockerfile") -Raw
if ($dfContent -notmatch 'ARG\s+BUILD_GIT_SHA') {
    Write-Host "ERROR: Dockerfile missing 'ARG BUILD_GIT_SHA' (WU-3 not landed)" -ForegroundColor Red
    exit 1
}
if ($dfContent -notmatch 'USER\s+luciel') {
    Write-Host "ERROR: Dockerfile missing 'USER luciel' directive (WU-2 not landed)" -ForegroundColor Red
    exit 1
}
Write-Host "    Dockerfile carries WU-2 (USER luciel) + WU-3 (BUILD_GIT_SHA) directives" -ForegroundColor Gray

# ----- Resolve image tag from short SHA -----
$Sha   = (git rev-parse --short HEAD).Trim()
$Tag   = "arc8-$Sha"
$Image = "${EcrRepo}:${Tag}"
Write-Host ""
Write-Host "==> Arc 8 paired rollout starting" -ForegroundColor Cyan
Write-Host "    git sha:   $Sha"
Write-Host "    image:     $Image"
Write-Host "    cluster:   $Cluster"
Write-Host "    backend:   $WebService -> container $BackendContainer"
Write-Host "    worker:    $WorkerService -> container $WorkerContainer"
Write-Host ""

# ----- [1/7] Build image with BUILD_GIT_SHA threaded in -----
# IMPORTANT: --platform linux/amd64 pins arch to match Fargate runtime.
# --provenance=false + --sbom=false disable BuildKit attestations that
# produce multi-entry manifest lists where Fargate may pull an attestation
# blob (no USER directive) instead of the real image. Without these flags,
# the Dockerfile USER directive may not take effect at container runtime.
# Drift evidence: arc8 WU-2 first attempt 2026-05-22 (image 9414390f) ran
# as uid=0(root) despite Dockerfile USER luciel; root cause was attestation
# manifest selection by Fargate.
Write-Host "==> [1/7] Building image (--build-arg BUILD_GIT_SHA=$Sha, single-arch, no attestations)" -ForegroundColor Cyan
docker buildx build `
  --platform linux/amd64 `
  --provenance=false `
  --sbom=false `
  --build-arg "BUILD_GIT_SHA=$Sha" `
  -t $Image `
  --load .
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: docker build failed" -ForegroundColor Red; exit 1 }

# ----- [1b/7] Verify USER directive baked in (paranoid local guard) -----
Write-Host "==> [1b/7] Verifying image USER=luciel (local docker inspect)" -ForegroundColor Cyan
$imageUser = docker inspect --format='{{.Config.User}}' $Image
if ($imageUser -ne "luciel") {
  Write-Host "ERROR: built image User='$imageUser', expected 'luciel'. Dockerfile USER directive did not bake. Aborting deploy." -ForegroundColor Red
  exit 1
}
Write-Host "    OK: image User=luciel"
$imageArch = docker inspect --format='{{.Architecture}}' $Image
if ($imageArch -ne "amd64") {
  Write-Host "ERROR: built image Architecture='$imageArch', expected 'amd64'. Aborting deploy." -ForegroundColor Red
  exit 1
}
Write-Host "    OK: image Architecture=amd64"

# ----- [2/7] ECR login + push + resolve digest -----
Write-Host "==> [2/7] ECR login + push" -ForegroundColor Cyan
$loginPwd = aws ecr get-login-password --region $AwsRegion
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: ecr get-login-password failed" -ForegroundColor Red; exit 1 }
$loginPwd | docker login --username AWS --password-stdin $EcrRepo
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: docker login failed" -ForegroundColor Red; exit 1 }

docker push $Image
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: docker push failed" -ForegroundColor Red; exit 1 }

# Pin to digest so both task-defs (backend + worker) reference an
# immutable identity, preserving the bit-identical-image invariant.
$Digest = aws ecr describe-images `
    --repository-name luciel-backend `
    --image-ids "imageTag=$Tag" `
    --region $AwsRegion `
    --query 'imageDetails[0].imageDigest' --output text
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($Digest) -or $Digest -eq "None") {
    Write-Host "ERROR: could not resolve digest for tag $Tag" -ForegroundColor Red
    exit 1
}
$PinnedImage = "${EcrRepo}@${Digest}"
Write-Host "    pinned: $PinnedImage" -ForegroundColor Gray

# ----- Shared Python rewriter (image-swap on task-def JSON) -----
$pythonExe = if ($env:VIRTUAL_ENV) { Join-Path $env:VIRTUAL_ENV "Scripts\python.exe" } else { "python" }
if (-not (Test-Path $pythonExe)) { $pythonExe = "python" }

$pyScript = @"
import json, sys
current_path = sys.argv[1]
out_path     = sys.argv[2]
container    = sys.argv[3]
image        = sys.argv[4]

with open(current_path, 'r', encoding='utf-8-sig') as f:
    td = json.load(f)

KEEP = (
    'family','taskRoleArn','executionRoleArn','networkMode',
    'containerDefinitions','volumes','placementConstraints',
    'requiresCompatibilities','cpu','memory','runtimePlatform',
)
filtered = {k: td[k] for k in KEEP if k in td and td[k] is not None}

matched = 0
for c in filtered.get('containerDefinitions', []):
    if c.get('name') == container:
        c['image'] = image
        matched += 1
if matched != 1:
    names = [c.get('name') for c in filtered.get('containerDefinitions', [])]
    sys.stderr.write(
        f"ERROR: expected exactly 1 container named {container!r} in source task-def, "
        f"matched {matched}. Found containers: {names}\n"
    )
    sys.exit(2)

with open(out_path, 'w', encoding='utf-8', newline='') as f:
    json.dump(filtered, f)
"@

$pyScriptPath = Join-Path $env:TEMP "arc8-rewrite.py"
Set-Content -Path $pyScriptPath -Value $pyScript -Encoding ascii

# ----- [3/7] Register luciel-backend:NEW (image swap) -----
Write-Host ""
Write-Host "==> [3/7] Registering luciel-backend:NEW (image swap only)" -ForegroundColor Cyan
$backendCurrentPath = Join-Path $env:TEMP "arc8-backend-current.json"
$backendNewPath     = Join-Path $env:TEMP "arc8-backend-new.json"

aws ecs describe-task-definition `
    --task-definition luciel-backend `
    --region $AwsRegion `
    --query 'taskDefinition' `
    --output json | Out-File -FilePath $backendCurrentPath -Encoding ascii
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: describe-task-definition (backend) failed" -ForegroundColor Red; exit 1 }

& $pythonExe $pyScriptPath $backendCurrentPath $backendNewPath $BackendContainer $PinnedImage
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: backend JSON rewrite failed" -ForegroundColor Red; exit 1 }

$BackendNewArn = aws ecs register-task-definition `
    --cli-input-json "file://$backendNewPath" `
    --region $AwsRegion `
    --query 'taskDefinition.taskDefinitionArn' --output text
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($BackendNewArn)) {
    Write-Host "ERROR: backend register-task-definition failed" -ForegroundColor Red
    exit 1
}
Write-Host "    registered: $BackendNewArn" -ForegroundColor Gray

# ----- [4/7] Register luciel-worker:NEW (image swap, same digest) -----
Write-Host ""
Write-Host "==> [4/7] Registering luciel-worker:NEW (same digest, image-pin invariant)" -ForegroundColor Cyan
$workerCurrentPath = Join-Path $env:TEMP "arc8-worker-current.json"
$workerNewPath     = Join-Path $env:TEMP "arc8-worker-new.json"

aws ecs describe-task-definition `
    --task-definition luciel-worker `
    --region $AwsRegion `
    --query 'taskDefinition' `
    --output json | Out-File -FilePath $workerCurrentPath -Encoding ascii
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: describe-task-definition (worker) failed" -ForegroundColor Red; exit 1 }

& $pythonExe $pyScriptPath $workerCurrentPath $workerNewPath $WorkerContainer $PinnedImage
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: worker JSON rewrite failed" -ForegroundColor Red; exit 1 }

$WorkerNewArn = aws ecs register-task-definition `
    --cli-input-json "file://$workerNewPath" `
    --region $AwsRegion `
    --query 'taskDefinition.taskDefinitionArn' --output text
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($WorkerNewArn)) {
    Write-Host "ERROR: worker register-task-definition failed" -ForegroundColor Red
    exit 1
}
Write-Host "    registered: $WorkerNewArn" -ForegroundColor Gray

# ----- [5/7] Update backend service + wait for stable -----
Write-Host ""
Write-Host "==> [5/7] Updating backend service to $BackendNewArn" -ForegroundColor Cyan
$updatedBackendTd = aws ecs update-service `
    --cluster $Cluster `
    --service $WebService `
    --task-definition $BackendNewArn `
    --region $AwsRegion `
    --query 'service.taskDefinition' --output text
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: backend update-service failed" -ForegroundColor Red; exit 1 }
Write-Host "    backend service now on: $updatedBackendTd" -ForegroundColor Gray

Write-Host "    waiting for backend to stabilize (ECS rolling deploy + ALB health checks, ~2-4 min)..." -ForegroundColor Gray
aws ecs wait services-stable `
    --cluster $Cluster `
    --services $WebService `
    --region $AwsRegion
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: backend services-stable wait failed" -ForegroundColor Red; exit 1 }
Write-Host "    backend stable" -ForegroundColor Green

# ----- [6/7] Update worker service + wait for stable -----
Write-Host ""
Write-Host "==> [6/7] Updating worker service to $WorkerNewArn" -ForegroundColor Cyan
$updatedWorkerTd = aws ecs update-service `
    --cluster $Cluster `
    --service $WorkerService `
    --task-definition $WorkerNewArn `
    --region $AwsRegion `
    --query 'service.taskDefinition' --output text
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: worker update-service failed" -ForegroundColor Red; exit 1 }
Write-Host "    worker service now on: $updatedWorkerTd" -ForegroundColor Gray

Write-Host "    waiting for worker to stabilize..." -ForegroundColor Gray
aws ecs wait services-stable `
    --cluster $Cluster `
    --services $WorkerService `
    --region $AwsRegion
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: worker services-stable wait failed" -ForegroundColor Red; exit 1 }
Write-Host "    worker stable" -ForegroundColor Green

# ----- [7/7] Paired smoke: /version returns $Sha + worker runs as luciel -----
Write-Host ""
Write-Host "==> [7/7] Paired smoke walk" -ForegroundColor Cyan

# (a) /api/v1/version smoke: assert git_sha matches our just-deployed $Sha.
Write-Host "    (a) GET /api/v1/version -- expect git_sha=$Sha" -ForegroundColor Gray
try {
    $verResp = Invoke-WebRequest `
        -Uri "https://api.vantagemind.ai/api/v1/version" `
        -UseBasicParsing `
        -TimeoutSec 15
    $verBody = $verResp.Content | ConvertFrom-Json
    Write-Host "        response: $($verResp.Content)" -ForegroundColor Gray
    if ($verBody.git_sha -ne $Sha) {
        Write-Host "    SMOKE FAILED: /version git_sha=$($verBody.git_sha) (want $Sha)" -ForegroundColor Red
        exit 1
    }
    Write-Host "        OK: /version git_sha=$Sha matches deployed image" -ForegroundColor Green
} catch {
    Write-Host "    SMOKE FAILED (version): $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

# (b) ECS Exec ps-check on a running worker task: expect PID 1 / celery uid=10001.
# IMPORTANT: do NOT use `id` here -- ECS Exec spawns its own session shell as
# root inside the container (the `ssm-session-worker` process), so `id` reports
# the session worker's uid (always root), NOT the container's main process uid.
# The canonical diagnostic is `ps -eo pid,user,uid,gid,comm` and reading the
# PID 1 / application-process row. See ~~D-worker-runs-as-root-in-container-2026-05-22~~
# closure diagnostic-correction note in docs/DRIFTS.md.
Write-Host "    (b) ECS Exec 'ps -eo' on worker task -- expect PID 1: celery user=luciel uid=10001" -ForegroundColor Gray
$WorkerTaskArn = aws ecs list-tasks `
    --cluster $Cluster `
    --service-name $WorkerService `
    --desired-status RUNNING `
    --region $AwsRegion `
    --query 'taskArns[0]' --output text
if ([string]::IsNullOrWhiteSpace($WorkerTaskArn) -or $WorkerTaskArn -eq "None") {
    Write-Host "        WARN: list-tasks returned none -- run manually:" -ForegroundColor Yellow
    Write-Host "          aws ecs list-tasks --cluster $Cluster --service-name $WorkerService --region $AwsRegion" -ForegroundColor Yellow
} else {
    Write-Host "        worker task: $WorkerTaskArn" -ForegroundColor Gray
    Write-Host ""
    Write-Host "        Run this OPERATOR command in a separate window to confirm:" -ForegroundColor Yellow
    Write-Host "          aws ecs execute-command ``" -ForegroundColor White
    Write-Host "            --cluster $Cluster ``" -ForegroundColor White
    Write-Host "            --task $WorkerTaskArn ``" -ForegroundColor White
    Write-Host "            --container $WorkerContainer --interactive ``" -ForegroundColor White
    Write-Host "            --command 'ps -eo pid,user,uid,gid,comm' ``" -ForegroundColor White
    Write-Host "            --region $AwsRegion" -ForegroundColor White
    Write-Host ""
    Write-Host "        Expected output row: '    1 luciel   10001 10001 celery'" -ForegroundColor Yellow
    Write-Host "        (do NOT use 'id' here -- it reports the ECS Exec session worker's uid (root), not PID 1)" -ForegroundColor Yellow
    Write-Host ""
}

# (c) Worker boot-log SecurityWarning check (informational; not auto-fail).
Write-Host "    (c) Worker boot log SecurityWarning check (manual)" -ForegroundColor Gray
Write-Host "        Run:" -ForegroundColor Yellow
Write-Host "          aws logs tail /ecs/luciel-worker --since 5m --region $AwsRegion ``" -ForegroundColor White
Write-Host "            | Select-String -Pattern 'SecurityWarning'" -ForegroundColor White
Write-Host "        Expected: zero matches." -ForegroundColor Yellow
Write-Host ""

Write-Host ""
Write-Host "==> Arc 8 WU-2 + WU-3 rollout COMPLETE" -ForegroundColor Green
Write-Host "    git sha:          $Sha"
Write-Host "    image digest:     $Digest"
Write-Host "    backend task-def: $BackendNewArn"
Write-Host "    worker task-def:  $WorkerNewArn"
Write-Host ""
Write-Host "    Next steps (operator):" -ForegroundColor Cyan
Write-Host "    1. Run the ECS Exec 'ps -eo' check on the worker task (above)"
Write-Host "       and confirm PID 1 row shows celery as luciel/10001."
Write-Host "    2. Tail /ecs/luciel-worker logs and confirm no SecurityWarning."
Write-Host "    3. Update DRIFTS.md: close"
Write-Host "         D-worker-runs-as-root-in-container-2026-05-22"
Write-Host "         D-version-endpoint-hardcoded-not-build-sha-2026-05-22"
Write-Host "       with closure stanzas carrying the deploy evidence."
Write-Host ""
