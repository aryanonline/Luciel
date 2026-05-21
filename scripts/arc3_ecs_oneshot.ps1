<#
.SYNOPSIS
  Arc 3 Work-Unit A.2 \u2014 Pattern O ceremony orchestrator.

  RDS rejects laptop ingress (Pattern N is forbidden), so the Arc 3
  remediation SQL runs from inside the VPC via an ECS one-shot task on
  the luciel-prod-ops task-def. This script handles three stages:

    -Stage build  : docker build + ECR push + register td-prod-ops:rev4
                    (one-time \u2014 the new image bakes the three Arc 3
                    scripts into COPY scripts/scripts/, so subsequent
                    runs reuse the same image).
    -Stage revoke : run-task that invokes
                    arc3_revoke_leaked_invites_run.py. Default --dry-run;
                    pass -Live to execute the UPDATE.
    -Stage audit  : run-task that invokes
                    arc3_audit_leaked_invites_record.py with the captured
                    PSV piped in via stdin. Requires -PsvFile.

  Every stage tails CloudWatch and reports exitCode.

  The JTI list (arc3-out/leaked-welcome-jtis.txt) is delivered to the
  container via the ARC3_JTI_INLINE env override, NOT baked into the
  image. This keeps the image generic and re-usable across windows.

.PARAMETER Stage
  'build' | 'revoke' | 'audit'.

.PARAMETER Live
  Switch. Only meaningful for -Stage revoke. Default OFF (dry-run).

.PARAMETER JtiFile
  Path to leaked-welcome-jtis.txt on the laptop. Default
  arc3-out\\leaked-welcome-jtis.txt.

.PARAMETER PsvFile
  Path to flipped-invites.psv on the laptop (captured between the
  PSV-BEGIN / PSV-END sentinels printed by the revoke task's stdout).
  Required for -Stage audit.

.EXAMPLE
  # one-time \u2014 build & register
  .\\scripts\\arc3_ecs_oneshot.ps1 -Stage build

.EXAMPLE
  # dry-run revoke
  .\\scripts\\arc3_ecs_oneshot.ps1 -Stage revoke

.EXAMPLE
  # live revoke (capture stdout for PSV between sentinels)
  .\\scripts\\arc3_ecs_oneshot.ps1 -Stage revoke -Live

.EXAMPLE
  # audit-record
  .\\scripts\\arc3_ecs_oneshot.ps1 -Stage audit -PsvFile arc3-out\\flipped-invites.psv
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('build', 'revoke', 'audit')]
    [string]$Stage,
    [switch]$Live,
    [string]$JtiFile = 'arc3-out\leaked-welcome-jtis.txt',
    [string]$PsvFile = '',
    [string]$Region = 'ca-central-1',
    [string]$Cluster = 'luciel-cluster',
    [string]$EcrRepo = '729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend',
    [string]$Tag = 'arc3-prod-ops'
)

$ErrorActionPreference = 'Stop'
$Image = "${EcrRepo}:${Tag}"
$RepoName = ($EcrRepo -split '/')[-1]
$LogGroup = '/ecs/luciel-backend'
$StreamPrefix = 'arc3-prod-ops'

function Invoke-Build {
    Write-Host "==> docker build ($Tag)" -ForegroundColor Cyan
    docker build -t $Image .
    if ($LASTEXITCODE -ne 0) { throw "docker build failed" }

    Write-Host "==> ECR login + push" -ForegroundColor Cyan
    $loginPwd = aws ecr get-login-password --region $Region
    if ($LASTEXITCODE -ne 0) { throw "ecr get-login-password failed" }
    $loginPwd | docker login --username AWS --password-stdin $EcrRepo
    if ($LASTEXITCODE -ne 0) { throw "docker login failed" }
    docker push $Image
    if ($LASTEXITCODE -ne 0) { throw "docker push failed" }

    Write-Host "==> Resolving digest" -ForegroundColor Cyan
    $Digest = aws ecr describe-images `
        --repository-name $RepoName `
        --image-ids "imageTag=$Tag" `
        --region $Region `
        --query 'imageDetails[0].imageDigest' --output text
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($Digest) -or $Digest -eq 'None') {
        throw "could not resolve digest for tag $Tag"
    }
    $ImageRef = "${EcrRepo}@${Digest}"
    Write-Host "    $ImageRef"

    Write-Host "==> Rendering td-prod-ops-rev4.json" -ForegroundColor Cyan
    if (-not (Test-Path 'td-prod-ops-rev3.json')) {
        throw "td-prod-ops-rev3.json not found in CWD; run from repo root"
    }
    # Surgical text edits on the raw JSON instead of round-tripping
    # through ConvertFrom-Json | ConvertTo-Json. PowerShell 5.1's
    # ConvertTo-Json collapses single-element arrays into scalars
    # (requiresCompatibilities, containerDefinitions, environment,
    # secrets all have len==1) which AWS rejects as "Invalid JSON".
    # Also avoids the Out-File -Encoding utf8 BOM gotcha entirely.
    $tdRaw = Get-Content 'td-prod-ops-rev3.json' -Raw
    # (1) Swap the pinned image digest. The rev3 file has a literal
    #     image string; replace the FIRST occurrence to be safe.
    $oldImageRegex = '"image"\s*:\s*"[^"]+"'
    $newImageLine = '"image": "' + $ImageRef + '"'
    if ($tdRaw -notmatch $oldImageRegex) {
        throw "Could not locate 'image' field in td-prod-ops-rev3.json"
    }
    $tdNew = [regex]::Replace($tdRaw, $oldImageRegex, $newImageLine, 1)
    # (2) Swap the awslogs-stream-prefix.
    $oldStreamRegex = '"awslogs-stream-prefix"\s*:\s*"[^"]+"'
    $newStreamLine = '"awslogs-stream-prefix": "' + $StreamPrefix + '"'
    if ($tdNew -notmatch $oldStreamRegex) {
        throw "Could not locate 'awslogs-stream-prefix' field in td-prod-ops-rev3.json"
    }
    $tdNew = [regex]::Replace($tdNew, $oldStreamRegex, $newStreamLine, 1)
    # Write BOM-less UTF-8 via the .NET API.
    [System.IO.File]::WriteAllText(
        (Join-Path (Get-Location) 'td-prod-ops-rev4.json'),
        $tdNew,
        [System.Text.UTF8Encoding]::new($false)
    )
    Write-Host "    Wrote td-prod-ops-rev4.json (BOM-less UTF-8)"

    Write-Host "==> register-task-definition" -ForegroundColor Cyan
    $tdArn = aws ecs register-task-definition `
        --cli-input-json 'file://td-prod-ops-rev4.json' `
        --region $Region `
        --query 'taskDefinition.taskDefinitionArn' --output text
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($tdArn)) {
        throw "register-task-definition failed"
    }
    Write-Host "    Registered: $tdArn" -ForegroundColor Green
}

function Get-LatestTaskDef {
    # NOTE: --max-items is a CLIENT-SIDE pagination param. With it set,
    # the CLI returns the requested items AND a pagination NextToken,
    # which makes --output text emit two lines and breaks --query slicing.
    # Drop it entirely and let --sort DESC + [0] pick the newest ACTIVE
    # task-def. The list is bounded (< 1000 entries) so no perf concern.
    $tdArn = aws ecs list-task-definitions `
        --family-prefix luciel-prod-ops `
        --status ACTIVE `
        --sort DESC `
        --region $Region `
        --query 'taskDefinitionArns[0]' --output text
    if ([string]::IsNullOrWhiteSpace($tdArn) -or $tdArn -eq 'None') {
        throw "no luciel-prod-ops task-def found; run -Stage build first"
    }
    return $tdArn
}

function Get-NetworkConfig {
    $netcfg = aws ecs describe-services `
        --cluster $Cluster `
        --services luciel-backend-service `
        --region $Region `
        --query "services[0].networkConfiguration.awsvpcConfiguration" `
        --output json | ConvertFrom-Json
    return @{
        awsvpcConfiguration = @{
            subnets = $netcfg.subnets
            securityGroups = $netcfg.securityGroups
            assignPublicIp = 'ENABLED'
        }
    }
}

function Invoke-OneShot {
    param(
        [Parameter(Mandatory)][string[]]$ContainerCmd,
        [Parameter(Mandatory)][array]$EnvOverrides
    )
    $tdArn = Get-LatestTaskDef
    $netCfg = Get-NetworkConfig
    Write-Host "    TaskDef   : $tdArn"
    Write-Host "    Subnets   : $($netCfg.awsvpcConfiguration.subnets -join ',')"
    Write-Host "    SGs       : $($netCfg.awsvpcConfiguration.securityGroups -join ',')"

    # Build the JSON for --overrides BY HAND. PowerShell 5.1's
    # ConvertTo-Json has well-documented bugs with single-element array
    # preservation in nested object graphs, and the workarounds
    # (Write-Output -NoEnumerate, comma operator, ArrayList) are
    # fragile. Since we control every input and the only variable
    # content is the JTI list (all chars are JSON-safe: hyphens, UUIDs,
    # newlines) and the small script bootstrap, we just emit the JSON
    # as a string literal. The .NET JsonConvert via System.Web is the
    # cleanest escaper for any user-provided values.
    Add-Type -AssemblyName System.Web.Extensions
    $jsonSer = New-Object System.Web.Script.Serialization.JavaScriptSerializer
    $jsonSer.MaxJsonLength = [int]::MaxValue

    # Build container override as a Hashtable; the JS serializer
    # respects [object[]] / ArrayList for arrays without collapsing.
    $envList = New-Object System.Collections.ArrayList
    foreach ($e in $EnvOverrides) {
        [void]$envList.Add(@{ name = $e.name; value = $e.value })
    }
    $cmdList = New-Object System.Collections.ArrayList
    foreach ($c in $ContainerCmd) { [void]$cmdList.Add($c) }

    $containerOv = @{
        name        = 'luciel-prod-ops'
        command     = $cmdList
        environment = $envList
    }
    $coList = New-Object System.Collections.ArrayList
    [void]$coList.Add($containerOv)
    $overridesObj = @{ containerOverrides = $coList }

    $overridesJson = $jsonSer.Serialize($overridesObj)
    # Sanity-check: containerOverrides MUST be an array.
    if ($overridesJson -notmatch '"containerOverrides"\s*:\s*\[') {
        throw "overrides JSON did not preserve containerOverrides array; got: $overridesJson"
    }

    # Network config: same shape concerns, same approach.
    $subnetList = New-Object System.Collections.ArrayList
    foreach ($s in $netCfg.awsvpcConfiguration.subnets) { [void]$subnetList.Add($s) }
    $sgList = New-Object System.Collections.ArrayList
    foreach ($g in $netCfg.awsvpcConfiguration.securityGroups) { [void]$sgList.Add($g) }
    $networkObj = @{
        awsvpcConfiguration = @{
            subnets         = $subnetList
            securityGroups  = $sgList
            assignPublicIp  = 'ENABLED'
        }
    }
    $networkJson = $jsonSer.Serialize($networkObj)

    # Windows has an ~32k command-line length limit. The JTI list +
    # JSON escaping inside --overrides easily blows past that on a
    # single argv. AWS CLI accepts file:// references for any string
    # argument, so we stage both JSON payloads to disk and pass them
    # by reference. Files are written BOM-less UTF-8 via .NET.
    $tmpDir = Join-Path $env:TEMP "arc3-runtask-$([guid]::NewGuid().ToString('N'))"
    New-Item -ItemType Directory -Path $tmpDir | Out-Null
    $overridesPath = Join-Path $tmpDir 'overrides.json'
    $networkPath   = Join-Path $tmpDir 'network.json'
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($overridesPath, $overridesJson, $utf8NoBom)
    [System.IO.File]::WriteAllText($networkPath,   $networkJson,   $utf8NoBom)
    Write-Host "    overrides : $overridesPath ($(($overridesJson.Length)) chars)"
    Write-Host "    network   : $networkPath"

    Write-Host "==> aws ecs run-task" -ForegroundColor Cyan
    $runOut = aws ecs run-task `
        --cluster $Cluster `
        --task-definition $tdArn `
        --launch-type FARGATE `
        --network-configuration "file://$networkPath" `
        --overrides "file://$overridesPath" `
        --region $Region `
        --output json | ConvertFrom-Json

    if (-not $runOut.tasks -or $runOut.tasks.Count -eq 0) {
        Write-Host "RUN-TASK FAILURES:" -ForegroundColor Red
        $runOut.failures | ConvertTo-Json -Depth 10
        throw "run-task failed"
    }

    $taskArn = $runOut.tasks[0].taskArn
    $taskId  = ($taskArn -split '/')[-1]
    $streamName = "$StreamPrefix/luciel-prod-ops/$taskId"
    Write-Host "    Task ARN  : $taskArn"
    Write-Host "    Stream    : $streamName"

    Write-Host "==> Tailing CloudWatch until STOPPED" -ForegroundColor Cyan
    $nextToken = $null
    $desc = $null
    while ($true) {
        Start-Sleep -Seconds 5
        $desc = aws ecs describe-tasks `
            --cluster $Cluster `
            --tasks $taskArn `
            --region $Region `
            --query 'tasks[0]' --output json | ConvertFrom-Json
        $logArgs = @(
            'logs', 'get-log-events',
            '--log-group-name', $LogGroup,
            '--log-stream-name', $streamName,
            '--start-from-head',
            '--region', $Region,
            '--output', 'json'
        )
        if ($nextToken) { $logArgs += @('--next-token', $nextToken) }
        $logRaw = & aws @logArgs 2>$null
        if ($LASTEXITCODE -eq 0 -and $logRaw) {
            $logOut = $logRaw | ConvertFrom-Json
            if ($logOut.events -and $logOut.events.Count -gt 0) {
                foreach ($e in $logOut.events) { Write-Host $e.message }
                $nextToken = $logOut.nextForwardToken
            }
        }
        if ($desc.lastStatus -eq 'STOPPED') { break }
        Write-Host "    [...lastStatus=$($desc.lastStatus)]" -ForegroundColor DarkGray
    }

    # Trailing drain.
    Start-Sleep -Seconds 3
    $logArgs = @(
        'logs', 'get-log-events',
        '--log-group-name', $LogGroup,
        '--log-stream-name', $streamName,
        '--start-from-head',
        '--region', $Region,
        '--output', 'json'
    )
    if ($nextToken) { $logArgs += @('--next-token', $nextToken) }
    $logRaw = & aws @logArgs 2>$null
    if ($LASTEXITCODE -eq 0 -and $logRaw) {
        $logOut = $logRaw | ConvertFrom-Json
        if ($logOut.events) {
            foreach ($e in $logOut.events) { Write-Host $e.message }
        }
    }

    $exitCode = $desc.containers[0].exitCode
    Write-Host ""
    Write-Host "==> Task STOPPED" -ForegroundColor Cyan
    Write-Host "    exitCode      : $exitCode"
    Write-Host "    stoppedReason : $($desc.stoppedReason)"
    if ($exitCode -ne 0) { exit 1 }
}

# ----------------------------------------------------------------------
# Dispatch
# ----------------------------------------------------------------------
switch ($Stage) {
    'build' {
        Invoke-Build
        break
    }
    'revoke' {
        if (-not (Test-Path $JtiFile)) { throw "JtiFile not found: $JtiFile" }
        $jtiContent = Get-Content $JtiFile -Raw
        $modeFlag = if ($Live) { '' } else { '--dry-run' }
        $bootstrap = @"
set -e
mkdir -p /tmp/arc3
printf '%s' "`$ARC3_JTI_INLINE" > /tmp/arc3/jtis.txt
echo "--- jti-file lines: `$(wc -l < /tmp/arc3/jtis.txt) ---"
exec python scripts/arc3_revoke_leaked_invites_run.py --jti-file /tmp/arc3/jtis.txt $modeFlag --out /tmp/arc3/flipped.psv
"@
        $cmd = @('sh', '-c', $bootstrap)
        $envOv = @(@{ name = 'ARC3_JTI_INLINE'; value = $jtiContent })
        Write-Host "==> Stage=revoke  Mode=$(if($Live){'LIVE'}else{'DRY-RUN'})" -ForegroundColor Cyan
        Invoke-OneShot -ContainerCmd $cmd -EnvOverrides $envOv
        break
    }
    'audit' {
        if (-not $PsvFile -or -not (Test-Path $PsvFile)) {
            throw "Stage=audit requires -PsvFile pointing at the captured PSV"
        }
        $psvContent = Get-Content $PsvFile -Raw
        $bootstrap = @'
set -e
echo "$ARC3_PSV_INLINE" | python scripts/arc3_audit_leaked_invites_record.py -
'@
        $cmd = @('sh', '-c', $bootstrap)
        $envOv = @(@{ name = 'ARC3_PSV_INLINE'; value = $psvContent })
        Write-Host "==> Stage=audit" -ForegroundColor Cyan
        Invoke-OneShot -ContainerCmd $cmd -EnvOverrides $envOv
        break
    }
}
