# =====================================================================
# scripts/diag_tail_backend.ps1
#
# Reads the literal tail of the most recent /ecs/luciel-backend log
# stream -- no filters, no search terms. Use this when filter-based
# diagnostics return empty across the board: it answers "what is the
# service actually writing right now?" so we can pick real search
# terms from real evidence on the next pass.
#
# Usage: .\scripts\diag_tail_backend.ps1
# =====================================================================

$ErrorActionPreference = "Continue"

$LogGroup = "/ecs/luciel-backend"
$Region   = "ca-central-1"

Write-Host "=== Tail of most-recent stream in $LogGroup ===" -ForegroundColor Cyan
Write-Host ""

# Find the latest stream name.
$streamJson = aws logs describe-log-streams `
    --log-group-name $LogGroup `
    --region $Region `
    --order-by LastEventTime `
    --descending `
    --max-items 1 `
    --output json | ConvertFrom-Json

$streamName = $streamJson.logStreams[0].logStreamName
$lastTs     = $streamJson.logStreams[0].lastEventTimestamp
$lastUtc    = [DateTimeOffset]::FromUnixTimeMilliseconds([int64]$lastTs).UtcDateTime
Write-Host "Stream:    $streamName" -ForegroundColor Yellow
Write-Host "Last evt:  $lastUtc UTC" -ForegroundColor Yellow
Write-Host ""

# Last 200 events on that stream, no filter.
Write-Host "--- last 200 events on stream (newest at bottom) ---" -ForegroundColor Yellow
aws logs get-log-events `
    --log-group-name $LogGroup `
    --log-stream-name $streamName `
    --region $Region `
    --limit 200 `
    --output text `
    --query "events[*].[timestamp,message]"

Write-Host ""
Write-Host "=== Done ===" -ForegroundColor Cyan
