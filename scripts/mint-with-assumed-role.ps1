<#
.SYNOPSIS
  Option 3 mint ceremony: assume the luciel-mint-operator-role with
  MFA, then invoke the hardened mint script with the resulting
  short-lived credentials.

.DESCRIPTION
  This is the operator helper that closes the Phase 2 Commit 4 boundary
  per P3-K. It:

    1. Prompts for the operator's current MFA TOTP code.
    2. Calls `aws sts assume-role` against `luciel-mint-operator-role`
       with the operator's MFA serial number and the supplied TOTP.
       AWS validates the code; if invalid, the assume-role fails and
       the script exits.
    3. Captures the short-lived credentials (Access Key, Secret,
       Session Token) into the current PowerShell session ONLY.
    4. Reads the admin DSN from SSM as the assumed role (the only
       principal that has read access on /luciel/database-url after
       P3-K), and pipes it directly to the mint script via
       --admin-db-url-stdin (NEVER as a CLI argument; never echoed).
    5. On exit (success or failure), clears the assumed credentials
       from the session.

  The credentials live for at most 1 hour (3600 s, the role's
  MaxSessionDuration). They cannot be reused after the script returns.
  The admin DSN never lands on disk, in shell history, or in any AWS
  log group.

.PARAMETER MfaSerial
  The MFA device ARN for luciel-admin. Default value is the value
  recorded at P3-J resolution (2026-05-03):
  arn:aws:iam::729005488042:mfa/Luciel-MFA

.PARAMETER MintRoleArn
  The ARN of the luciel-mint-operator-role created by P3-K. Default
  value is the canonical ARN.

.PARAMETER WorkerHost
  The RDS endpoint hostname for the worker DB connection string.

.PARAMETER WorkerDbName
  The Postgres database name (default: luciel).

.PARAMETER WorkerSsmPath
  The SSM path where the new worker DSN will be written (default:
  /luciel/production/worker_database_url).

.PARAMETER Region
  AWS region (default: ca-central-1).

.PARAMETER DryRun
  Pass through to the mint script. Performs no Postgres or SSM writes;
  used for ceremony walkthroughs.

.PARAMETER EmitDsnOnly
  Alternate mode used by P3-H (admin password rotation) and any future
  runbook that needs to read the admin DSN through the same MFA-gated
  ceremony but does NOT mint a worker password. When set:
    - WorkerHost / WorkerDbName / WorkerSsmPath are ignored.
    - The mint script is NOT invoked.
    - The admin DSN is converted to a SecureString and written to the
      pipeline as the script's only output (caller assigns it).
    - The plain DSN is wiped from memory in the same finally block as
      the assumed credentials.
  Caller usage:
      $assumedDsn = .\scripts\mint-with-assumed-role.ps1 -EmitDsnOnly
  $assumedDsn is a SecureString; convert via
      [System.Net.NetworkCredential]::new('',$assumedDsn).Password
  ONLY in the same shell, immediately, and clear afterwards.

.EXAMPLE
  # Worker DB role swap mint (P3-K / Phase 2 Commit 4)
  .\scripts\mint-with-assumed-role.ps1 `
      -WorkerHost luciel-db.c3oyiegi01hr.ca-central-1.rds.amazonaws.com `
      -DryRun

.EXAMPLE
  # Admin DSN read for P3-H rotation
  $assumedDsn = .\scripts\mint-with-assumed-role.ps1 -EmitDsnOnly

.NOTES
  Author: Aryan Singh
  Created: 2026-05-03 (P3-K)
  Cross-references:
    - docs/recaps/2026-05-03-mint-incident.md  (incident drove this design)
    - docs/PHASE_3_COMPLIANCE_BACKLOG.md  P3-K  (architecture)
    - docs/CANONICAL_RECAP.md  Section 12 anchor 7  (locked decision)
    - scripts/mint_worker_db_password_ssm.py  (the hardened mint script)
#>

[CmdletBinding(DefaultParameterSetName = 'Mint')]
param(
    [string]$MfaSerial    = "arn:aws:iam::729005488042:mfa/Luciel-MFA",
    [string]$MintRoleArn  = "arn:aws:iam::729005488042:role/luciel-mint-operator-role",

    [Parameter(ParameterSetName = 'Mint', Mandatory = $true)]
    [string]$WorkerHost,

    [Parameter(ParameterSetName = 'Mint')]
    [string]$WorkerDbName  = "luciel",

    [Parameter(ParameterSetName = 'Mint')]
    [string]$WorkerSsmPath = "/luciel/production/worker_database_url",

    [string]$AdminDsnSsmPath = "/luciel/database-url",
    [string]$Region        = "ca-central-1",

    [Parameter(ParameterSetName = 'Mint')]
    [switch]$DryRun,

    [Parameter(ParameterSetName = 'EmitDsn', Mandatory = $true)]
    [switch]$EmitDsnOnly
)

$ErrorActionPreference = "Stop"

# ----- Step 1: prompt for TOTP -----
Write-Host ""
if ($EmitDsnOnly) {
    Write-Host "Option 3 admin-DSN read (EmitDsnOnly)" -ForegroundColor Cyan
    Write-Host "  MFA serial : $MfaSerial"
    Write-Host "  Mint role  : $MintRoleArn"
    Write-Host "  Region     : $Region"
    Write-Host "  Admin SSM  : $AdminDsnSsmPath"
    Write-Host "  Mode       : EmitDsnOnly (no mint script invocation)"
} else {
    Write-Host "Option 3 mint ceremony" -ForegroundColor Cyan
    Write-Host "  MFA serial : $MfaSerial"
    Write-Host "  Mint role  : $MintRoleArn"
    Write-Host "  Region     : $Region"
    Write-Host "  Worker host: $WorkerHost"
    Write-Host "  Worker SSM : $WorkerSsmPath"
    Write-Host "  Admin SSM  : $AdminDsnSsmPath"
    Write-Host "  Dry run    : $DryRun"
}
Write-Host ""

$tokenCode = Read-Host -Prompt "Enter current MFA 6-digit code"
if ([string]::IsNullOrWhiteSpace($tokenCode)) {
    throw "MFA code is empty; aborting."
}

# ----- Step 2: assume the role with MFA -----
Write-Host "Calling sts:AssumeRole with MFA..." -ForegroundColor Yellow

$sessionName = "mint-ceremony-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
$assumeJson = aws sts assume-role `
    --role-arn $MintRoleArn `
    --role-session-name $sessionName `
    --serial-number $MfaSerial `
    --token-code $tokenCode `
    --duration-seconds 3600 `
    --output json
if ($LASTEXITCODE -ne 0) {
    throw "AssumeRole failed (exit $LASTEXITCODE). Most likely cause: wrong MFA code, expired code, or trust policy mismatch."
}

$assumed = $assumeJson | ConvertFrom-Json
$accessKey   = $assumed.Credentials.AccessKeyId
$secretKey   = $assumed.Credentials.SecretAccessKey
$sessionTok  = $assumed.Credentials.SessionToken
$expiration  = $assumed.Credentials.Expiration
Write-Host "  AssumeRole OK; credentials valid until $expiration" -ForegroundColor Green

# ----- Step 3: stash the credentials in env vars FOR THIS PROCESS ONLY -----
$env:AWS_ACCESS_KEY_ID     = $accessKey
$env:AWS_SECRET_ACCESS_KEY = $secretKey
$env:AWS_SESSION_TOKEN     = $sessionTok

try {
    # ----- Step 4a: read admin DSN as the assumed role -----
    Write-Host "Reading admin DSN from SSM as the assumed role..." -ForegroundColor Yellow

    # --with-decryption returns the plaintext; we capture it ONLY into
    # a SecureString-backed variable that gets handed to the mint script
    # via stdin. The DSN never appears on the command line.
    $adminDsnPlain = aws ssm get-parameter `
        --name $AdminDsnSsmPath `
        --with-decryption `
        --region $Region `
        --query "Parameter.Value" `
        --output text
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($adminDsnPlain)) {
        throw "Failed to read admin DSN from SSM. Verify the assumed role has ssm:GetParameter on $AdminDsnSsmPath."
    }
    Write-Host "  Admin DSN read OK (length=$($adminDsnPlain.Length) chars; value not echoed)" -ForegroundColor Green

    if ($EmitDsnOnly) {
        # ----- EmitDsnOnly branch (P3-H rotation) -----
        # Convert plaintext DSN to SecureString and emit ONLY that to the
        # pipeline. Caller captures via:
        #   $assumedDsn = .\scripts\mint-with-assumed-role.ps1 -EmitDsnOnly
        # The plaintext copy is wiped in the finally block below.
        Write-Host "Returning admin DSN as SecureString to caller pipeline..." -ForegroundColor Yellow
        $secure = ConvertTo-SecureString -String $adminDsnPlain -AsPlainText -Force
        Write-Output $secure
        Write-Host "DSN emitted; remember to clear caller-side variable when done." -ForegroundColor Green
    } else {
        # ----- Mint branch (Phase 2 Commit 4) -----
        # We pipe the DSN to stdin so it never appears in process args
        # (visible via `ps`/`Get-Process` on multi-user systems).
        Write-Host "Invoking mint_worker_db_password_ssm with assumed credentials..." -ForegroundColor Yellow

        $mintArgs = @(
            "-m", "scripts.mint_worker_db_password_ssm",
            "--admin-db-url-stdin",
            "--worker-host", $WorkerHost,
            "--worker-port", "5432",
            "--worker-db-name", $WorkerDbName,
            "--ssm-path", $WorkerSsmPath,
            "--region", $Region
        )
        if ($DryRun) { $mintArgs += "--dry-run" }

        $adminDsnPlain | python @mintArgs
        if ($LASTEXITCODE -ne 0) {
            throw "Mint script exited with code $LASTEXITCODE. See script output above."
        }

        Write-Host ""
        Write-Host "Mint ceremony complete." -ForegroundColor Green
    }
}
finally {
    # ----- Step 5: clear assumed credentials, ALWAYS -----
    Remove-Item Env:\AWS_ACCESS_KEY_ID     -ErrorAction SilentlyContinue
    Remove-Item Env:\AWS_SECRET_ACCESS_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:\AWS_SESSION_TOKEN     -ErrorAction SilentlyContinue

    # Wipe plaintext DSN copy in this script's scope. In EmitDsnOnly
    # mode the SecureString in $secure is shared by reference with the
    # caller's captured variable; we drop our reference here but do NOT
    # call .Dispose() (that would destroy the caller's copy too). The
    # caller is responsible for clearing their captured variable when
    # finished, per the docstring on -EmitDsnOnly.
    if (Get-Variable -Name adminDsnPlain -ErrorAction SilentlyContinue) {
        Remove-Variable adminDsnPlain
    }
    if (Get-Variable -Name secure -ErrorAction SilentlyContinue) {
        Remove-Variable secure
    }
    Write-Host "Assumed credentials cleared from session." -ForegroundColor DarkGray
}
