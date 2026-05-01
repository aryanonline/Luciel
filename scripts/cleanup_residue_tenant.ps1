<#
.SYNOPSIS
    Cleanup a single Luciel residue tenant via the prod admin API (Pattern S).

.DESCRIPTION
    Walks a tenant's resource tree leaf-first and deactivates each child
    resource, then PATCHes the tenant itself to active=false. Idempotent:
    skips resources already inactive. Emits one JSON log line per action
    taken (or skipped) via Write-Host so output is robust to [void] callers.

    Security boundary (Pattern E): admin key sourced from
    $env:LUCIEL_PROD_ADMIN_KEY, never echoed, never written to disk, never
    to shell history.

    GETs use Invoke-RestMethod. PATCH/DELETE use curl.exe.

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

# Fail loudly on any unhandled error.
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

function Invoke-AdminGet {
    param([string]$path)
    $url = "$ApiBase$path"
    $headers = @{ "Authorization" = $authHeaderValue }
    try {
        return Invoke-RestMethod -Method Get -Uri $url -Headers $headers -ErrorAction Stop
    } catch {
        $code = $null
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        Emit-Log @{ action="get-failed"; method="GET"; url=$url; http_code=$code; error=$_.Exception.Message }
        throw "GET $url failed: $($_.Exception.Message)"
    }
}

function Do-Delete {
    param([string]$path, [string]$resourceType, $resourceId)
    $url = "$ApiBase$path"
    if ($DryRun) {
        Emit-Log @{ action="would-delete"; method="DELETE"; url=$url; resource_type=$resourceType; resource_id=$resourceId }
        return
    }
    $code = curl.exe -sS -X DELETE -o NUL -w "%{http_code}" -H $authHeaderArg $url
    $codeInt = [int]$code
    $ok = ($codeInt -eq 200) -or ($codeInt -eq 204)
    Emit-Log @{
        action        = if ($ok) { "deleted" } else { "delete-failed" }
        method        = "DELETE"
        url           = $url
        http_code     = $codeInt
        resource_type = $resourceType
        resource_id   = $resourceId
    }
}

function Do-PatchTenant {
    param([string]$tenantId)
    $url = "$ApiBase/api/v1/admin/tenants/$tenantId"
    if ($DryRun) {
        Emit-Log @{ action="would-patch"; method="PATCH"; url=$url; body="active=false" }
        return
    }
    $bodyPath = Join-Path $env:TEMP "cleanup_patch_$tenantId.json"
    '{"active": false}' | Set-Content -Path $bodyPath -Encoding ascii -NoNewline
    try {
        $code = curl.exe -sS -X PATCH -o NUL -w "%{http_code}" `
            -H $authHeaderArg -H "Content-Type: application/json" `
            --data "@$bodyPath" $url
        $codeInt = [int]$code
        $ok = ($codeInt -eq 200)
        Emit-Log @{
            action        = if ($ok) { "patched" } else { "patch-failed" }
            method        = "PATCH"
            url           = $url
            http_code     = $codeInt
            resource_type = "tenant"
            resource_id   = $tenantId
        }
    } finally {
        Remove-Item $bodyPath -ErrorAction SilentlyContinue
    }
}

# ---- Walk ----
try {
    Emit-Log @{ action="walk-start"; tenant=$TenantId }

    # 1. api-keys (leaf-most)
    $apiKeys = Invoke-AdminGet "/api/v1/admin/api-keys?tenant_id=$TenantId"
    foreach ($k in @($apiKeys)) {
        if (-not $k.active) {
            Emit-Log @{ action="skipped-already-inactive"; resource_type="api-key"; resource_id=$k.id; key_prefix=$k.key_prefix }
            continue
        }
        Do-Delete -path "/api/v1/admin/api-keys/$($k.id)" -resourceType "api-key" -resourceId $k.id
    }

    # 2. luciel-instances
    $instances = Invoke-AdminGet "/api/v1/admin/luciel-instances?tenant_id=$TenantId"
    foreach ($i in @($instances)) {
        if (-not $i.active) {
            Emit-Log @{ action="skipped-already-inactive"; resource_type="luciel-instance"; resource_id=$i.id; instance_id=$i.instance_id }
            continue
        }
        Do-Delete -path "/api/v1/admin/luciel-instances/$($i.id)" -resourceType "luciel-instance" -resourceId $i.id
    }

    # 3. agents
    $agents = Invoke-AdminGet "/api/v1/admin/agents?tenant_id=$TenantId"
    foreach ($a in @($agents)) {
        if (-not $a.active) {
            Emit-Log @{ action="skipped-already-inactive"; resource_type="agent"; resource_id=$a.agent_id }
            continue
        }
        Do-Delete -path "/api/v1/admin/agents/$TenantId/$($a.agent_id)" -resourceType "agent" -resourceId $a.agent_id
    }

    # 4. domains
    $domains = Invoke-AdminGet "/api/v1/admin/domains?tenant_id=$TenantId"
    foreach ($d in @($domains)) {
        if (-not $d.active) {
            Emit-Log @{ action="skipped-already-inactive"; resource_type="domain"; resource_id=$d.domain_id }
            continue
        }
        Do-Delete -path "/api/v1/admin/domains/$TenantId/$($d.domain_id)" -resourceType "domain" -resourceId $d.domain_id
    }

    # 5. tenant PATCH (only if currently active)
    $tenants = Invoke-AdminGet "/api/v1/admin/tenants"
    $thisTenant = $tenants | Where-Object { $_.tenant_id -eq $TenantId }
    if (-not $thisTenant) {
        Emit-Log @{ action="tenant-not-found"; tenant_id=$TenantId }
        exit 4
    }
    if ($thisTenant.active) {
        Do-PatchTenant -tenantId $TenantId
    } else {
        Emit-Log @{ action="skipped-already-inactive"; resource_type="tenant"; resource_id=$TenantId }
    }

    # 6. final teardown-integrity check
    $ti = Invoke-AdminGet "/api/v1/admin/verification/teardown-integrity?tenant_id=$TenantId"
    $violationCount = if ($ti.violations) { @($ti.violations).Count } else { 0 }
    Emit-Log @{
        action          = "walk-complete"
        teardown_passed = [bool]$ti.passed
        violation_count = $violationCount
        violations      = $ti.violations
    }

    if ($DryRun) { exit 0 }
    if ($ti.passed) { exit 0 } else { exit 4 }

} catch {
    Emit-Log @{ action="walk-failed"; error=$_.Exception.Message; line=$_.InvocationInfo.ScriptLineNumber }
    exit 5
}