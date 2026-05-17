# scripts/deploy_30a3.ps1
# Step 30a.3 -- password-auth (mandatory at signup, Option B) rollout
#
# What this deploys:
#   1. New luciel-backend image carrying:
#        * /api/v1/auth router (login, set-password, forgot-password)
#        * AuthService (argon2id) + MagicLinkService set/reset token classes
#        * Webhook + /onboarding/claim send welcome-set-password emails
#        * 27 new contract tests
#   2. Alembic migration a3c1f08b9d42 (users.password_hash column,
#      down_revision = dfea1a04e037). Operator-run via ECS exec BEFORE
#      the service is updated -- same pattern as deploy_30a2.sh §4.
#
# What this does NOT deploy:
#   - Any worker change. Step 30a.3 is backend-only; the worker image
#     stays on the existing task-def revision.
#   - The marketing site. That was deployed separately via
#     aryanonline/Luciel-Website commit 13b431e (its own Vercel/CF
#     pipeline).
#
# Idempotency:
#   Re-running with the same SHA is a no-op for the ECR push (the tag
#   already exists) and re-registers an identical task-def (new revision
#   number, same image+command). The service-update at the end is what
#   actually triggers placement.
#
# Container-name convention: the backend task-def container is "web"
# (confirmed via deploy_30a2.sh and deploy_27c.sh -- both reference
# `--container web` in the ECS exec block).

$ErrorActionPreference = "Stop"

# ----- Constants -----
$AwsRegion     = "ca-central-1"
$AccountId     = "729005488042"
$EcrRepo       = "$AccountId.dkr.ecr.$AwsRegion.amazonaws.com/luciel-backend"
$Cluster       = "luciel-cluster"
$WebService    = "luciel-backend-service"
$ContainerName = "web"

# ----- Preflight 0a: clean workspace -----
Write-Host "==> [0/6] Preflight: confirm workspace is clean" -ForegroundColor Cyan
$dirty = git status --porcelain
if ($dirty) {
    Write-Host "ERROR: workspace has uncommitted changes. Commit or stash first." -ForegroundColor Red
    git status --short
    exit 1
}

# ----- Preflight 0b: migration file present + chain correct -----
$RepoRoot = (git rev-parse --show-toplevel).Trim()
$MigrationFile = Join-Path $RepoRoot "alembic/versions/a3c1f08b9d42_step30a_3_users_password_hash.py"
if (-not (Test-Path $MigrationFile)) {
    Write-Host "ERROR: expected migration file not found:" -ForegroundColor Red
    Write-Host "       $MigrationFile" -ForegroundColor Red
    exit 1
}
$migContent = Get-Content $MigrationFile -Raw
if ($migContent -notmatch 'revision\s*=\s*"a3c1f08b9d42"' -or
    $migContent -notmatch 'down_revision\s*=\s*"dfea1a04e037"') {
    Write-Host "ERROR: migration chain mismatch -- expected a3c1f08b9d42 down_revision dfea1a04e037" -ForegroundColor Red
    exit 1
}
Write-Host "    migration: alembic/versions/a3c1f08b9d42_step30a_3_users_password_hash.py" -ForegroundColor Gray
Write-Host "    chain:     a3c1f08b9d42 -> dfea1a04e037 (verified)" -ForegroundColor Gray

# ----- Resolve image tag -----
$Sha   = (git rev-parse --short HEAD).Trim()
$Tag   = "step30a3-$Sha"
$Image = "${EcrRepo}:${Tag}"
Write-Host ""
Write-Host "==> Step 30a.3 rollout starting" -ForegroundColor Cyan
Write-Host "    git sha:   $Sha"
Write-Host "    image:     $Image"
Write-Host "    cluster:   $Cluster"
Write-Host ""

# ----- [1/6] Build + push -----
Write-Host "==> [1/6] Building image" -ForegroundColor Cyan
docker build -t $Image .
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: docker build failed" -ForegroundColor Red; exit 1 }

Write-Host "==> [1/6] ECR login + push" -ForegroundColor Cyan
$loginPwd = aws ecr get-login-password --region $AwsRegion
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: ecr get-login-password failed" -ForegroundColor Red; exit 1 }
$loginPwd | docker login --username AWS --password-stdin $EcrRepo
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: docker login failed" -ForegroundColor Red; exit 1 }

docker push $Image
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: docker push failed" -ForegroundColor Red; exit 1 }

# Pin to digest so the task-def is immutable against tag overwrite.
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

# ----- [2/6] Register new backend task-def revision -----
Write-Host ""
Write-Host "==> [2/6] Registering luciel-backend:NEW (image swap only)" -ForegroundColor Cyan
$currentJsonPath = Join-Path $env:TEMP "backend-current-30a3.json"
$newJsonPath     = Join-Path $env:TEMP "backend-30a3.json"

# Pull the current task-def via the AWS CLI's native JSON output and
# write it to disk byte-for-byte (no PS object round-trip).
aws ecs describe-task-definition `
    --task-definition luciel-backend `
    --region $AwsRegion `
    --query 'taskDefinition' `
    --output json | Out-File -FilePath $currentJsonPath -Encoding ascii
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: describe-task-definition failed" -ForegroundColor Red; exit 1 }

# Field-filter + image swap via Python instead of PowerShell's
# ConvertTo-Json. Rationale: PS's serializer collapses empty arrays
# (placementConstraints=[]) to nothing on round-trip, drops type
# information on nested objects, and Set-Content -Encoding utf8 on
# Windows PowerShell 5.1 emits a BOM that aws-cli rejects with the
# opaque 'Invalid JSON received' message. Python json round-trips
# the shape losslessly and writes plain UTF-8.
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

for c in filtered.get('containerDefinitions', []):
    if c.get('name') == container:
        c['image'] = image

with open(out_path, 'w', encoding='utf-8', newline='') as f:
    json.dump(filtered, f)
"@

$pyScriptPath = Join-Path $env:TEMP "backend-30a3-rewrite.py"
Set-Content -Path $pyScriptPath -Value $pyScript -Encoding ascii

& $pythonExe $pyScriptPath $currentJsonPath $newJsonPath $ContainerName $PinnedImage
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: JSON rewrite failed" -ForegroundColor Red; exit 1 }

$BackendNewArn = aws ecs register-task-definition `
    --cli-input-json "file://$newJsonPath" `
    --region $AwsRegion `
    --query 'taskDefinition.taskDefinitionArn' --output text
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($BackendNewArn)) {
    Write-Host "ERROR: register-task-definition failed" -ForegroundColor Red
    exit 1
}
Write-Host "    registered: $BackendNewArn" -ForegroundColor Gray

# ----- [3/6] Alembic migration (OPERATOR-RUN via ECS exec) -----
Write-Host ""
Write-Host "==> [3/6] Alembic migration a3c1f08b9d42 -- OPERATOR-RUN" -ForegroundColor Yellow

# Resolve a running backend task ARN so we can give the operator a
# ready-to-paste exec command (saves a `aws ecs list-tasks` round-trip).
$RunningTaskArn = aws ecs list-tasks `
    --cluster $Cluster `
    --service-name $WebService `
    --desired-status RUNNING `
    --region $AwsRegion `
    --query 'taskArns[0]' --output text
if ([string]::IsNullOrWhiteSpace($RunningTaskArn) -or $RunningTaskArn -eq "None") {
    $RunningTaskArn = "<list-tasks-returned-none -- run aws ecs list-tasks --cluster $Cluster --service-name $WebService --region $AwsRegion>"
}

Write-Host ""
Write-Host "    PAUSE: run the migration BEFORE updating the service." -ForegroundColor Yellow
Write-Host "    The currently-running task is on the OLD image; that's fine --" -ForegroundColor Yellow
Write-Host "    alembic only needs DATABASE_URL, which is identical across" -ForegroundColor Yellow
Write-Host "    revisions. New code on the OLD container is harmless." -ForegroundColor Yellow
Write-Host ""
Write-Host "    Command (run from this PowerShell window):" -ForegroundColor Yellow
Write-Host ""
Write-Host "      aws ecs execute-command ``" -ForegroundColor White
Write-Host "        --cluster $Cluster ``" -ForegroundColor White
Write-Host "        --task $RunningTaskArn ``" -ForegroundColor White
Write-Host "        --container $ContainerName --interactive ``" -ForegroundColor White
Write-Host "        --command 'alembic upgrade head' ``" -ForegroundColor White
Write-Host "        --region $AwsRegion" -ForegroundColor White
Write-Host ""
Write-Host "    Expected: 'Running upgrade dfea1a04e037 -> a3c1f08b9d42'." -ForegroundColor Yellow
Write-Host ""
$null = Read-Host "    Press ENTER once the upgrade reports the new revision (or Ctrl-C to abort)"

# ----- [4/6] Update backend service to new task-def -----
Write-Host ""
Write-Host "==> [4/6] Updating backend service to $BackendNewArn" -ForegroundColor Cyan
$updatedTd = aws ecs update-service `
    --cluster $Cluster `
    --service $WebService `
    --task-definition $BackendNewArn `
    --region $AwsRegion `
    --query 'service.taskDefinition' --output text
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: update-service failed" -ForegroundColor Red; exit 1 }
Write-Host "    service now on: $updatedTd" -ForegroundColor Gray

# ----- [5/6] Wait for service to stabilize -----
Write-Host ""
Write-Host "==> [5/6] Waiting for backend service to stabilize" -ForegroundColor Cyan
Write-Host "    (ECS rolling deploy + ALB health checks -- typically 2-4 min)" -ForegroundColor Gray
aws ecs wait services-stable `
    --cluster $Cluster `
    --services $WebService `
    --region $AwsRegion
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: services-stable wait failed" -ForegroundColor Red; exit 1 }

# ----- [6/6] Smoke -----
Write-Host ""
Write-Host "==> [6/6] Smoke: probing /api/v1/auth/forgot-password (must 200)" -ForegroundColor Cyan
# Forgot-password is the cheapest, side-effect-free probe -- backend
# always returns 200 with a generic body whether the email exists or
# not (no enumeration). A 200 here proves the new router is mounted,
# the SKIP_AUTH_PATHS exemption works, and argon2id+JWT init didn't
# crash the container.
$smokeProbeEmail = "deploy-smoke-30a3-$Sha@vantagemind.invalid"
try {
    $smokeResp = Invoke-WebRequest `
        -Uri "https://api.vantagemind.ai/api/v1/auth/forgot-password" `
        -Method POST `
        -ContentType "application/json" `
        -Body (@{ email = $smokeProbeEmail } | ConvertTo-Json -Compress) `
        -UseBasicParsing `
        -TimeoutSec 15
    if ($smokeResp.StatusCode -ne 200) {
        Write-Host "    SMOKE FAILED: status $($smokeResp.StatusCode)" -ForegroundColor Red
        Write-Host "    body: $($smokeResp.Content)" -ForegroundColor Red
        exit 1
    }
    Write-Host "    smoke OK: 200 on /api/v1/auth/forgot-password" -ForegroundColor Green
} catch {
    Write-Host "    SMOKE FAILED with exception: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "==> Step 30a.3 rollout COMPLETE" -ForegroundColor Green
Write-Host "    backend task-def: $BackendNewArn"
Write-Host "    image digest:     $Digest"
Write-Host ""
Write-Host "    Next steps (operator):" -ForegroundColor Cyan
Write-Host "    1. Live E2E -- buy the Individual tier (`$100) from a clean browser:"
Write-Host "         a. Open https://www.vantagemind.ai/pricing in incognito"
Write-Host "         b. Click Start 90-day pilot -> Stripe Checkout"
Write-Host "         c. Land on /onboarding (passive 'check your email' copy)"
Write-Host "         d. Receive welcome email, click /auth/set-password?token=... link"
Write-Host "         e. Set a password >=8 chars -> auto-navigated to /dashboard"
Write-Host "         f. Sign out, sign back in at /login with email+password"
Write-Host "    2. Close drift D-magic-link-only-auth-no-password-fallback-2026-05-16:"
Write-Host "         move to DRIFTS §5 with strikethrough on the §3 entry."
Write-Host ""
