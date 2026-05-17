# =====================================================================
# scripts/diag_invite_email_revoke.ps1
#
# Pulls recent CloudWatch logs for the luciel-backend service and
# greps for:
#   1. SES / welcome-set-password-email markers around the time of
#      the failing aryanbusiness030201@gmail.com invite send.
#   2. /api/v1/admin/invites DELETE traffic + any 4xx/5xx + exception
#      tracebacks around the time of the failing Revoke click.
#
# Usage (PowerShell 5.1, from C:\Users\aryan\Projects\Business\Luciel):
#   .\scripts\diag_invite_email_revoke.ps1
#
# Requires AWS CLI configured for ca-central-1 with read on the
# /ecs/luciel-backend log group.
#
# CloudWatch filter pattern syntax notes (learned the hard way):
#   * Multi-term OR uses ?"term1" ?"term2"; every term MUST be quoted
#     and prefixed with ?. Mixing quoted + bare terms makes the AWS CLI
#     shell-split them as separate options.
#   * Forward slash '/' is NOT allowed inside filter terms; use a
#     substring without the slash (e.g. "invites" not "/invites").
# =====================================================================

$ErrorActionPreference = "Stop"

$LogGroup  = "/ecs/luciel-backend"
$Region    = "ca-central-1"
# Look back 60 minutes -- both the email-not-arriving and the
# revoke-failure happened in the most recent UI session, and we want
# to be sure we catch the original Send Invite click too.
$StartTime = [DateTimeOffset]::UtcNow.AddMinutes(-60).ToUnixTimeMilliseconds()

function Invoke-LogFilter {
    param(
        [Parameter(Mandatory)][string]$Label,
        [Parameter(Mandatory)][string]$Pattern,
        [int]$Max = 100
    )
    Write-Host ""
    Write-Host "--- $Label ---" -ForegroundColor Yellow
    # Build the args array explicitly so PowerShell does not split the
    # filter pattern on its embedded spaces.
    $awsArgs = @(
        "logs", "filter-log-events",
        "--log-group-name", $LogGroup,
        "--start-time", "$StartTime",
        "--filter-pattern", $Pattern,
        "--region", $Region,
        "--max-items", "$Max",
        "--query", "events[*].[timestamp,message]",
        "--output", "text"
    )
    & aws @awsArgs
}

Write-Host "=== Log group: $LogGroup  region: $Region  start: -60min ===" -ForegroundColor Cyan

# (1) Welcome-set-password-email markers (the SES success/log path).
Invoke-LogFilter -Label "(1) welcome-set-password-email markers (last 60 min)" `
                 -Pattern '"welcome-set-password-email"'

# (2) SES / boto errors. Each OR alternative is a separately quoted ?"term".
Invoke-LogFilter -Label "(2) SES / boto errors (last 60 min)" `
                 -Pattern '?"SES send_email failed" ?"boto3 is not installed" ?"ClientError" ?"BotoCoreError" ?"MessageRejected" ?"EmailAddressNotVerified" ?"AccessDenied"'

# (3) invite_service create / error rows.
Invoke-LogFilter -Label "(3) invite_service.create_invite info/error rows (last 60 min)" `
                 -Pattern '?"Invite created tenant" ?"create_invite: invite email send failed" ?"invite_service" ?"WelcomeEmailError"'

# (4) Revoke service log line + any revoke-side error.
Invoke-LogFilter -Label "(4) revoke_invite service log (last 60 min)" `
                 -Pattern '?"Invite revoked tenant" ?"revoke_invite"'

# (5) Any mention of "invites" (no leading slash -- CloudWatch rejects /).
Invoke-LogFilter -Label "(5) any line mentioning 'invites' (last 60 min)" `
                 -Pattern '"invites"' -Max 150

# (6) Raw error / traceback. Each OR alternative quoted + ?-prefixed.
Invoke-LogFilter -Label "(6) Traceback / ERROR / Exception / IntegrityError (last 60 min)" `
                 -Pattern '?"Traceback" ?"ERROR" ?"Exception" ?"IntegrityError"'

Write-Host ""
Write-Host "=== Done ===" -ForegroundColor Cyan
