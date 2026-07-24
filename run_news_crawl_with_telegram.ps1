# LazyCrawler news-monitor: crawl + DeepSeek digest + Telegram send
# Requires environment variables:
#   TELEGRAM_BOT_TOKEN
#   TELEGRAM_CHAT_ID
#   DEEPSEEK_API_KEY   (smart-mode local-language sources + the digest step)

param(
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
    Write-Host "[$(Get-Date -Format s)] Building report for session $sessionId"
    & $Python (Join-Path $Root 'make_news_report.py') --session-id $sessionId
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
