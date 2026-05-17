# scripts/deploy_30a3_rollforward.ps1
#
# Roll-forward fix for the 2026-05-16 Step 30a.3 first-attempt failure.
#
# What went wrong in deploy_30a3.ps1's first run:
#   The script's $ContainerName was hard-coded to "web", but the actual
#   container in the luciel-backend task-def is named "luciel-backend"
#   (renamed sometime between Step 30a.2 and now -- exact when unknown,
#   but verified live via `describe-tasks ... containers[].name`).
#   The Python rewrite loop in [2/6] iterated containerDefinitions
#   looking for name=='web' and silently no-op'd, so task-def revision
#   :62 was registered carrying the same image as :61 (the prior
#   pre-Step-30a.3 image, digest sha256:e7506936...). ECS rolled the
#   service to :62, but :62 == :61 image-wise, so the container kept
#   serving pre-30a.3 code. Symptoms: alembic current returned
#   dfea1a04e037 (head) instead of a3c1f08b9d42, and /api/v1/auth/*
#   returned the api-key middleware 401 because the SKIP_AUTH_PATHS
#   entry for "/api/v1/auth" wasn't in the running code.
#
# What this script does:
#   1. Re-register the task-def with the CORRECT container name and
#      the already-pushed pinned image
#      (sha256:ca8f24e8bd3c127b2808585ce3d4f6ab4ab9c2e8eec6e582954e954e65ac73a2).
#   2. Verify the rewrite landed before registering (the same belt-and-
#      braces check now baked into deploy_30a3.ps1 for the next run).
#   3. Update the service.
#   4. Wait for stability.
#   5. Print the exec command for the operator to run the migration.
#   6. After ENTER, smoke /api/v1/auth/forgot-password.
#
# Idempotency: re-running this is safe. If the service is already on
# the new image, the register/update steps just produce a new (identical)
# revision and a no-op deploy.

$ErrorActionPreference = "Stop"

$AwsRegion     = "ca-central-1"
$Cluster       = "luciel-cluster"
$WebService    = "luciel-backend-service"
$ContainerName = "luciel-backend"
$EcrRepo       = "729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend"

# Image was already pushed in the first attempt of deploy_30a3.ps1.
# Pinned by digest so this is immutable against tag drift.
$PinnedImage = "$EcrRepo@sha256:ca8f24e8bd3c127b2808585ce3d4f6ab4ab9c2e8eec6e582954e954e65ac73a2"

Write-Host "==> Step 30a.3 ROLL-FORWARD" -ForegroundColor Cyan
Write-Host "    target image: $PinnedImage"
Write-Host "    container:    $ContainerName"
Write-Host ""

# ----- [1/5] Re-register task-def with CORRECT container -----
Write-Host "==> [1/5] Re-registering luciel-backend task-def" -ForegroundColor Cyan

$currentJsonPath = Join-Path $env:TEMP "rf-backend-current.json"
$newJsonPath     = Join-Path $env:TEMP "rf-backend-new.json"

aws ecs describe-task-definition `
    --task-definition luciel-backend `
    --region $AwsRegion `
    --query 'taskDefinition' `
    --output json | Out-File -FilePath $currentJsonPath -Encoding ascii
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: describe-task-definition failed" -ForegroundColor Red; exit 1 }

# Confirm the container is actually in the task-def before we waste a revision.
$containerNames = aws ecs describe-task-definition `
    --task-definition luciel-backend `
    --region $AwsRegion `
    --query 'taskDefinition.containerDefinitions[].name' --output text
if ($containerNames -notmatch "\b$ContainerName\b") {
    Write-Host "ERROR: container '$ContainerName' not present in task-def. Found: $containerNames" -ForegroundColor Red
    exit 1
}
Write-Host "    container '$ContainerName' confirmed in task-def" -ForegroundColor Gray

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

swapped = 0
for c in filtered.get('containerDefinitions', []):
    if c.get('name') == container:
        c['image'] = image
        swapped += 1
if swapped != 1:
    raise SystemExit(f'expected exactly 1 image swap, did {swapped}')

with open(out_path, 'w', encoding='utf-8', newline='') as f:
    json.dump(filtered, f)
print(f'swapped {container} -> {image}')
"@

$pyScriptPath = Join-Path $env:TEMP "rf-backend-rewrite.py"
Set-Content -Path $pyScriptPath -Value $pyScript -Encoding ascii

& $pythonExe $pyScriptPath $currentJsonPath $newJsonPath $ContainerName $PinnedImage
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: JSON rewrite failed" -ForegroundColor Red; exit 1 }

# Post-rewrite verification
$rewrittenImage = & $pythonExe -c "import json; d=json.load(open(r'$newJsonPath','r',encoding='utf-8-sig')); print([c['image'] for c in d['containerDefinitions'] if c['name']=='$ContainerName'][0])"
if ($rewrittenImage.Trim() -ne $PinnedImage) {
    Write-Host "ERROR: image swap did not take. Got: $rewrittenImage" -ForegroundColor Red
    Write-Host "       Expected: $PinnedImage" -ForegroundColor Red
    exit 1
}
Write-Host "    image-swap verified" -ForegroundColor Gray

$BackendNewArn = aws ecs register-task-definition `
    --cli-input-json "file://$newJsonPath" `
    --region $AwsRegion `
    --query 'taskDefinition.taskDefinitionArn' --output text
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($BackendNewArn)) {
    Write-Host "ERROR: register-task-definition failed" -ForegroundColor Red
    exit 1
}
Write-Host "    registered: $BackendNewArn" -ForegroundColor Gray

# ----- [2/5] Update service -----
Write-Host ""
Write-Host "==> [2/5] Updating service to $BackendNewArn" -ForegroundColor Cyan
$updatedTd = aws ecs update-service `
    --cluster $Cluster `
    --service $WebService `
    --task-definition $BackendNewArn `
    --region $AwsRegion `
    --query 'service.taskDefinition' --output text
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: update-service failed" -ForegroundColor Red; exit 1 }
Write-Host "    service now on: $updatedTd" -ForegroundColor Gray

# ----- [3/5] Wait -----
Write-Host ""
Write-Host "==> [3/5] Waiting for service stability (2-4 min)" -ForegroundColor Cyan
aws ecs wait services-stable `
    --cluster $Cluster `
    --services $WebService `
    --region $AwsRegion
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: services-stable wait failed" -ForegroundColor Red; exit 1 }

# Verify the running container is actually on the new image now.
$task = aws ecs list-tasks `
    --cluster $Cluster `
    --service-name $WebService `
    --desired-status RUNNING `
    --region $AwsRegion `
    --query 'taskArns[0]' --output text
$runningImage = aws ecs describe-tasks `
    --cluster $Cluster `
    --tasks $task `
    --region $AwsRegion `
    --query 'tasks[0].containers[0].image' --output text
if ($runningImage.Trim() -ne $PinnedImage) {
    Write-Host "ERROR: running container is on $runningImage, expected $PinnedImage" -ForegroundColor Red
    exit 1
}
Write-Host "    running image confirmed: $runningImage" -ForegroundColor Green

# ----- [4/5] Migration (operator-run) -----
Write-Host ""
Write-Host "==> [4/5] Migration -- OPERATOR-RUN" -ForegroundColor Yellow
Write-Host ""
Write-Host "    aws ecs execute-command ``" -ForegroundColor White
Write-Host "      --cluster $Cluster ``" -ForegroundColor White
Write-Host "      --task $task ``" -ForegroundColor White
Write-Host "      --container $ContainerName --interactive ``" -ForegroundColor White
Write-Host "      --command 'alembic upgrade head' ``" -ForegroundColor White
Write-Host "      --region $AwsRegion" -ForegroundColor White
Write-Host ""
Write-Host "    Expected: 'Running upgrade dfea1a04e037 -> a3c1f08b9d42'" -ForegroundColor Yellow
Write-Host ""
Write-Host "    Then VERIFY before pressing ENTER (must print 'a3c1f08b9d42 (head)'):" -ForegroundColor Yellow
Write-Host "    aws ecs execute-command ``" -ForegroundColor White
Write-Host "      --cluster $Cluster --task $task ``" -ForegroundColor White
Write-Host "      --container $ContainerName --interactive ``" -ForegroundColor White
Write-Host "      --command 'alembic current' --region $AwsRegion" -ForegroundColor White
Write-Host ""
$null = Read-Host "    Press ENTER only after 'alembic current' reports a3c1f08b9d42 (head)"

# ----- [5/5] Smoke -----
Write-Host ""
Write-Host "==> [5/5] Smoke: /api/v1/auth/forgot-password (must 200)" -ForegroundColor Cyan
$smokeEmail = "rf-smoke-30a3@vantagemind.invalid"
try {
    $resp = Invoke-WebRequest `
        -Uri "https://api.vantagemind.ai/api/v1/auth/forgot-password" `
        -Method POST `
        -ContentType "application/json" `
        -Body (@{ email = $smokeEmail } | ConvertTo-Json -Compress) `
        -UseBasicParsing `
        -TimeoutSec 15
    if ($resp.StatusCode -ne 200) {
        Write-Host "    SMOKE FAILED: $($resp.StatusCode)" -ForegroundColor Red
        Write-Host "    body: $($resp.Content)" -ForegroundColor Red
        exit 1
    }
    Write-Host "    smoke OK (200): $($resp.Content)" -ForegroundColor Green
} catch {
    Write-Host "    SMOKE FAILED: $($_.Exception.Message)" -ForegroundColor Red
    if ($_.Exception.Response) {
        $errReader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
        Write-Host "    body: $($errReader.ReadToEnd())" -ForegroundColor Red
    }
    exit 1
}

Write-Host ""
Write-Host "==> ROLL-FORWARD COMPLETE" -ForegroundColor Green
Write-Host "    Next: live E2E paid signup test (Individual tier, `$100)" -ForegroundColor Cyan
