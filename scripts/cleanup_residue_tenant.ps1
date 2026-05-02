<#
.SYNOPSIS
    Trigger tenant cascade deactivation via the prod admin API.

.DESCRIPTION
    Step 28 commit `8b-prereq-data-tenant-cascade-in-code` (2026-05-02)
    moved cascade logic from this PowerShell walker into the platform
    itself: PATCH /api/v1/admin/tenants/{id} with active=false now
    atomically deactivates every tenant-scoped resource (memory_items,
    api_keys, luciel_instances, agents, agent_configs, domain_configs)
    in a single transaction with full audit-row emission.

    This script is now a thin operator trigger for that cascade. It:
      1. PATCHes the tenant active=false (cascade fires server-side)
      2. Runs teardown-integrity probe to verify zero live residue

    Idempotent: re-running against an already-inactive tenant is a
    no-op (memory cascade still emits a count=0 audit row by design).

    Security boundary (Pattern E): admin key sourced from
    $env:LUCIEL_PROD_ADMIN_KEY, never echoed, never disk, never history.

    History: Pre-2026-05-02 this script implemented Pattern S walker
    leaf-first cleanup (api-keys, luciel-instances, agents, domains,
    tenant). That logic is now in
    AdminService.deactivate_tenant_with_cascade and is tested by
    Pillar 18 (tenant cascade end-to-end). The pre-rewrite version
    is in git history under commits prior to 8b-prereq-data-tenant-
    cascade-in-code.

.PARAMETER TenantId
    The tenant_id of the residue tenant to clean up.

.PARAMETER ApiBase
    Base URL of the prod API. Default: https://api.vantagemind.ai

.PARAMETER DryRun
    If set, list every action that would be taken but do not call any
    state-changing endpoint.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [string]$TenantId,

    [string]$ApiBase = "https://api.vantagemind.ai",

    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

if (-not $env:LUCIEL_PROD_ADMIN_KEY) {
    Write-Error "LUCIEL_PROD_ADMIN_KEY not set in environment."
    exit 2
}
if (-not $env:LUCIEL_PROD_ADMIN_KEY.StartsWith("luc_sk_")) {
    Write-Error "LUCIEL_PROD_ADMIN_KEY does not have expected luc_sk_ prefix."
    exit 2
}

$authHeaderValue = "Bearer $env:LUCIEL_PROD_ADMIN_KEY"
$authHeaderArg   = "Authorization: $authHeaderValue"

function Emit-Log {
    param([hashtable]$entry)
    $entry["timestamp"] = (Get-Date -Format "yyyy-MM-ddTHH:mm:ss.fffZ")
    $entry["tenant_id"] = $TenantId
    $entry["dry_run"]   = [bool]$DryRun
    Write-Host ($entry | ConvertTo-Json -Compress)
}

try {
    Emit-Log @{ action = "cascade-trigger-start"; tenant = $TenantId }

    # 1. PATCH tenant active=false -- server-side cascade fires.
    $url = "$ApiBase/api/v1/admin/tenants/$TenantId"
    if ($DryRun) {
        Emit-Log @{ action = "would-patch"; method = "PATCH"; url = $url; body = "active=false" }
    } else {
        $bodyPath = Join-Path $env:TEMP "cleanup_patch_$TenantId.json"
        '{"active": false}' | Set-Content -Path $bodyPath -Encoding ascii -NoNewline
        try {
            $code = curl.exe -sS -X PATCH -o NUL -w "%{http_code}" `
                -H $authHeaderArg -H "Content-Type: application/json" `
                --data "@$bodyPath" $url
            $codeInt = [int]$code
            $ok = ($codeInt -eq 200)
            Emit-Log @{
                action        = if ($ok) { "cascade-triggered" } else { "patch-failed" }
                method        = "PATCH"
                url           = $url
                http_code     = $codeInt
                resource_type = "tenant"
                resource_id   = $TenantId
            }
            if (-not $ok) {
                Emit-Log @{ action = "abort"; reason = "tenant PATCH returned $codeInt" }
                exit 4
            }
        } finally {
            Remove-Item $bodyPath -ErrorAction SilentlyContinue
        }
    }

    # 2. Teardown-integrity probe -- verify zero live residue.
    $integrityUrl = "$ApiBase/api/v1/admin/verification/teardown-integrity?tenant_id=$TenantId"
    if ($DryRun) {
        Emit-Log @{ action = "would-check"; method = "GET"; url = $integrityUrl }
        Emit-Log @{ action = "walk-complete"; teardown_passed = $null; note = "dry-run" }
        exit 0
    }

    $headers = @{ "Authorization" = $authHeaderValue }
    $ti = Invoke-RestMethod -Method Get -Uri $integrityUrl -Headers $headers -ErrorAction Stop
    $violationCount = if ($ti.violations) { @($ti.violations).Count } else { 0 }
    Emit-Log @{
        action          = "walk-complete"
        teardown_passed = [bool]$ti.passed
        violation_count = $violationCount
        violations      = $ti.violations
    }

    if ($ti.passed) { exit 0 } else { exit 4 }

} catch {
    Emit-Log @{
        action = "walk-failed"
        error  = $_.Exception.Message
        line   = $_.InvocationInfo.ScriptLineNumber
    }
    exit 5
}