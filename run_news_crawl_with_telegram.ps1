# LazyCrawler news-monitor: crawl + DeepSeek digest + Telegram send
# Requires environment variables:
#   TELEGRAM_BOT_TOKEN
#   TELEGRAM_CHAT_ID
#   DEEPSEEK_API_KEY   (smart-mode local-language sources + the digest step)

param(
    # The source list to crawl. Mandatory and passed straight through: there
    # is no default and no search path, because a crawl running silently
    # against someone else's curation produces a plausible digest of the
    # wrong world.
    [Parameter(Mandatory)] [string]   $SourcesConfig,
    # Passed straight through to make_news_report.py --digest-engines.
    # Mandatory: which engines write the digest is a cost decision, and a
    # default here would spend one desk's budget on another's behalf.
    [Parameter(Mandatory)] [string]   $DigestEngines,
    # Passed straight through to make_news_report.py --cycle. Identifies
    # which scheduled run produced this digest, and is stored so the delta
    # report can pull "the last N usclose digests" precisely rather than
    # guessing from timestamps. Mandatory: a wrong or empty cycle does not
    # fail, it silently files the digest under the wrong heading.
    [Parameter(Mandatory)] [string]   $Cycle,
    [string[]]$CrawlArgs = @()
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
$crawlOutput = & $Python (Join-Path $Root 'run_news_crawl.py') --sources-config $SourcesConfig @CrawlArgs 2>&1 | Tee-Object -Variable crawlOutputVar
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
