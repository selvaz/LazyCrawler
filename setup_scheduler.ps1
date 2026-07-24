# ============================================================================
# setup_scheduler.ps1 -- creates the 3 daily Windows scheduled tasks for the
# LazyCrawler news-monitor (crawl + DeepSeek digest + Telegram send).
#
# Times are chosen for an Ireland-based user (GMT/IST), converted to this
# machine's Pacific clock (Ireland is a constant 8h ahead of Pacific, summer
# and winter alike, since both regions shift DST by the same amount):
#
#   Ireland 07:00 (morning catch-up, before European open)  -> Pacific 23:00 (previous day)
#   Ireland 16:30 (European bourses close)                  -> Pacific 08:30
#   Ireland 21:00 (US market close, 16:00 ET + 5h)           -> Pacific 13:00
#
# Runs every day (not just weekdays): geopolitical events don't wait for
# market hours, and this gives a weekend catch-up before Monday's open.
#
# Run from PowerShell as administrator:
#     powershell -ExecutionPolicy Bypass -File .\setup_scheduler.ps1
#
# To remove the tasks:
#     powershell -ExecutionPolicy Bypass -File .\setup_scheduler.ps1 -Remove
# ============================================================================
param(
    [switch]$Remove,
    [string]$Root = "",
    [string]$Python = "C:\ProgramData\spyder-6\python.exe"
)

$ErrorActionPreference = "Stop"
if (!$Root) {
    $Root = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$wrapper = Join-Path $Root "run_news_crawl_with_telegram.ps1"
$logDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$taskNames = @("LazyCrawler_News_Morning", "LazyCrawler_News_EuropeClose", "LazyCrawler_News_USClose")

if ($Remove) {
    foreach ($name in $taskNames) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "Removed task $name"
        }
    }
    Write-Host "Done."
    return
}

function New-NewsTask($name, $time, $description) {
    $logFile = Join-Path $logDir "$name.log"
    # -Command (not -File): Task Scheduler invokes powershell.exe directly, and
    # -File would pass "*>>" through as an inert literal argument instead of
    # redirecting output (same reasoning as the other repos' setup_scheduler.ps1).
    $cmdString = "& '$wrapper' *>> '$logFile'"
    $psArgs = "-NoProfile -ExecutionPolicy Bypass -Command `"$cmdString`""
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $psArgs
    $trigger = New-ScheduledTaskTrigger -Daily -At $time
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 3)
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
    }
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Settings $settings -Description $description | Out-Null
    Write-Host "Created task '$name' (daily $time Pacific) -> $(Split-Path -Leaf $wrapper)"
}

New-NewsTask "LazyCrawler_News_Morning" "23:00" `
    "LazyCrawler news-monitor: morning cycle (07:00 Ireland)"
New-NewsTask "LazyCrawler_News_EuropeClose" "08:30" `
    "LazyCrawler news-monitor: European market close cycle (16:30 Ireland)"
New-NewsTask "LazyCrawler_News_USClose" "13:00" `
    "LazyCrawler news-monitor: US market close cycle (21:00 Ireland)"

Write-Host ""
Write-Host "Tasks created. Verify with: Get-ScheduledTask -TaskName LazyCrawler_News*"
Write-Host "Logs in: $logDir"
