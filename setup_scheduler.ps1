# ============================================================================
# setup_scheduler.ps1 -- creates the 3 daily Windows scheduled tasks for the
# LazyCrawler news-monitor (crawl + digest + Telegram send). Morning sends
# both a Claude and a DeepSeek digest for comparison (--digest-engines
# claude,deepseek); EuropeClose/USClose send only the Claude digest.
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
# Two modes, declared as parameter sets rather than checked in the body:
# PowerShell binds mandatory parameters before any code runs, so a removal
# invocation would otherwise have to supply an interpreter it never uses and a
# source list it never reads -- prompting for them under a scheduler, where
# nobody is there to answer.
[CmdletBinding(DefaultParameterSetName = "Install")]
param(
    [Parameter(Mandatory, ParameterSetName = "Remove")] [switch]$Remove,
    # In both sets: where the wrappers live is the one thing removal and
    # installation agree on.
    [string]$Root = "",
    # Passed straight through to the wrappers, which now require it. Mandatory
    # for the same reason as the source list: which interpreter runs the crawl
    # decides which packages it runs against, and the default that used to sit
    # here named one machine's shared development install -- so every task this
    # script registered ran production against a checkout under active edit.
    [Parameter(Mandatory, ParameterSetName = "Install")] [string]$Python,
    # The crawl wrapper requires -SourcesConfig and this script had no way to
    # supply it, so every task it registered stopped at PowerShell parameter
    # binding before the crawl began -- noninteractively, with nothing in the
    # log to say why. The list itself is private and deliberately not in this
    # repository (see examples/news_sources.example.yaml for the shape), which
    # is exactly why it has to be passed in rather than defaulted.
    [Parameter(Mandatory, ParameterSetName = "Install")] [string]$SourcesConfig
)

$ErrorActionPreference = "Stop"
if (!$Root) {
    $Root = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$wrapper = Join-Path $Root "run_news_crawl_with_telegram.ps1"
$deltaWrapper = Join-Path $Root "run_digest_delta_with_telegram.ps1"
$logDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$taskNames = @("LazyCrawler_News_Morning", "LazyCrawler_News_EuropeClose", "LazyCrawler_News_USClose", "LazyCrawler_News_DeltaReport")

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

function New-NewsTask($name, $time, $description, $DigestEngines = "claude", $Cycle = "") {
    $logFile = Join-Path $logDir "$name.log"
    # -Command (not -File): Task Scheduler invokes powershell.exe directly, and
    # -File would pass "*>>" through as an inert literal argument instead of
    # redirecting output (same reasoning as the other repos' setup_scheduler.ps1).
    $cmdString = "& '$wrapper' -SourcesConfig '$SourcesConfig' -DigestEngines '$DigestEngines' -Cycle '$Cycle' -Python '$Python' *>> '$logFile'"
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
    "LazyCrawler news-monitor: morning cycle (07:00 Ireland)" -DigestEngines "claude,deepseek" -Cycle "morning"
New-NewsTask "LazyCrawler_News_EuropeClose" "08:30" `
    "LazyCrawler news-monitor: European market close cycle (16:30 Ireland)" -Cycle "europeclose"
New-NewsTask "LazyCrawler_News_USClose" "13:00" `
    "LazyCrawler news-monitor: US market close cycle (21:00 Ireland)" -Cycle "usclose"

# Delta report ("what's actually new") -- must run after the Morning task's
# digest lands in digests.db. Morning starts 23:00 Pacific and has taken
# 32-48 min end-to-end over the last 10 days (checked live), so 00:15
# Pacific (75 min after start) leaves a comfortable buffer -> 08:15 Ireland.
$deltaLogFile = Join-Path $logDir "LazyCrawler_News_DeltaReport.log"
# QueryCycle/BaselineCycle/BaselineCount are mandatory on that wrapper; the
# values are the ones the comment above describes -- the morning digest
# against the four most recent US closes. Without them the task stopped at
# parameter binding, noninteractively, before reaching Python.
$deltaCmdString = "& '$deltaWrapper' -QueryCycle 'morning' -BaselineCycle 'usclose' -BaselineCount 4 -Python '$Python' *>> '$deltaLogFile'"
$deltaPsArgs = "-NoProfile -ExecutionPolicy Bypass -Command `"$deltaCmdString`""
$deltaAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $deltaPsArgs
$deltaTrigger = New-ScheduledTaskTrigger -Daily -At "00:15"
$deltaSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 1)
if (Get-ScheduledTask -TaskName "LazyCrawler_News_DeltaReport" -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName "LazyCrawler_News_DeltaReport" -Confirm:$false
}
Register-ScheduledTask -TaskName "LazyCrawler_News_DeltaReport" -Action $deltaAction -Trigger $deltaTrigger `
    -Settings $deltaSettings -Description "LazyCrawler news delta: what's new in the morning digest vs the last 4 usclose digests (08:15 Ireland)" | Out-Null
Write-Host "Created task 'LazyCrawler_News_DeltaReport' (daily 00:15 Pacific / 08:15 Ireland) -> $(Split-Path -Leaf $deltaWrapper)"

Write-Host ""
Write-Host "Tasks created. Verify with: Get-ScheduledTask -TaskName LazyCrawler_News*"
Write-Host "Logs in: $logDir"
