# LazyCrawler news delta report: what's new in the morning digest vs the
# preceding evening (usclose) digests -- sent to Telegram.
# Requires environment variables:
#   TELEGRAM_BOT_TOKEN
#   TELEGRAM_CHAT_ID
#   DEEPSEEK_API_KEY   (only needed if the morning digest itself fell back to deepseek)

param(
    # All three mandatory. Which cycle is checked against which baseline, and
    # over how many digests, decides what counts as "new" — and a default
    # here would let one desk's editorial judgement run silently for
    # another's.
    [Parameter(Mandatory)] [string] $QueryCycle,
    [Parameter(Mandatory)] [string] $BaselineCycle,
    [Parameter(Mandatory)] [int]    $BaselineCount
)

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = 'C:\ProgramData\spyder-6\python.exe'

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

Import-PersistedEnvVar "TELEGRAM_BOT_TOKEN"
Import-PersistedEnvVar "TELEGRAM_CHAT_ID"
Import-PersistedEnvVar "DEEPSEEK_API_KEY"
Import-PersistedEnvVar "CRAWLER_ARTIFACTS_DB"

Write-Host "[$(Get-Date -Format s)] Building digest delta report: query=$QueryCycle baseline=$BaselineCycle x$BaselineCount"
& $Python (Join-Path $Root 'make_digest_delta_report.py') --query-cycle $QueryCycle --baseline-cycle $BaselineCycle --baseline-count $BaselineCount --send
$exit = $LASTEXITCODE
Write-Host "[$(Get-Date -Format s)] make_digest_delta_report.py exit code: $exit"

exit $exit
