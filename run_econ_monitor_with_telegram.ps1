# Economic Release Monitor -- daily check + Telegram send (only when something
# new released). Requires environment variables:
#   BEA_API_KEY
#   CENSUS_API_KEY
#   TELEGRAM_BOT_TOKEN
#   TELEGRAM_CHAT_ID
#   CRAWLER_ARTIFACTS_DB   (optional -- artifact registration is best-effort)

param(
    # The interpreter that runs this job's Python steps. Mandatory and with no
    # default: an interpreter hard-coded here is a deployment decision written
    # into a public wrapper, and it silently outranks whatever the caller
    # believes it chose. A wrong one does not fail either -- it runs the job
    # against a different set of installed packages and reports success.
    [Parameter(Mandatory)] [string] $Python
)

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

Set-Location $Root

function Import-PersistedEnvVar($Name) {
    if (Test-Path "Env:$Name") {
        return
    }
    $value = [Environment]::GetEnvironmentVariable($Name, "User")
    if (!$value) {
        $value = [Environment]::GetEnvironmentVariable($Name, "Machine")
    }
    if ($value) {
        Set-Item -Path "Env:$Name" -Value $value
        Write-Host "[$(Get-Date -Format s)] Loaded $Name from persisted environment."
    }
}

Import-PersistedEnvVar "BEA_API_KEY"
Import-PersistedEnvVar "CENSUS_API_KEY"
Import-PersistedEnvVar "TELEGRAM_BOT_TOKEN"
Import-PersistedEnvVar "TELEGRAM_CHAT_ID"
Import-PersistedEnvVar "CRAWLER_ARTIFACTS_DB"

Write-Host "[$(Get-Date -Format s)] Starting economic release monitor"
& $Python (Join-Path $Root 'run_econ_monitor.py')
$exitCode = $LASTEXITCODE
Write-Host "[$(Get-Date -Format s)] run_econ_monitor.py exit code: $exitCode"

exit $exitCode
