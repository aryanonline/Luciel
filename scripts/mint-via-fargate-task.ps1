<#
.SYNOPSIS
  Pattern N variant mint ceremony: assume the luciel-mint-operator-role
  with MFA, then launch the mint as an ECS Fargate one-shot task that
  runs INSIDE the production VPC with the dedicated luciel-ecs-mint-role
  task role. Tail CloudWatch Logs on the laptop while the task runs.

.DESCRIPTION
  This is the P3-S.a replacement for `scripts/mint-with-assumed-role.ps1`.
  It exists because the prior Option 3 ceremony assumed the operator
  could `psycopg.connect(admin_dsn)` from their laptop, which is
  incompatible with the production VPC posture (RDS in private subnet,
  no public ingress, no bastion, no VPN). See
  `docs/recaps/2026-05-04-mint-architectural-boundary-pause.md` for the
  full forensic narrative and `docs/PHASE_3_COMPLIANCE_BACKLOG.md` P3-S
  for the architectural decision.

  Architectural shape (Pattern N variant):
    1. Operator runs THIS script on their laptop.
    2. Script prompts for MFA TOTP, calls sts:AssumeRole on
       `luciel-mint-operator-role` with --serial-number + --token-code.
       AssumeRole's only purpose at the laptop layer is to scope the
       blast radius of an `aws ecs run-task` call: with the assumed
       creds, the operator can launch the mint task and tail logs;
       without them, they cannot.
    3. Script issues `aws ecs run-task` against the dedicated
       `luciel-mint:N` task definition. The task launches in the
       application subnets, picks up its task role
       (`luciel-ecs-mint-role`) which holds the same 5 IAM statements
       the operator role holds. The task role is what the running
       container uses for AWS API calls (admin DSN read, worker SSM
       write, KMS decrypt/encrypt). The container runs
       `python -m scripts.mint_worker_db_password_ssm` against RDS
       inside the VPC.
    4. Script waits for task RUNNING -> STOPPED, polling
       `aws ecs describe-tasks` and tailing
       `/ecs/luciel-backend` log stream `mint/luciel-backend/<task-id>`.
       Operator sees the same stdout/stderr they would have seen on
       their laptop, redaction-safe (Pattern E discipline preserved by
       the script's `_redact_dsn_in_message`).
    5. On task STOPPED, script reads the task's exitCode from
       describe-tasks. exit 0 = mint succeeded (or dry-run completed).
       Anything else = aborted; check logs.
    6. Always-runs finally block clears the assumed credentials from
       the operator session.

  Crucially: the admin DSN no longer traverses the operator laptop at
  all. It is delivered to the container via task-def `secrets:` block,
  which ECS resolves through the SSM endpoint inside the VPC. The
  laptop only sees CloudWatch log lines, which the mint script has
  already passed through `_redact_dsn_in_message`.

.PARAMETER MfaSerial
  The MFA device ARN for luciel-admin. Default value is the value
  recorded at P3-J resolution (2026-05-03):
  arn:aws:iam::729005488042:mfa/Luciel-MFA

.PARAMETER MintRoleArn
  The ARN of the luciel-mint-operator-role created by P3-K. Default
  value is the canonical ARN. The operator role is what THIS script
  assumes on the laptop. The task role
  (`luciel-ecs-mint-role`, defined in
  `infra/iam/luciel-ecs-mint-role-trust-policy.json`) is what the
  running ECS task uses; the task role is referenced from the task
  definition, not from this script.

.PARAMETER Cluster
  ECS cluster (default: luciel-cluster).

.PARAMETER TaskDefinition
  Task definition family or family:revision (default: luciel-mint).
  Pass `luciel-mint:N` to pin a specific revision once one is
  registered. Default uses the latest ACTIVE revision.

.PARAMETER Subnets
  Subnet IDs for the task ENI. MUST be application subnets (the ones
  used by `luciel-backend-service`), not RDS DB subnets — RDS subnets
  have no SSM VPC endpoint and the task will fail with
  ResourceInitializationError. Default values are the production
  application subnets canonicalised in `docs/CANONICAL_RECAP.md`.
  Verify before override:
      aws ecs describe-services --cluster luciel-cluster `
          --services luciel-backend-service `
          --query "services[0].networkConfiguration.awsvpcConfiguration.subnets"

.PARAMETER SecurityGroups
  Security group IDs for the task ENI. MUST match the production
  application SG that has egress to RDS on 5432.

.PARAMETER AssignPublicIp
  Whether to assign a public IP to the task ENI. Default ENABLED to
  match `luciel-backend-service`. The task does NOT need a public IP
  for any of its real work (everything goes through VPC endpoints or
  intra-VPC traffic), but matching the backend service's network
  config keeps subnet/SG troubleshooting consistent — if backend
  reaches AWS APIs from these subnets, mint will too.

.PARAMETER Region
  AWS region (default: ca-central-1).

.PARAMETER WorkerHost
  Override the WORKER_HOST env var baked into the task definition.
  Optional. Pass only when targeting a non-production worker DB
  (staging, etc.) WITHOUT registering a new task-def revision.

.PARAMETER WorkerDbName
  Override WORKER_DB_NAME (default in task-def: luciel). Optional.

.PARAMETER WorkerSsmPath
  Override WORKER_SSM_PATH (default in task-def:
  /luciel/production/worker_database_url). Optional.

.PARAMETER DryRun
  When set, overrides MINT_DRY_RUN=true on the task. The task-def
  ships with MINT_DRY_RUN=true as the safe default; this switch is
  effectively a no-op against the unmodified task-def but is
  preserved for explicit ceremony intent. Pass -RealRun to actually
  mint.

.PARAMETER RealRun
  When set, overrides MINT_DRY_RUN=false on the task — i.e. the
  container will actually `ALTER ROLE` and write to the worker SSM
  parameter. This flag is mutually exclusive with -DryRun and
  REQUIRED for the real mint. The task-def's safe default
  (MINT_DRY_RUN=true) is intentional: an accidental `aws ecs
  run-task` without -RealRun is a no-op, not a real mint.

.PARAMETER PollIntervalSeconds
  How often to re-check task state and pull new log lines while the
  task is running. Default 5.

.PARAMETER MaxWaitMinutes
  Hard ceiling on total wait. Default 10. The mint script's
  pre-flight + connect + ALTER ROLE + SSM put completes in well
  under 60 s in normal operation; anything past this ceiling
  indicates a stuck task and the script aborts the wait (the task
  itself continues running until the ECS-level stop).

.EXAMPLE
  # Dry-run smoke (uses task-def safe default MINT_DRY_RUN=true)
  .\scripts\mint-via-fargate-task.ps1

.EXAMPLE
  # Real mint (Phase 2 Commit 4)
  .\scripts\mint-via-fargate-task.ps1 -RealRun

.EXAMPLE
  # Pin a specific task-def revision
  .\scripts\mint-via-fargate-task.ps1 -TaskDefinition luciel-mint:1 -RealRun

.NOTES
  Author: Aryan Singh
  Created: 2026-05-05 (P3-S.a Half 1)
  Supersedes: scripts/mint-with-assumed-role.ps1 (kept on disk for
  reference; do NOT invoke for prod mint — it cannot reach RDS).
  Cross-references:
    - docs/recaps/2026-05-04-mint-architectural-boundary-pause.md
    - docs/PHASE_3_COMPLIANCE_BACKLOG.md  P3-S
    - docs/runbooks/operator-patterns.md  Pattern N
    - docs/runbooks/step-28-phase-2-deploy.md  §4.0.6 (Pattern N mint architecture)
    - infra/iam/luciel-ecs-mint-role-trust-policy.json
    - infra/iam/luciel-ecs-mint-role-permission-policy.json
    - mint-td-rev1.json  (task-def registration source)
    - scripts/mint_worker_db_password_ssm.py  (the hardened mint script)
#>

[CmdletBinding(DefaultParameterSetName = 'DryRun')]
param(
    [string]$MfaSerial      = "arn:aws:iam::729005488042:mfa/Luciel-MFA",
    [string]$MintRoleArn    = "arn:aws:iam::729005488042:role/luciel-mint-operator-role",

    [string]$Cluster        = "luciel-cluster",
    [string]$TaskDefinition = "luciel-mint",

    [string[]]$Subnets        = @(
        "subnet-0e54df62d1a4463bc",
        "subnet-0e95d953fd553cbd1"
    ),
    [string[]]$SecurityGroups = @(
        "sg-0f2e317f987925601"
    ),
    [ValidateSet("ENABLED", "DISABLED")]
    [string]$AssignPublicIp = "ENABLED",

    [string]$Region         = "ca-central-1",

    [string]$WorkerHost,
    [string]$WorkerDbName,
    [string]$WorkerSsmPath,

    [Parameter(ParameterSetName = 'DryRun')]
    [switch]$DryRun,

    [Parameter(ParameterSetName = 'RealRun', Mandatory = $true)]
    [switch]$RealRun,

    [int]$PollIntervalSeconds = 5,
    [int]$MaxWaitMinutes      = 10
)

$ErrorActionPreference = "Stop"

# ----- Banner -----
Write-Host ""
Write-Host "Pattern N mint ceremony (P3-S.a Fargate task)" -ForegroundColor Cyan
Write-Host "  MFA serial      : $MfaSerial"
Write-Host "  Mint role (laptop AssumeRole): $MintRoleArn"
Write-Host "  Region          : $Region"
Write-Host "  Cluster         : $Cluster"
Write-Host "  Task definition : $TaskDefinition"
Write-Host "  Subnets         : $($Subnets -join ', ')"
Write-Host "  Security groups : $($SecurityGroups -join ', ')"
Write-Host "  Assign public IP: $AssignPublicIp"
if ($WorkerHost)    { Write-Host "  WORKER_HOST override     : $WorkerHost" }
if ($WorkerDbName)  { Write-Host "  WORKER_DB_NAME override  : $WorkerDbName" }
if ($WorkerSsmPath) { Write-Host "  WORKER_SSM_PATH override : $WorkerSsmPath" }
if ($RealRun) {
    Write-Host "  Mode            : REAL RUN (MINT_DRY_RUN=false override)" -ForegroundColor Yellow
} else {
    Write-Host "  Mode            : dry-run (MINT_DRY_RUN=true; task-def default)" -ForegroundColor Green
}
Write-Host ""

# ----- Step 1: prompt for TOTP -----
$tokenCode = Read-Host -Prompt "Enter current MFA 6-digit code"
if ([string]::IsNullOrWhiteSpace($tokenCode)) {
    throw "MFA code is empty; aborting."
}

# ----- Step 2: assume the mint operator role with MFA -----
Write-Host "Calling sts:AssumeRole with MFA..." -ForegroundColor Yellow

$sessionName = "mint-fargate-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
$assumeJson = aws sts assume-role `
    --role-arn $MintRoleArn `
    --role-session-name $sessionName `
    --serial-number $MfaSerial `
    --token-code $tokenCode `
    --duration-seconds 3600 `
    --output json
if ($LASTEXITCODE -ne 0) {
    throw "AssumeRole failed (exit $LASTEXITCODE). Most likely cause: wrong MFA code, expired code, or trust policy mismatch on $MintRoleArn."
}

$assumed = $assumeJson | ConvertFrom-Json
$accessKey  = $assumed.Credentials.AccessKeyId
$secretKey  = $assumed.Credentials.SecretAccessKey
$sessionTok = $assumed.Credentials.SessionToken
$expiration = $assumed.Credentials.Expiration
Write-Host "  AssumeRole OK; credentials valid until $expiration" -ForegroundColor Green

# ----- Step 3: stash creds in env vars FOR THIS PROCESS ONLY -----
$env:AWS_ACCESS_KEY_ID     = $accessKey
$env:AWS_SECRET_ACCESS_KEY = $secretKey
$env:AWS_SESSION_TOKEN     = $sessionTok

$taskArn = $null
$taskId  = $null

try {
    # ----- Step 4: build env-var overrides (if any) and launch the task -----
    $envOverrides = @()
    if ($WorkerHost)    { $envOverrides += @{ name = "WORKER_HOST";     value = $WorkerHost } }
    if ($WorkerDbName)  { $envOverrides += @{ name = "WORKER_DB_NAME";  value = $WorkerDbName } }
    if ($WorkerSsmPath) { $envOverrides += @{ name = "WORKER_SSM_PATH"; value = $WorkerSsmPath } }
    if ($RealRun) {
        $envOverrides += @{ name = "MINT_DRY_RUN"; value = "false" }
    } elseif ($DryRun) {
        # explicit dry-run: confirm task-def default
        $envOverrides += @{ name = "MINT_DRY_RUN"; value = "true" }
    }

    $containerOverride = @{
        name        = "luciel-backend"
        environment = $envOverrides
    }
    $overrides = @{
        containerOverrides = @($containerOverride)
    }
    $overridesJson = $overrides | ConvertTo-Json -Compress -Depth 6

    $networkConfig = @{
        awsvpcConfiguration = @{
            subnets        = $Subnets
            securityGroups = $SecurityGroups
            assignPublicIp = $AssignPublicIp
        }
    }
    $networkConfigJson = $networkConfig | ConvertTo-Json -Compress -Depth 6

    Write-Host "Launching luciel-mint Fargate task..." -ForegroundColor Yellow
    $runJson = aws ecs run-task `
        --cluster $Cluster `
        --task-definition $TaskDefinition `
        --launch-type FARGATE `
        --network-configuration $networkConfigJson `
        --overrides $overridesJson `
        --region $Region `
        --output json
    if ($LASTEXITCODE -ne 0) {
        throw "ecs:RunTask failed (exit $LASTEXITCODE). Confirm the assumed role has ecs:RunTask + iam:PassRole on the task and execution roles."
    }

    $run = $runJson | ConvertFrom-Json
    if ($run.failures -and $run.failures.Count -gt 0) {
        $failJson = $run.failures | ConvertTo-Json -Depth 4
        throw "ecs:RunTask returned failures: $failJson"
    }
    if (-not $run.tasks -or $run.tasks.Count -lt 1) {
        throw "ecs:RunTask returned no tasks and no failures. Inspect raw output: $runJson"
    }

    $taskArn = $run.tasks[0].taskArn
    $taskId  = ($taskArn -split '/')[-1]
    Write-Host "  Task launched: $taskArn" -ForegroundColor Green
    Write-Host "  Task id     : $taskId"

    # CloudWatch log stream is awslogs-stream-prefix/container-name/task-id
    # per the task-def's logConfiguration. Our task-def uses prefix `mint`
    # and container name `luciel-backend`.
    $logGroup  = "/ecs/luciel-backend"
    $logStream = "mint/luciel-backend/$taskId"
    Write-Host "  Log group   : $logGroup"
    Write-Host "  Log stream  : $logStream"
    Write-Host ""

    # ----- Step 5: poll task status + tail logs -----
    Write-Host "Waiting for task to reach STOPPED (poll every $PollIntervalSeconds s, ceiling $MaxWaitMinutes min)..." -ForegroundColor Yellow

    $startUtc      = (Get-Date).ToUniversalTime()
    $deadlineUtc   = $startUtc.AddMinutes($MaxWaitMinutes)
    $nextLogToken  = $null
    $lastStatus    = ""
    $taskStopped   = $false
    $exitCode      = $null
    $stoppedReason = $null

    while ((Get-Date).ToUniversalTime() -lt $deadlineUtc) {
        Start-Sleep -Seconds $PollIntervalSeconds

        # Pull task state
        $descJson = aws ecs describe-tasks `
            --cluster $Cluster `
            --tasks $taskArn `
            --region $Region `
            --output json
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  describe-tasks failed (transient?), continuing..." -ForegroundColor DarkYellow
            continue
        }
        $desc = $descJson | ConvertFrom-Json
        if (-not $desc.tasks -or $desc.tasks.Count -lt 1) {
            Write-Host "  describe-tasks returned no tasks, continuing..." -ForegroundColor DarkYellow
            continue
        }
        $t = $desc.tasks[0]
        $status = $t.lastStatus
        if ($status -ne $lastStatus) {
            Write-Host "  task status: $status" -ForegroundColor Cyan
            $lastStatus = $status
        }

        # Pull log lines (best-effort; stream may not exist until container starts)
        $logArgs = @(
            "logs", "get-log-events",
            "--log-group-name",  $logGroup,
            "--log-stream-name", $logStream,
            "--start-from-head",
            "--region", $Region,
            "--output", "json"
        )
        if ($nextLogToken) {
            $logArgs += @("--next-token", $nextLogToken)
        }
        $logsJson = aws @logArgs 2>$null
        if ($LASTEXITCODE -eq 0 -and $logsJson) {
            try {
                $logs = $logsJson | ConvertFrom-Json
                if ($logs.events) {
                    foreach ($ev in $logs.events) {
                        Write-Host "  [task] $($ev.message)"
                    }
                }
                if ($logs.nextForwardToken -and $logs.nextForwardToken -ne $nextLogToken) {
                    $nextLogToken = $logs.nextForwardToken
                }
            } catch {
                # swallow JSON parse errors during transient log retrieval
            }
        }

        if ($status -eq "STOPPED") {
            $taskStopped   = $true
            $stoppedReason = $t.stoppedReason
            if ($t.containers -and $t.containers.Count -ge 1) {
                $exitCode = $t.containers[0].exitCode
            }
            break
        }
    }

    if (-not $taskStopped) {
        throw "Task did not reach STOPPED within $MaxWaitMinutes min. Task is still running on the cluster; check CloudWatch and ECS console. Task ARN: $taskArn"
    }

    # ----- Step 6: final log drain (catch any tail-end lines) -----
    Write-Host ""
    Write-Host "Task STOPPED. Draining final log lines..." -ForegroundColor Yellow
    $logArgs = @(
        "logs", "get-log-events",
        "--log-group-name",  $logGroup,
        "--log-stream-name", $logStream,
        "--start-from-head",
        "--region", $Region,
        "--output", "json"
    )
    if ($nextLogToken) {
        $logArgs += @("--next-token", $nextLogToken)
    }
    $logsJson = aws @logArgs 2>$null
    if ($LASTEXITCODE -eq 0 -and $logsJson) {
        try {
            $logs = $logsJson | ConvertFrom-Json
            if ($logs.events) {
                foreach ($ev in $logs.events) {
                    Write-Host "  [task] $($ev.message)"
                }
            }
        } catch {}
    }

    # ----- Step 7: report exit code -----
    Write-Host ""
    Write-Host "Stopped reason : $stoppedReason"
    Write-Host "Container exit : $exitCode"

    if ($null -eq $exitCode) {
        throw "Task stopped without a container exit code. This usually means the container failed to start (image pull, IAM, networking). Check logs above and `aws ecs describe-tasks --tasks $taskArn`."
    }
    if ($exitCode -ne 0) {
        throw "Mint task exited non-zero (code $exitCode). See log lines above. NOTE: the mint script's atomicity defenses mean a non-zero exit BEFORE the SSM put is non-destructive (no role change, no SSM write). A non-zero exit AFTER the ALTER ROLE but BEFORE the SSM put is the dangerous case — verify by attempting a worker auth as luciel_worker against the OLD password (should fail) and the NEW password (should also fail since we have no copy). If both fail, run scripts/rotate_worker_role.sql to reset, then re-run the mint."
    }

    Write-Host ""
    if ($RealRun) {
        Write-Host "REAL MINT COMPLETE." -ForegroundColor Green
        Write-Host "Verify: aws ssm get-parameter --name /luciel/production/worker_database_url --region $Region --query 'Parameter.Version'"
        Write-Host "Expected: Version > 0 (parameter exists). Do NOT --with-decryption."
    } else {
        Write-Host "DRY-RUN COMPLETE. No production state mutated." -ForegroundColor Green
    }
}
finally {
    # ----- Step 8: clear assumed credentials, ALWAYS -----
    Remove-Item Env:\AWS_ACCESS_KEY_ID     -ErrorAction SilentlyContinue
    Remove-Item Env:\AWS_SECRET_ACCESS_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:\AWS_SESSION_TOKEN     -ErrorAction SilentlyContinue
    Write-Host "Assumed credentials cleared from session." -ForegroundColor DarkGray
}
