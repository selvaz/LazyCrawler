# LazyCrawler news-monitor: crawl + DeepSeek digest + Telegram send
# Requires environment variables:
#   TELEGRAM_BOT_TOKEN
#   TELEGRAM_CHAT_ID
#   DEEPSEEK_API_KEY   (smart-mode local-language sources + the digest step)

param(
    [string[]]$CrawlArgs = @(),
    # Passed straight through to make_news_report.py --digest-engines.
    # Default "claude" matches the EuropeClose/USClose cycles' current
    # behavior; the Morning task is registered with "claude,deepseek" (see
    # setup_scheduler.ps1) so that cycle sends both digests for comparison.
    [string]$DigestEngines = "claude",
    # Passed straight through to make_news_report.py --cycle -- identifies
    # which scheduled task produced this run (morning/europeclose/usclose),
    # stored in digests.db so make_digest_delta_report.py can pull "the
    # last N usclose digests" precisely instead of guessing from timestamps.
    [string]$Cycle = ""
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

Write-Host "[$(Get-Date -Format s)] Starting news crawl: $($CrawlArgs -join ' ')"
$crawlOutput = & $Python (Join-Path $Root 'run_news_crawl.py') @CrawlArgs 2>&1 | Tee-Object -Variable crawlOutputVar
$crawlExit = $LASTEXITCODE
Write-Host "[$(Get-Date -Format s)] run_news_crawl.py exit code: $crawlExit"

$sessionLine = $crawlOutputVar | Select-String -Pattern '^SESSION_ID=' | Select-Object -Last 1
$sessionId = $null
if ($sessionLine) {
    $sessionId = $sessionLine.ToString().Split('=')[1].Trim()
}

if ($crawlExit -eq 0 -and $sessionId) {
    $reportArgs = @('--session-id', $sessionId, '--digest-engines', $DigestEngines)
    if ($Cycle) {
        $reportArgs += @('--cycle', $Cycle)
    }
    Write-Host "[$(Get-Date -Format s)] Building report for session $sessionId (digest engines: $DigestEngines, cycle: $Cycle)"
    & $Python (Join-Path $Root 'make_news_report.py') @reportArgs
    $reportExit = $LASTEXITCODE
    Write-Host "[$(Get-Date -Format s)] make_news_report.py exit code: $reportExit"

    Write-Host "[$(Get-Date -Format s)] Sending Telegram report"
    & $Python (Join-Path $Root 'send_telegram_news_report.py') --session-id $sessionId
    $telegramExit = $LASTEXITCODE
    Write-Host "[$(Get-Date -Format s)] Telegram report exit code: $telegramExit"
} else {
    Write-Warning "Skipping report/Telegram: crawl failed or no session id (exit $crawlExit)."
}

exit $crawlExit
