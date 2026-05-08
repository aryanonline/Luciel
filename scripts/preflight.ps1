<#
.SYNOPSIS
    Step 28 P3-N pre-flight gate. Run before any prod-touching ceremony
    or any verification run that aims to exercise the async memory path.

.DESCRIPTION
    The historical "5-block pre-flight" lived as inline PowerShell
    snippets in CANONICAL_RECAP.md Section 13 Step 3. It validated AWS
    identity, git state, docker liveness, dev admin key presence, and
    local verification (`python -m app.verification`). It did NOT
    validate that the local stack was production-shaped: in particular,
    it passed cleanly when celery was not running locally and when
    `settings.memory_extraction_async` was False (the local default).
    The Pillar 13 A3 incident on 2026-05-04 demonstrated that the
    silent sync fallback in `ChatService` masked a real customer-facing
    bug for an entire prod-parity gap -- the assistant said
    "I'll remember that" while zero MemoryItem rows landed.

    This script runs all five historical gates plus two new ones:

      Gate 6  Celery responder check
              celery -A app.worker.celery_app inspect ping --timeout 5
              Fails (exit 1) if the celery binary is unreachable, or
              if the broker is unreachable, or if zero responders
              answer the ping. Skipped only when `-AllowDevSync` is
              passed, in which case a clear "DEGRADED" warning is
              printed.

      Gate 7  Async-flag posture
              Loads `app.core.config.settings` and asserts
              `memory_extraction_async == True` (matching the prod
              default `MEMORY_EXTRACTION_ASYNC=true` set in
              backend-td-rev*.json). When False, fails (exit 1)
              unless `-AllowDevSync` is passed, in which case a
              clear "DEGRADED" warning is printed.

    The two -AllowDevSync paths are present so an operator can still
    do non-async-related work (e.g. UI tweaks, docs, schema review)
    without celery+redis running. They are NOT a way to bypass the
    gate when a verification run is intended to cover the async path.
    The runbook at `docs/runbooks/operator-patterns.md` codifies which
    workflows require Gate 6 + Gate 7 strict mode.

.PARAMETER ExpectedAccount
    AWS account ID that `aws sts get-caller-identity` must return.
    Default 729005488042 (Luciel production account).

.PARAMETER AllowDevSync
    Permit Gate 6 (celery responder) and Gate 7 (async flag) to
    print a DEGRADED warning instead of failing. Use only when the
    operator's intended workflow does not exercise the async memory
    path. Verification runs aiming at Pillar 11 / Pillar 13 must NOT
    pass this flag.

.PARAMETER SkipVerification
    Skip Gate 5 (`python -m app.verification`). Verification is the
    expensive gate (~90s); skip only when explicitly resuming a known
    in-flight ceremony where verification ran in a prior session and
    the working tree has not changed since.

.PARAMETER ExpectedSha
    Optional. If provided, Gate 2 also asserts `git rev-parse HEAD`
    equals this SHA. Used by the runbook's "operator pulled before
    write-side AWS op" guard (D-operator-pull-skipped-before-write-
    side-aws-ops-2026-05-05).

.EXAMPLE
    .\scripts\preflight.ps1
    Strict mode. All seven gates must pass.

.EXAMPLE
    .\scripts\preflight.ps1 -AllowDevSync
    Permits dev-sync local development without celery. Prints
    DEGRADED warnings on Gates 6 and 7. NOT acceptable before a
    verification run targeting the async path.

.EXAMPLE
    .\scripts\preflight.ps1 -ExpectedSha 1d880be
    Strict mode plus SHA pin. Used after `git pull origin <branch>`
    when a specific commit is expected.

.NOTES
    Authored as Step 28 C9 (P3-N) on 2026-05-06.
    Sister gate to operator-patterns.md "Pre-flight gate" section.
    Exits with code 1 on any failure. Pure ASCII.
#>

[CmdletBinding()]
param(
    [string]$ExpectedAccount = "729005488042",
    [switch]$AllowDevSync,
    [switch]$SkipVerification,
    [string]$ExpectedSha = ""
)

$ErrorActionPreference = "Stop"
$global:PreflightFailed = $false

function Write-GateHeader {
    param([string]$Name)
    Write-Host ""
    Write-Host ("=" * 72)
    Write-Host "  $Name"
    Write-Host ("=" * 72)
}

function Write-GatePass {
    param([string]$Detail)
    Write-Host "  [PASS] $Detail" -ForegroundColor Green
}

function Write-GateFail {
    param([string]$Detail)
    Write-Host "  [FAIL] $Detail" -ForegroundColor Red
    $global:PreflightFailed = $true
}

function Write-GateWarn {
    param([string]$Detail)
    Write-Host "  [WARN] $Detail" -ForegroundColor Yellow
}

# ---------- Gate 1: AWS identity ----------
Write-GateHeader "Gate 1: AWS identity (expect $ExpectedAccount)"
try {
    $account = (aws sts get-caller-identity --query Account --output text 2>&1).Trim()
    if ($LASTEXITCODE -ne 0) {
        Write-GateFail "aws sts get-caller-identity failed: $account"
    } elseif ($account -ne $ExpectedAccount) {
        Write-GateFail "Account mismatch: got '$account', expected '$ExpectedAccount'"
    } else {
        Write-GatePass "Account $account"
    }
} catch {
    Write-GateFail "aws CLI invocation threw: $_"
}

# ---------- Gate 2: Git state ----------
Write-GateHeader "Gate 2: Git state (clean tree, optional SHA pin)"
$dirty = (git status --short 2>&1)
if ($LASTEXITCODE -ne 0) {
    Write-GateFail "git status failed: $dirty"
} elseif (-not [string]::IsNullOrWhiteSpace($dirty)) {
    Write-GateFail "Working tree not clean:`n$dirty"
} else {
    Write-GatePass "Working tree clean"
}
$head = (git log -1 --oneline 2>&1)
if ($LASTEXITCODE -eq 0) {
    Write-Host "         HEAD: $head"
}
$stashes = (git stash list 2>&1)
if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($stashes)) {
    Write-GateWarn "Stashes present (informational, not failing):`n$stashes"
}
if (-not [string]::IsNullOrEmpty($ExpectedSha)) {
    $headSha = (git rev-parse HEAD 2>&1).Trim()
    if ($LASTEXITCODE -ne 0) {
        Write-GateFail "git rev-parse HEAD failed: $headSha"
    } elseif (-not $headSha.StartsWith($ExpectedSha)) {
        Write-GateFail "SHA mismatch: HEAD is '$headSha', expected prefix '$ExpectedSha' (run 'git pull origin <branch>')"
    } else {
        Write-GatePass "SHA pin matched ($ExpectedSha)"
    }
}

# ---------- Gate 3: Docker ----------
Write-GateHeader "Gate 3: Docker daemon"
try {
    $dockerInfo = (docker info --format "{{.ServerVersion}} {{.OperatingSystem}}" 2>&1)
    if ($LASTEXITCODE -ne 0) {
        Write-GateFail "docker info failed: $dockerInfo"
    } else {
        Write-GatePass "Docker $dockerInfo"
    }
} catch {
    Write-GateFail "docker CLI invocation threw: $_"
}

# ---------- Gate 4: Dev admin key ----------
Write-GateHeader "Gate 4: Dev admin key in environment"
$key = $env:LUCIEL_PLATFORM_ADMIN_KEY
if ([string]::IsNullOrEmpty($key)) {
    Write-GateFail "LUCIEL_PLATFORM_ADMIN_KEY is not set"
} elseif (-not $key.StartsWith("luc_sk_")) {
    Write-GateFail "LUCIEL_PLATFORM_ADMIN_KEY does not start with 'luc_sk_'"
} elseif ($key.Length -ne 50) {
    Write-GateFail "LUCIEL_PLATFORM_ADMIN_KEY length is $($key.Length), expected 50"
} else {
    Write-GatePass "LUCIEL_PLATFORM_ADMIN_KEY present, prefix and length OK (value never echoed)"
}

# ---------- Gate 5: Verification ----------
if ($SkipVerification) {
    Write-GateHeader "Gate 5: Verification (SKIPPED via -SkipVerification)"
    Write-GateWarn "python -m app.verification not run this invocation"
} else {
    Write-GateHeader "Gate 5: Local verification (python -m app.verification)"
    $verifyOut = (python -m app.verification 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) {
        Write-GateFail "verification exited $LASTEXITCODE"
        Write-Host $verifyOut
    } elseif ($verifyOut -notmatch "RESULT:\s+\d+/\d+\s+pillars\s+green") {
        Write-GateFail "verification output did not contain 'RESULT: N/N pillars green' line"
        Write-Host $verifyOut
    } else {
        $resultLine = ($verifyOut -split "`n" | Where-Object { $_ -match "RESULT:" } | Select-Object -Last 1).Trim()
        Write-GatePass $resultLine
    }
}

# ---------- Gate 6: Celery responder check (P3-N) ----------
Write-GateHeader "Gate 6: Celery responder check (P3-N)"
$celeryOut = ""
$celeryExit = 0
try {
    $celeryOut = (celery -A app.worker.celery_app inspect ping --timeout 5 2>&1 | Out-String)
    $celeryExit = $LASTEXITCODE
} catch {
    $celeryOut = "$_"
    $celeryExit = -1
}
$responderCount = ([regex]::Matches($celeryOut, "->\s*[^:]+:\s*OK")).Count
if ($celeryExit -eq 0 -and $responderCount -gt 0) {
    Write-GatePass "celery inspect ping: $responderCount responder(s)"
} else {
    $msg = "celery inspect ping returned exit=$celeryExit, responders=$responderCount"
    if ($AllowDevSync) {
        Write-GateWarn "DEGRADED -- $msg"
        Write-GateWarn "Async memory path will NOT be exercised. -AllowDevSync acknowledged."
    } else {
        Write-GateFail $msg
        Write-Host "         Start the celery worker locally:" -ForegroundColor Red
        Write-Host "           celery -A app.worker.celery_app worker --loglevel=info" -ForegroundColor Red
        Write-Host "         Or re-invoke with -AllowDevSync if the intended workflow" -ForegroundColor Red
        Write-Host "         does not exercise the async memory path." -ForegroundColor Red
        Write-Host "         Raw output:" -ForegroundColor DarkGray
        Write-Host $celeryOut -ForegroundColor DarkGray
    }
}

# ---------- Gate 7: Async-flag posture (P3-N) ----------
Write-GateHeader "Gate 7: Async-flag posture (settings.memory_extraction_async)"
$flagOut = (python -c "from app.core.config import settings; print('MEMORY_EXTRACTION_ASYNC=' + str(settings.memory_extraction_async))" 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    Write-GateFail "could not load settings: $flagOut"
} elseif ($flagOut -eq "MEMORY_EXTRACTION_ASYNC=True") {
    Write-GatePass "settings.memory_extraction_async = True (prod-shaped)"
} elseif ($flagOut -eq "MEMORY_EXTRACTION_ASYNC=False") {
    if ($AllowDevSync) {
        Write-GateWarn "DEGRADED -- settings.memory_extraction_async = False (dev-sync mode)"
        Write-GateWarn "Async memory path will NOT be exercised. -AllowDevSync acknowledged."
    } else {
        Write-GateFail "settings.memory_extraction_async = False (sync fallback active)"
        Write-Host "         Set MEMORY_EXTRACTION_ASYNC=true in your local env, e.g.:" -ForegroundColor Red
        Write-Host "           `$env:MEMORY_EXTRACTION_ASYNC = 'true'" -ForegroundColor Red
        Write-Host "         Or re-invoke with -AllowDevSync if the intended workflow" -ForegroundColor Red
        Write-Host "         does not exercise the async memory path." -ForegroundColor Red
    }
} else {
    Write-GateFail "unexpected output: $flagOut"
}

# ---------- summary ----------
Write-Host ""
Write-Host ("=" * 72)
if ($global:PreflightFailed) {
    Write-Host "  PRE-FLIGHT FAILED -- diagnosis is the only acceptable next action." -ForegroundColor Red
    Write-Host ("=" * 72)
    exit 1
} else {
    if ($AllowDevSync) {
        Write-Host "  PRE-FLIGHT PASSED (dev-sync mode -- async path NOT covered)" -ForegroundColor Yellow
    } else {
        Write-Host "  PRE-FLIGHT PASSED -- prod-shaped (all 7 gates green)" -ForegroundColor Green
    }
    Write-Host ("=" * 72)
    exit 0
}
