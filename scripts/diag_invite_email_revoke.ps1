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
#   pwsh -File .\luciel_repo\scripts\diag_invite_email_revoke.ps1
# or simply:
#   .\luciel_repo\scripts\diag_invite_email_revoke.ps1
#
# Requires AWS CLI configured for ca-central-1 with read on the
# /ecs/luciel-backend log group.
# =====================================================================

$ErrorActionPreference = "Stop"

$LogGroup  = "/ecs/luciel-backend"
$Region    = "ca-central-1"
# Look back 30 minutes -- both the email-not-arriving and the
# revoke-failure happened in the most recent UI session.
$StartTime = [DateTimeOffset]::UtcNow.AddMinutes(-30).ToUnixTimeMilliseconds()

Write-Host "=== Log group: $LogGroup  region: $Region  start: -30min ===" -ForegroundColor Cyan
Write-Host ""

# ---------------------------------------------------------------
# 1. Welcome-set-password-email markers (the SES path)
# ---------------------------------------------------------------
Write-Host "--- (1) welcome-set-password-email markers (last 30 min) ---" -ForegroundColor Yellow
aws logs filter-log-events `
    --log-group-name $LogGroup `
    --start-time $StartTime `
    --filter-pattern '"welcome-set-password-email"' `
    --region $Region `
    --max-items 50 `
    --query 'events[*].[timestamp,message]' `
    --output text

Write-Host ""
Write-Host "--- (2) any SES / boto errors (last 30 min) ---" -ForegroundColor Yellow
aws logs filter-log-events `
    --log-group-name $LogGroup `
    --start-time $StartTime `
    --filter-pattern '?"SES send_email failed" ?"boto3 is not installed" ?"ClientError" ?"BotoCoreError" ?MessageRejected ?"EmailAddressNotVerified" ?"AccessDenied"' `
    --region $Region `
    --max-items 50 `
    --query 'events[*].[timestamp,message]' `
    --output text

Write-Host ""
Write-Host "--- (3) invite_service.create_invite info/error rows (last 30 min) ---" -ForegroundColor Yellow
aws logs filter-log-events `
    --log-group-name $LogGroup `
    --start-time $StartTime `
    --filter-pattern '?"Invite created tenant" ?"create_invite: invite email send failed" ?"invite_service" ?"WelcomeEmailError"' `
    --region $Region `
    --max-items 50 `
    --query 'events[*].[timestamp,message]' `
    --output text

Write-Host ""
Write-Host "--- (4) DELETE /admin/invites traffic + revoke service log (last 30 min) ---" -ForegroundColor Yellow
aws logs filter-log-events `
    --log-group-name $LogGroup `
    --start-time $StartTime `
    --filter-pattern '?"DELETE /api/v1/admin/invites" ?"Invite revoked tenant" ?"revoke_invite"' `
    --region $Region `
    --max-items 50 `
    --query 'events[*].[timestamp,message]' `
    --output text

Write-Host ""
Write-Host "--- (5) any HTTP 4xx/5xx mentioning /invites (last 30 min) ---" -ForegroundColor Yellow
aws logs filter-log-events `
    --log-group-name $LogGroup `
    --start-time $StartTime `
    --filter-pattern '"/invites"' `
    --region $Region `
    --max-items 100 `
    --query 'events[*].[timestamp,message]' `
    --output text

Write-Host ""
Write-Host "--- (6) raw ERROR / Traceback (last 30 min) ---" -ForegroundColor Yellow
aws logs filter-log-events `
    --log-group-name $LogGroup `
    --start-time $StartTime `
    --filter-pattern '?"Traceback" ?"ERROR" ?"Exception" ?"IntegrityError"' `
    --region $Region `
    --max-items 100 `
    --query 'events[*].[timestamp,message]' `
    --output text

Write-Host ""
Write-Host "=== Done ===" -ForegroundColor Cyan
