# =====================================================================
# scripts/diag_invite_email_revoke.ps1
#
# Pulls recent CloudWatch logs for the luciel-backend service.
# v3: ditches multi-term OR filter-patterns entirely (PowerShell 5.1's
# argument tokenizer keeps stripping inner quotes no matter what we do
# with splatting / escape sequences). Instead we issue one filter call
# per single-quoted term -- slower but bulletproof. We also dump the
# log group's recent stream list up front so we can SEE whether the
# log group name is correct and the service is actually writing logs.
#
# Usage (PowerShell 5.1, from C:\Users\aryan\Projects\Business\Luciel):
#   .\scripts\diag_invite_email_revoke.ps1
# =====================================================================

$ErrorActionPreference = "Continue"

$LogGroup  = "/ecs/luciel-backend"
$Region    = "ca-central-1"
$StartTime = [DateTimeOffset]::UtcNow.AddMinutes(-90).ToUnixTimeMilliseconds()

Write-Host "=== Log group: $LogGroup  region: $Region  start: -90min ===" -ForegroundColor Cyan

# ----------------------------------------------------------------------
# (0) Sanity: does the log group exist, and does it have ANY recent
#     activity? If the group name is wrong OR the service has not
#     written in -90min, every subsequent filter returns empty -- so
#     we'd misdiagnose "no SES error" when the truth is "no logs at
#     all reaching the filter."
# ----------------------------------------------------------------------
Write-Host ""
Write-Host "--- (0a) log groups matching 'luciel' ---" -ForegroundColor Yellow
aws logs describe-log-groups --log-group-name-prefix /ecs --region $Region --query "logGroups[?contains(logGroupName, 'luciel')].[logGroupName,storedBytes]" --output table

Write-Host ""
Write-Host "--- (0b) most recent 5 streams in $LogGroup ---" -ForegroundColor Yellow
aws logs describe-log-streams --log-group-name $LogGroup --region $Region --order-by LastEventTime --descending --max-items 5 --query "logStreams[*].[logStreamName,lastEventTimestamp]" --output table

# ----------------------------------------------------------------------
# Helper: filter on a single quoted term. Term is passed as a single
# CLI arg via -- to avoid any further word-splitting.
# ----------------------------------------------------------------------
function Invoke-SingleTermFilter {
    param(
        [Parameter(Mandatory)][string]$Label,
        [Parameter(Mandatory)][string]$Term,
        [int]$Max = 50
    )
    Write-Host ""
    Write-Host "--- $Label  [term: $Term] ---" -ForegroundColor Yellow
    # Quote the term so CloudWatch treats embedded spaces as a literal
    # phrase. The outer single quotes pass cleanly through powershell
    # -> aws cli without re-tokenization.
    $quoted = "`"$Term`""
    aws logs filter-log-events `
        --log-group-name $LogGroup `
        --region $Region `
        --start-time $StartTime `
        --max-items $Max `
        --filter-pattern $quoted `
        --query "events[*].[timestamp,message]" `
        --output text
}

# ----------------------------------------------------------------------
# (1) Welcome email success path log line
# ----------------------------------------------------------------------
Invoke-SingleTermFilter "(1) welcome-set-password-email success marker" "welcome-set-password-email"

# ----------------------------------------------------------------------
# (2) SES / boto failure markers -- one filter per term
# ----------------------------------------------------------------------
Invoke-SingleTermFilter "(2a) SES send_email failed"  "SES send_email failed"
Invoke-SingleTermFilter "(2b) boto3 missing"          "boto3 is not installed"
Invoke-SingleTermFilter "(2c) ClientError"            "ClientError"
Invoke-SingleTermFilter "(2d) MessageRejected"        "MessageRejected"
Invoke-SingleTermFilter "(2e) EmailAddressNotVerified" "EmailAddressNotVerified"
Invoke-SingleTermFilter "(2f) AccessDenied"           "AccessDenied"
Invoke-SingleTermFilter "(2g) WelcomeEmailError"      "WelcomeEmailError"

# ----------------------------------------------------------------------
# (3) invite_service create-side log lines
# ----------------------------------------------------------------------
Invoke-SingleTermFilter "(3a) Invite created tenant"        "Invite created tenant"
# (3b): original term contained ':' which CloudWatch rejects with
# InvalidParameterException. Drop the colon and the prose suffix --
# 'invite email send failed' is unique enough across the codebase.
Invoke-SingleTermFilter "(3b) create_invite email failure"  "invite email send failed"

# ----------------------------------------------------------------------
# (4) revoke-side log lines
# ----------------------------------------------------------------------
Invoke-SingleTermFilter "(4a) Invite revoked tenant" "Invite revoked tenant"

# ----------------------------------------------------------------------
# (5) Generic invites mention (no slash -- CloudWatch rejects /)
# ----------------------------------------------------------------------
Invoke-SingleTermFilter "(5) any line mentioning 'invites'" "invites" 200

# ----------------------------------------------------------------------
# (6) Raw error markers -- one filter per term
# ----------------------------------------------------------------------
Invoke-SingleTermFilter "(6a) Traceback"      "Traceback"
Invoke-SingleTermFilter "(6b) ERROR"          "ERROR"
Invoke-SingleTermFilter "(6c) IntegrityError" "IntegrityError"

# ----------------------------------------------------------------------
# (7) Recipient-specific: did we even attempt to send to this address?
# ----------------------------------------------------------------------
Invoke-SingleTermFilter "(7) recipient mention" "aryanbusiness030201"

Write-Host ""
Write-Host "=== Done ===" -ForegroundColor Cyan
