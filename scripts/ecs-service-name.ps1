<#
.SYNOPSIS
Returns the canonical Luciel ECS service name for a given task-definition family.

.DESCRIPTION
Eliminates the recurring `luciel-backend` vs `luciel-backend-service`
transcription hazard documented in:

  - drift D-ecs-service-name-asymmetry-with-td-family-2026-05-05
  - CANONICAL_RECAP v3.0 Section 15 ("Phase 3 closure sweep")
  - this script's sister runbook section
    (docs/runbooks/operator-patterns.md "AWS CLI naming hygiene")

Convention (canonical, do not deviate):

  TD family name   ->  ECS service name
  ---------------      ----------------
  luciel-backend       luciel-backend-service
  luciel-worker        luciel-worker-service
  luciel-verify        (no service -- run-task only, no -service suffix)
  luciel-mint          (no service -- run-task only, no -service suffix)
  luciel-migrate       (no service -- run-task only, no -service suffix)

Long-running services always have the "-service" suffix on the service
name; the underlying TD family does NOT carry the suffix. One-shot task
definitions (verify, mint, migrate) have no associated ECS service at
all -- they only ever run via `aws ecs run-task`, so calling
`Get-LucielEcsServiceName` on them is a logic error and the function
will write a warning + return $null.

.PARAMETER Family
The task-definition family name without the "-service" suffix
(e.g. "luciel-backend", "luciel-worker"). Required.

.EXAMPLE
PS> .\scripts\ecs-service-name.ps1 luciel-backend
luciel-backend-service

.EXAMPLE
PS> $svc = .\scripts\ecs-service-name.ps1 luciel-worker
PS> aws ecs describe-services --cluster luciel-cluster --services $svc --region ca-central-1

.EXAMPLE
PS> .\scripts\ecs-service-name.ps1 luciel-verify
WARNING: 'luciel-verify' is a one-shot task family with no associated
ECS service. Use 'aws ecs run-task' against the task definition
directly. Returning $null.

.NOTES
This is a PURE function -- it makes no AWS API calls and does not
require any IAM permissions. It exists solely to canonicalize the
naming convention so operator transcription cannot drift it.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateNotNullOrEmpty()]
    [string]$Family
)

# Long-running services -- ALWAYS have a "-service" suffix on the service name.
$LongRunningFamilies = @(
    'luciel-backend',
    'luciel-worker'
)

# One-shot task families -- NO associated ECS service.
$OneShotFamilies = @(
    'luciel-verify',
    'luciel-mint',
    'luciel-migrate'
)

if ($LongRunningFamilies -contains $Family) {
    return "$Family-service"
}

if ($OneShotFamilies -contains $Family) {
    Write-Warning ("'{0}' is a one-shot task family with no associated " +
        "ECS service. Use 'aws ecs run-task' against the task " +
        "definition directly. Returning `$null." -f $Family)
    return $null
}

# Unknown family -- be loud, do not silently return a guess.
Write-Error ("Unknown task-definition family '{0}'. Known long-running: " +
    "{1}. Known one-shot: {2}. If this is a new family, update " +
    "scripts/ecs-service-name.ps1 in the same commit that introduces " +
    "the new family." -f $Family,
    ($LongRunningFamilies -join ', '),
    ($OneShotFamilies -join ', '))
exit 1
