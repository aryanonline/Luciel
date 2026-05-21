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
# Helper: window-scoped CloudWatch filter. CRITICAL FIX (Arc 3, 2026-05-21):
# the previous version passed hyphenated multi-token patterns like
# "welcome-set-password-email" as a quoted filter. CloudWatch Logs
# tokenizes filter patterns on hyphens (and other non-alphanumerics),
# so a quoted hyphenated string silently matches zero events — even
# though the substring appears verbatim in the message body. Partner's
# manual run with the OR-filter `?token= ?jti=` proved real leaked
# JTIs exist in the same window the script reported as empty.
#
# Fix: filter ONCE on the broad tokenizer-safe OR-pattern `?token= ?jti=`
# (catches every emitter that leaked a JWT in either URL-arg or claim
# form), capture the union to a single raw file, then grep that file
# locally with Select-String for the per-emitter tags. PowerShell's
# regex layer is hyphen-safe; only CloudWatch's tokenizer is not.
# ----------------------------------------------------------------------
function Get-WindowedRawCapture {
    param(
        [Parameter(Mandatory)][string]$Pattern,
        [Parameter(Mandatory)][string]$OutFile,
        [int]$MaxItems = 5000
    )
    Write-Host ""
    Write-Host "--- filter (CloudWatch): $Pattern -> $OutFile ---" -ForegroundColor Yellow
    # IMPORTANT: the CloudWatch OR-filter syntax ?token= ?jti= contains a
    # space that PowerShell would otherwise split into two CLI args. We
    # build the argv array explicitly and let PowerShell pass --filter-
    # pattern's value as a single quoted argument to aws.exe. This is
    # the same pattern the partner used in the manual run that returned
    # real leaked JTIs.
    $awsArgs = @(
        "logs", "filter-log-events",
        "--log-group-name", $LogGroup,
        "--region", $Region,
        "--start-time", $StartMs,
        "--end-time", $EndMs,
        "--max-items", $MaxItems,
        "--filter-pattern", $Pattern,
        "--query", "events[*].[timestamp,message]",
        "--output", "text"
    )
    & aws @awsArgs | Out-File -FilePath $OutFile -Encoding utf8
    $lineCount = (Get-Content $OutFile -ErrorAction SilentlyContinue | Measure-Object -Line).Lines
    Write-Host "  -> $lineCount lines captured"
}

function Split-EmitterTag {
    param(
        [Parameter(Mandatory)][string]$SourceFile,
        [Parameter(Mandatory)][string]$Tag,
        [Parameter(Mandatory)][string]$OutFile
    )
    # Hyphens are fine in PowerShell's Select-String regex; the
    # CloudWatch tokenizer is the ONLY layer that splits on them.
    if (Test-Path $SourceFile) {
        Select-String -Path $SourceFile -Pattern ([regex]::Escape($Tag)) -SimpleMatch `
            | ForEach-Object { $_.Line } `
            | Out-File -FilePath $OutFile -Encoding utf8
    } else {
        "" | Out-File -FilePath $OutFile -Encoding utf8
    }
    $lineCount = (Get-Content $OutFile -ErrorAction SilentlyContinue | Measure-Object -Line).Lines
    Write-Host ("  split -> {0,5} lines for tag '{1}' -> {2}" -f $lineCount, $Tag, (Split-Path -Leaf $OutFile))
}

# ----------------------------------------------------------------------
# (2) Single broad capture, then local per-emitter split.
#     Only the welcome-set-password subset is row-bound and load-bearing
#     on remediation; magic-link and pilot-refund splits are evidence-only.
# ----------------------------------------------------------------------
$RawFile         = Join-Path $OutDir "token-jti-raw.txt"
$WelcomeFile     = Join-Path $OutDir "welcome-set-password-emitters.txt"
$MagicLinkFile   = Join-Path $OutDir "magic-link-emitters.txt"
$PilotRefundFile = Join-Path $OutDir "pilot-refund-emitters.txt"

# Tokenizer-safe OR-filter — matches the partner-validated manual run.
Get-WindowedRawCapture -Pattern "?token= ?jti=" -OutFile $RawFile -MaxItems 10000

Write-Host ""
Write-Host "--- (2x) split raw capture by emitter tag ---" -ForegroundColor Yellow
Split-EmitterTag -SourceFile $RawFile -Tag "welcome-set-password-email" -OutFile $WelcomeFile
Split-EmitterTag -SourceFile $RawFile -Tag "magic-link-email"           -OutFile $MagicLinkFile
Split-EmitterTag -SourceFile $RawFile -Tag "pilot-refund-email"         -OutFile $PilotRefundFile

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

# Run the extractor against BOTH the welcome subset (for the load-bearing
# JTI list that drives the SQL flip) AND the full raw capture (so the
# decode log records every leaked JTI in the window, including magic-
# link JTIs that are TTL-expired but still need disclosure-record entries).
python $PyTmp $WelcomeFile $JtiFile $JtiDecodeLog
$UniqueJtis = (Get-Content $JtiFile -ErrorAction SilentlyContinue | Measure-Object -Line).Lines
Write-Host "  -> $UniqueJtis unique welcome JTIs extracted to $JtiFile" -ForegroundColor Green

$AllJtiFile      = Join-Path $OutDir "leaked-all-jtis.txt"
$AllJtiDecodeLog = Join-Path $OutDir "leaked-all-jti-decode.log"
python $PyTmp $RawFile $AllJtiFile $AllJtiDecodeLog
$AllUniqueJtis = (Get-Content $AllJtiFile -ErrorAction SilentlyContinue | Measure-Object -Line).Lines
Write-Host "  -> $AllUniqueJtis unique JTIs (all emitters) extracted to $AllJtiFile" -ForegroundColor Green

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
