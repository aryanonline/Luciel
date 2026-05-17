# scripts/deploy_30a4.ps1
# Step 30a.4 -- Team-tier self-serve teammate invites rollout
#
# What this deploys:
#   1. New luciel-backend image carrying:
#        * UserInvite model (app/models/user_invite.py)
#        * UserInviteRepository (app/repositories/user_invites.py)
#        * InviteService (app/services/invite_service.py) -- module-level
#          create_invite / redeem_invite / resend_invite / revoke_invite
#        * 4 new /admin/invites routes + /auth/set-password invite-purpose
#          branch (app/api/v1/admin.py + app/api/v1/auth.py)
#        * UserInviteCreate / UserInviteRead / UserInviteResendResponse /
#          UserInviteRevokeResponse Pydantic schemas (app/schemas/invite.py)
#        * USER_INVITED / INVITE_REDEEMED / INVITE_RESENT / INVITE_REVOKED
#          audit constants whitelisted in AdminAuditLog
#        * Deprecation marker on the Step 30a.1 teammate_email overload
#          on /admin/luciel-instances POST (removal at Step 30a.5)
#        * 28 new contract tests + 1 env-gated live e2e harness
#   2. Alembic migration e7b2c9d4a18f (user_invites table, down_revision
#      = a3c1f08b9d42). Operator-run via ECS exec BEFORE the service is
#      updated -- same pattern as deploy_30a3.ps1 §3.
#
# What this does NOT deploy:
#   - Any worker change. Step 30a.4 is backend-only on this repo; the
#     worker image stays on the existing task-def revision.
#   - The /app/team UI. That ships from aryanonline/Luciel-Website at
#     its own Amplify pipeline (deploy commit G).
#
# Idempotency:
#   Re-running with the same SHA is a no-op for the ECR push (the tag
#   already exists) and re-registers an identical task-def (new revision
#   number, same image+command). The service-update at the end is what
#   actually triggers placement.
#
# Container-name convention: backend task-def container is "luciel-backend"
# (verified against luciel-backend:64 + :65 on 2026-05-17). The earlier deploy
# scripts (deploy_27c.sh, deploy_30a2.sh, deploy_30a3.ps1) all hardcoded "web"
# but the Python patcher silently no-op'd on missing container, registering
# image-name-only copies of the source task-def. This script hard-fails if the
# container is not found in the source task-def -- see [2/6] patcher guard.

$ErrorActionPreference = "Stop"

# ----- Constants -----
$AwsRegion     = "ca-central-1"
$AccountId     = "729005488042"
$EcrRepo       = "$AccountId.dkr.ecr.$AwsRegion.amazonaws.com/luciel-backend"
$Cluster       = "luciel-cluster"
$WebService    = "luciel-backend-service"
$ContainerName = "luciel-backend"

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
$MigrationFile = Join-Path $RepoRoot "alembic/versions/e7b2c9d4a18f_step30a_4_user_invites_table.py"
if (-not (Test-Path $MigrationFile)) {
    Write-Host "ERROR: expected migration file not found:" -ForegroundColor Red
    Write-Host "       $MigrationFile" -ForegroundColor Red
    exit 1
}
$migContent = Get-Content $MigrationFile -Raw
if ($migContent -notmatch 'revision\s*=\s*"e7b2c9d4a18f"' -or
    $migContent -notmatch 'down_revision\s*=\s*"a3c1f08b9d42"') {
    Write-Host "ERROR: migration chain mismatch -- expected e7b2c9d4a18f down_revision a3c1f08b9d42" -ForegroundColor Red
    exit 1
}
Write-Host "    migration: alembic/versions/e7b2c9d4a18f_step30a_4_user_invites_table.py" -ForegroundColor Gray
Write-Host "    chain:     e7b2c9d4a18f -> a3c1f08b9d42 (verified)" -ForegroundColor Gray

# ----- Resolve image tag -----
$Sha   = (git rev-parse --short HEAD).Trim()
$Tag   = "step30a4-$Sha"
$Image = "${EcrRepo}:${Tag}"
Write-Host ""
Write-Host "==> Step 30a.4 rollout starting" -ForegroundColor Cyan
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
$currentJsonPath = Join-Path $env:TEMP "backend-current-30a4.json"
$newJsonPath     = Join-Path $env:TEMP "backend-30a4.json"

aws ecs describe-task-definition `
    --task-definition luciel-backend `
    --region $AwsRegion `
    --query 'taskDefinition' `
    --output json | Out-File -FilePath $currentJsonPath -Encoding ascii
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: describe-task-definition failed" -ForegroundColor Red; exit 1 }

# Field-filter + image swap via Python (see deploy_30a3.ps1 for rationale).
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

$pyScriptPath = Join-Path $env:TEMP "backend-30a4-rewrite.py"
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

# ----- [3/6] Update backend service to new task-def -----
# Migration order note: we deploy the new image FIRST, then run alembic
# from the new task. The original sequence (migrate before service swap)
# does not work because alembic reads migration files from the container
# filesystem -- the old image does not contain alembic/versions/e7b2c9d4a18f_*.
# Running 'alembic upgrade head' against the old container is a silent
# no-op (head still resolves to a3c1f08b9d42). The new code is forward-safe
# against the old schema during the 2-4 minute rolling deploy: the four new
# /admin/invites routes are cookie-gated to Team-tier admin sessions and
# would only 500 if hit before the migration runs (near-zero hit rate;
# no auto-traffic).
Write-Host ""
Write-Host "==> [3/6] Updating backend service to $BackendNewArn" -ForegroundColor Cyan
$updatedTd = aws ecs update-service `
    --cluster $Cluster `
    --service $WebService `
    --task-definition $BackendNewArn `
    --region $AwsRegion `
    --query 'service.taskDefinition' --output text
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: update-service failed" -ForegroundColor Red; exit 1 }
Write-Host "    service now on: $updatedTd" -ForegroundColor Gray

# ----- [4/6] Wait for service to stabilize -----
Write-Host ""
Write-Host "==> [4/6] Waiting for backend service to stabilize" -ForegroundColor Cyan
Write-Host "    (ECS rolling deploy + ALB health checks -- typically 2-4 min)" -ForegroundColor Gray
aws ecs wait services-stable `
    --cluster $Cluster `
    --services $WebService `
    --region $AwsRegion
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: services-stable wait failed" -ForegroundColor Red; exit 1 }

# ----- [5/6] Alembic migration on NEW container (OPERATOR-RUN via ECS exec) -----
Write-Host ""
Write-Host "==> [5/6] Alembic migration e7b2c9d4a18f -- OPERATOR-RUN on NEW image" -ForegroundColor Yellow

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
Write-Host "    PAUSE: run the migration NOW on the new task ($RunningTaskArn)." -ForegroundColor Yellow
Write-Host "    The new image ships alembic/versions/e7b2c9d4a18f_*.py, so" -ForegroundColor Yellow
Write-Host "    'alembic upgrade head' will apply the new revision and" -ForegroundColor Yellow
Write-Host "    'alembic current' will report e7b2c9d4a18f (head)." -ForegroundColor Yellow
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
Write-Host "    Expected: 'Running upgrade a3c1f08b9d42 -> e7b2c9d4a18f'." -ForegroundColor Yellow
Write-Host ""
Write-Host "    Then VERIFY with (must print 'e7b2c9d4a18f (head)'):" -ForegroundColor Yellow
Write-Host "      aws ecs execute-command ``" -ForegroundColor White
Write-Host "        --cluster $Cluster --task $RunningTaskArn ``" -ForegroundColor White
Write-Host "        --container $ContainerName --interactive ``" -ForegroundColor White
Write-Host "        --command 'alembic current' --region $AwsRegion" -ForegroundColor White
Write-Host ""
$null = Read-Host "    Press ENTER only after 'alembic current' reports e7b2c9d4a18f (head) (or Ctrl-C to abort)"

# ----- [6/6] Smoke (post-migration) -----
Write-Host ""
Write-Host "==> [6/6] Smoke: probing /api/v1/admin/invites with no cookie (must 401)" -ForegroundColor Cyan
# A 401 (no session cookie) proves the new admin/invites router is
# mounted and cookie-gated. We deliberately do NOT smoke-test the
# happy path here -- that requires a real cookied session and is
# covered by the live e2e harness against dev-Postgres, not prod.
try {
    $smokeResp = Invoke-WebRequest `
        -Uri "https://api.vantagemind.ai/api/v1/admin/invites" `
        -Method POST `
        -ContentType "application/json" `
        -Body '{"invited_email":"deploy-smoke-30a4@vantagemind.ai","role":"teammate"}' `
        -UseBasicParsing `
        -TimeoutSec 15 `
        -SkipHttpErrorCheck
    if ($smokeResp.StatusCode -ne 401) {
        Write-Host "    SMOKE FAILED: status $($smokeResp.StatusCode) (want 401)" -ForegroundColor Red
        Write-Host "    body: $($smokeResp.Content)" -ForegroundColor Red
        exit 1
    }
    Write-Host "    smoke OK: 401 on POST /api/v1/admin/invites (no cookie, route mounted)" -ForegroundColor Green
} catch {
    Write-Host "    SMOKE FAILED with exception: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "==> Step 30a.4 rollout COMPLETE" -ForegroundColor Green
Write-Host "    backend task-def: $BackendNewArn"
Write-Host "    image digest:     $Digest"
Write-Host ""
Write-Host "    Next steps (operator):" -ForegroundColor Cyan
Write-Host "    1. Deploy the /app/team UI from aryanonline/Luciel-Website"
Write-Host "       (commit G -- AppTeam.tsx + AppInviteAccept.tsx)."
Write-Host "    2. Cookied-session smoke (after UI is live):"
Write-Host "         a. Open https://www.vantagemind.ai in incognito, log in"
Write-Host "            as a Team-tier tenant owner."
Write-Host "         b. Navigate to /app/team -- pending invites list renders."
Write-Host "         c. Invite a teammate -- 201 + invite appears in pending list."
Write-Host "         d. Open the welcome email -> /auth/set-password?token=..."
Write-Host "            redemption mints User + Agent + ScopeAssignment."
Write-Host "         e. Refresh /app/team -- invite shows accepted, agent visible."
Write-Host "    3. Close drift D-team-self-serve-incomplete-invite-ui-missing-2026-05-16:"
Write-Host "         move to DRIFTS §5 with strikethrough on the §3 entry"
Write-Host "         (commit H, already locally-committed -- pop the stash)."
Write-Host ""
