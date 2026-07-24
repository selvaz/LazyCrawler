# ============================================================================
# setup_first_run.ps1 - interactive first-run bootstrap for the LazyCrawler
# news-monitor pipeline (run_news_crawl.py / make_news_report.py /
# send_telegram_news_report.py + the 3 daily scheduled tasks).
#
# Run from PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\setup_first_run.ps1
#
# Idempotent: safe to re-run after pulling the repo on a new machine, or
# after rotating a key -- existing env vars are offered as defaults, not
# silently overwritten.
# ============================================================================
param(
    [string]$Python = "C:\ProgramData\spyder-6\python.exe",
    [switch]$SkipInstall,
    [switch]$SkipTests,
    [switch]$SkipSmokeTest,
    [switch]$ConfigureScheduler
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Read-OptionalSecret($Prompt, $ExistingLabel) {
    $current = [Environment]::GetEnvironmentVariable($ExistingLabel, "User")
    if ($current) {
        $answer = Read-Host "$Prompt already set. Press Enter to keep it, or paste a new value"
    } else {
        $answer = Read-Host "$Prompt (press Enter to skip)"
    }
    if ($answer) {
        [Environment]::SetEnvironmentVariable($ExistingLabel, $answer, "User")
        Set-Item -Path "Env:$ExistingLabel" -Value $answer
        Write-Host "Set $ExistingLabel for current user."
    } elseif ($current) {
        Set-Item -Path "Env:$ExistingLabel" -Value $current
        Write-Host "Keeping existing $ExistingLabel."
    } else {
        Write-Host "Skipping $ExistingLabel."
    }
}

Write-Host ""
Write-Host "LazyCrawler news-monitor first-run setup"
Write-Host "Repo: $Root"
Write-Host ""

if (!(Test-Path $Python)) {
    $found = Get-Command python -ErrorAction SilentlyContinue
    if ($found) {
        Write-Warning "Configured Python not found: $Python"
        $Python = $found.Source
        Write-Host "Using Python on PATH: $Python"
    } else {
        throw "Python not found. Install Python 3.11+ (LazyBridge/smart-mode requires it) or pass -Python C:\path\python.exe"
    }
}

Read-OptionalSecret "DeepSeek API key (smart-mode local-language sources + index summaries + digest)" "DEEPSEEK_API_KEY"
Read-OptionalSecret "Telegram bot token" "TELEGRAM_BOT_TOKEN"
Read-OptionalSecret "Telegram chat id / @channel" "TELEGRAM_CHAT_ID"

if (!$SkipInstall) {
    Write-Host ""
    Write-Host "Installing/updating LazyCrawler + extras (smart, ml, nlp, news)..."
    & $Python -m ensurepip --upgrade
    & $Python -m pip install -e ".[smart,ml,nlp,news,dev]"

    Write-Host "Downloading spaCy English model (ml-mode named-entity recognition)..."
    & $Python -m spacy download en_core_web_sm

    # LazyBridge is on PyPI; LazyTools/lazytoolkit is not (git-installed).
    # Prefer an editable install from a sibling checkout if one exists (this
    # workstation's normal layout), otherwise install straight from PyPI/git
    # so a genuinely fresh machine -- clone LazyCrawler only, run this
    # script -- still ends up fully working.
    $lazyBridgeSibling = Join-Path (Split-Path $Root -Parent) "LazyBridge"
    if (Test-Path $lazyBridgeSibling) {
        Write-Host "Installing LazyBridge from local sibling repo..."
        & $Python -m pip install -e "$lazyBridgeSibling"
    } else {
        Write-Host "Installing LazyBridge from PyPI..."
        & $Python -m pip install "lazybridge>=0.9"
    }

    $lazyToolsSibling = Join-Path (Split-Path $Root -Parent) "LazyTools"
    if (Test-Path $lazyToolsSibling) {
        Write-Host "Installing LazyTools[telegram] from local sibling repo..."
        & $Python -m pip install -e "$lazyToolsSibling[telegram]"
    } else {
        Write-Host "Installing LazyTools[telegram] from GitHub..."
        & $Python -m pip install "lazytoolkit[telegram] @ git+https://github.com/selvaz/LazyTools.git"
    }
}

Write-Host ""
Write-Host "Verifying dependencies..."
& $Python -c @"
import importlib
mods = ['spacy', 'yake', 'vaderSentiment', 'model2vec', 'feedparser', 'lazybridge', 'lazytools', 'trafilatura']
missing = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as e:
        missing.append(f'{m} ({e})')
if missing:
    raise SystemExit('Missing dependencies: ' + ', '.join(missing))
import spacy
spacy.load('en_core_web_sm')
print('All dependencies OK.')
"@

if (!$SkipTests) {
    Write-Host ""
    Write-Host "Running test suite (unit tests only, no network/API key needed)..."
    & $Python -m pytest -q
}

if (!$SkipSmokeTest) {
    $token = [Environment]::GetEnvironmentVariable("DEEPSEEK_API_KEY", "User")
    if ($token) {
        Write-Host ""
        Write-Host "Running a small end-to-end smoke test (2 sources, no Telegram send)..."
        & $Python (Join-Path $Root "run_news_crawl.py") --sources "BBC,Clarin - Economia" --ml-max-items 2 --smart-max-items 2 --session-id smoke_test
        & $Python (Join-Path $Root "make_news_report.py") --session-id smoke_test
        Write-Host "Smoke test reports written to reports\news\*smoke_test*.md -- inspect them, then delete."
    } else {
        Write-Host ""
        Write-Warning "Skipping smoke test: no DEEPSEEK_API_KEY set."
    }
}

if ($ConfigureScheduler) {
    Write-Host ""
    Write-Host "Creating/updating the 3 daily scheduled tasks..."
    powershell -ExecutionPolicy Bypass -File (Join-Path $Root "setup_scheduler.ps1") -Root $Root -Python $Python
}

Write-Host ""
Write-Host "First-run setup complete."
Write-Host "Open a new PowerShell session to inherit saved user environment variables."
Write-Host "Re-run with -ConfigureScheduler to register the 3 daily tasks (see setup_scheduler.ps1 for the times/timezone math)."
