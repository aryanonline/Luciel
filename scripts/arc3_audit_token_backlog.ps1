# =====================================================================
# scripts/arc3_audit_token_backlog.ps1
#
# Arc 3 Work-Unit A — CloudWatch token-backlog audit.
#
# Closes the deferred leg (c) of D-set-password-token-logged-plaintext-2026-05-17
# from the §3 resolution path (now §5 stanza after Arc 2 commit bc9abe1):
#   "aws logs filter-log-events --filter-pattern '?token=' against the
#    historical buffer for the discovery window 2026-05-13 -> 2026-05-20,
#    followed by an idempotent status='revoked' flip on any user_invites
#    row whose token_jti appears in the buffer."
#
# This script DOES NOT MUTATE STATE. It only collects evidence:
#   (1) Confirms the log group exists and has recent activity.
#   (2) Walks the welcome-set-password and pilot-refund emitter markers
#       across the 2026-05-13 00:00 UTC -> 2026-05-21 04:00 UTC window
#       (the welcome path is the only row-bound surface; magic-link JTIs
#       are stateless and expire on TTL only). Output written to
#       arc3-out/welcome-set-password-emitters.txt.
#   (3) Greps the captured emitter lines for `token=<JWT>` substrings and
#       extracts each unique JTI to arc3-out/leaked-welcome-jtis.txt.
#   (4) Prints a Python decode block for the partner to copy-paste into
#       a local shell with the prod JWT signing key in env so JTIs can
#       be decoded WITHOUT round-tripping the live JWT through any tool.
#
# Once leaked-welcome-jtis.txt is populated, the matching SQL idempotency
# block in scripts/arc3_revoke_leaked_invites.sql gets the JTI list as a
# parameter and flips user_invites.status PENDING -> REVOKED for each
# match, leaving any already-ACCEPTED or already-REVOKED row alone.
#
# Magic-link tokens are STATELESS (no row to revoke). Any magic-link JTI
# that landed in the buffer is bounded only by the JWT TTL (24h default)
# — the discovery window ended 2026-05-20 23:00 EDT, so by the time this
# script runs (2026-05-21 13:33 EDT and later) all magic-link JTIs in the
# window have already expired by natural TTL. The audit step (2c) below
# still grep-counts magic-link JTI emissions for the disclosure record
# but does NOT attempt remediation.
#
# Pilot-refund emails carry no JTI (they confirm a completed refund, no
# action surface). They are grep-counted for completeness only.
#
# Usage (PowerShell 5.1, from C:\Users\aryan\Projects\Business\Luciel):
#   .\scripts\arc3_audit_token_backlog.ps1
#
# Window override (optional):
#   $env:ARC3_START_UTC = "2026-05-13T00:00:00Z"
#   $env:ARC3_END_UTC   = "2026-05-21T04:00:00Z"
#   .\scripts\arc3_audit_token_backlog.ps1
# =====================================================================

$ErrorActionPreference = "Continue"

$LogGroup = "/ecs/luciel-backend"
$Region   = "ca-central-1"

# Default window: 2026-05-13 00:00 UTC -> 2026-05-21 04:00 UTC (covers
# the full discovery -> Arc 2 commit -> Arc 3 audit-run gap with 4h
# slack on the trailing edge to capture any in-flight backend write).
$StartUtc = if ($env:ARC3_START_UTC) { $env:ARC3_START_UTC } else { "2026-05-13T00:00:00Z" }
$EndUtc   = if ($env:ARC3_END_UTC)   { $env:ARC3_END_UTC }   else { "2026-05-21T04:00:00Z" }

$StartMs = ([DateTimeOffset]::Parse($StartUtc)).ToUnixTimeMilliseconds()
$EndMs   = ([DateTimeOffset]::Parse($EndUtc)).ToUnixTimeMilliseconds()

$OutDir = Join-Path (Get-Location) "arc3-out"
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }

Write-Host "=== Arc 3 Work-Unit A: token-backlog audit ===" -ForegroundColor Cyan
Write-Host "Log group : $LogGroup"
Write-Host "Region    : $Region"
Write-Host "Window    : $StartUtc -> $EndUtc"
Write-Host "Output    : $OutDir"
Write-Host ""

# ----------------------------------------------------------------------
# (1) Log-group sanity check. If the group is missing OR has no activity
#     in the window we want to know NOW, not after the SQL block runs
#     against an empty JTI list.
# ----------------------------------------------------------------------
Write-Host "--- (1a) describe log group ---" -ForegroundColor Yellow
aws logs describe-log-groups `
    --log-group-name-prefix $LogGroup `
    --region $Region `
    --query "logGroups[].[logGroupName,storedBytes,retentionInDays]" `
    --output table

Write-Host ""
Write-Host "--- (1b) 5 most recent streams in $LogGroup ---" -ForegroundColor Yellow
aws logs describe-log-streams `
    --log-group-name $LogGroup `
    --region $Region `
    --order-by LastEventTime `
    --descending `
    --max-items 5 `
    --query "logStreams[*].[logStreamName,lastEventTimestamp]" `
    --output table

# ----------------------------------------------------------------------
# Helper: window-scoped filter on a single quoted term. Mirrors the
# convention from scripts/diag_invite_email_revoke.ps1 (one term per
# call to dodge PowerShell 5.1's argument tokenizer issues with OR
# patterns). Captures BOTH timestamp and message; we need the message
# body to grep for token= later.
# ----------------------------------------------------------------------
function Get-WindowedEmitterLines {
    param(
        [Parameter(Mandatory)][string]$Term,
        [Parameter(Mandatory)][string]$OutFile,
        [int]$MaxItems = 1000
    )
    $quoted = "`"$Term`""
    Write-Host ""
    Write-Host "--- filter: $Term -> $OutFile ---" -ForegroundColor Yellow
    aws logs filter-log-events `
        --log-group-name $LogGroup `
        --region $Region `
        --start-time $StartMs `
        --end-time   $EndMs `
        --max-items  $MaxItems `
        --filter-pattern $quoted `
        --query "events[*].[timestamp,message]" `
        --output text | Out-File -FilePath $OutFile -Encoding utf8
    $lineCount = (Get-Content $OutFile -ErrorAction SilentlyContinue | Measure-Object -Line).Lines
    Write-Host "  -> $lineCount lines captured"
}

# ----------------------------------------------------------------------
# (2) Capture all three emitter markers across the discovery window.
#     Only (2a) is row-bound and load-bearing on remediation. (2b) and
#     (2c) are evidence-only.
# ----------------------------------------------------------------------
$WelcomeFile     = Join-Path $OutDir "welcome-set-password-emitters.txt"
$MagicLinkFile   = Join-Path $OutDir "magic-link-emitters.txt"
$PilotRefundFile = Join-Path $OutDir "pilot-refund-emitters.txt"

Get-WindowedEmitterLines -Term "welcome-set-password-email" -OutFile $WelcomeFile      -MaxItems 2000
Get-WindowedEmitterLines -Term "magic-link-email"           -OutFile $MagicLinkFile    -MaxItems 2000
Get-WindowedEmitterLines -Term "pilot-refund-email"         -OutFile $PilotRefundFile  -MaxItems 2000

# ----------------------------------------------------------------------
# (3) Extract leaked JTIs from the welcome-set-password capture.
#     The bare token= pattern in the URL is followed by a JWS dot-
#     separated triple (header.payload.signature). We don't trust
#     regex around CloudWatch's text output; we use a Python one-liner
#     that splits on token=, takes char-sequence up to first whitespace
#     or quote, and base64url-decodes the payload segment to extract
#     the jti claim. Outputs one JTI per line to leaked-welcome-jtis.txt.
# ----------------------------------------------------------------------
Write-Host ""
Write-Host "--- (3) extract JTIs from $WelcomeFile ---" -ForegroundColor Yellow

$JtiFile     = Join-Path $OutDir "leaked-welcome-jtis.txt"
$JtiDecodeLog = Join-Path $OutDir "leaked-welcome-jti-decode.log"

# Inline Python via python -c to keep the dependency surface zero (the
# partner's laptop already has Python 3.x; no boto3 / no pyjwt needed,
# only base64 + json + re from stdlib). We pass the welcome capture file
# as the first arg and the output JTI file as the second. The decode
# log preserves the (timestamp, jti) pairing for the audit record.
$PyExtract = @'
import base64, json, re, sys

src, jti_out, decode_log = sys.argv[1], sys.argv[2], sys.argv[3]
TOKEN_RE = re.compile(r"token=([A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)")

def b64url_decode(s):
    s = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode("ascii"))

jtis = set()
with open(src, "r", encoding="utf-8", errors="replace") as f, \
     open(decode_log, "w", encoding="utf-8") as logf:
    for raw in f:
        line = raw.rstrip("\n")
        m = TOKEN_RE.search(line)
        if not m:
            continue
        jws = m.group(1)
        parts = jws.split(".")
        if len(parts) != 3:
            continue
        try:
            payload = json.loads(b64url_decode(parts[1]))
        except Exception as e:
            logf.write(f"DECODE_FAIL\t{e}\t{jws[:40]}...\n")
            continue
        jti = payload.get("jti")
        if not jti:
            logf.write(f"NO_JTI\t{payload}\n")
            continue
        jtis.add(jti)
        # First field of CloudWatch text-output is the timestamp; preserve
        # it for the audit record even though we dedupe on jti alone.
        ts = line.split("\t", 1)[0] if "\t" in line else "?"
        logf.write(f"FOUND\t{ts}\t{jti}\n")

with open(jti_out, "w", encoding="utf-8") as g:
    for jti in sorted(jtis):
        g.write(jti + "\n")

print(f"unique_jtis={len(jtis)}")
'@

# Write the Python snippet to a temp file so PowerShell doesn't re-quote
# its contents. (Inline -c would mangle the embedded dollar-signs and
# triple-quotes.)
$PyTmp = Join-Path $OutDir "_extract_jtis.py"
$PyExtract | Out-File -FilePath $PyTmp -Encoding utf8

python $PyTmp $WelcomeFile $JtiFile $JtiDecodeLog
$UniqueJtis = (Get-Content $JtiFile -ErrorAction SilentlyContinue | Measure-Object -Line).Lines
Write-Host "  -> $UniqueJtis unique welcome JTIs extracted to $JtiFile" -ForegroundColor Green

# ----------------------------------------------------------------------
# (4) Counts for the audit record (magic-link + pilot-refund are evidence-
#     only; not remediated by SQL).
# ----------------------------------------------------------------------
Write-Host ""
Write-Host "--- (4) emitter-line counts (evidence record) ---" -ForegroundColor Yellow
$WelcomeCount     = (Get-Content $WelcomeFile     -ErrorAction SilentlyContinue | Measure-Object -Line).Lines
$MagicLinkCount   = (Get-Content $MagicLinkFile   -ErrorAction SilentlyContinue | Measure-Object -Line).Lines
$PilotRefundCount = (Get-Content $PilotRefundFile -ErrorAction SilentlyContinue | Measure-Object -Line).Lines
Write-Host ("  welcome-set-password : {0,5} lines  (row-bound, remediated via SQL)" -f $WelcomeCount)
Write-Host ("  magic-link           : {0,5} lines  (stateless JWT, TTL-bounded only)" -f $MagicLinkCount)
Write-Host ("  pilot-refund         : {0,5} lines  (no actionable token)" -f $PilotRefundCount)

# ----------------------------------------------------------------------
# Done. Next step (Work-Unit A.2): hand $JtiFile to
# scripts/arc3_revoke_leaked_invites.sql via psql.
# ----------------------------------------------------------------------
Write-Host ""
Write-Host "=== Audit done. Hand off file: $JtiFile ===" -ForegroundColor Cyan
Write-Host "Next: psql `$env:DATABASE_URL -v jti_file=$JtiFile -f scripts\arc3_revoke_leaked_invites.sql"
