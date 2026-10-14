# Idempotent setup for the TCF Bot on a fresh Windows machine.
# Safe to re-run; never overwrites an existing config.yaml or .env.
#
# Run from PowerShell:
#   .\setup.ps1
#
# If you get "running scripts is disabled":
#   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

$ErrorActionPreference = "Stop"

$Script = $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Script
Set-Location $Root

function Step { param($msg) Write-Host "==> $msg" -ForegroundColor Blue }
function Ok   { param($msg) Write-Host "  ✓ $msg" -ForegroundColor Green }
function Warn { param($msg) Write-Host "  ! $msg" -ForegroundColor Yellow }
function Err  { param($msg) Write-Host "  ✗ $msg" -ForegroundColor Red }

# ----- 1. Check uv -----
Step "Checking for uv"
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Err "uv is not installed."
    Write-Host ""
    Write-Host "  Install it with:"
    Write-Host "    irm https://astral.sh/uv/install.ps1 | iex"
    Write-Host "    # or via winget:  winget install --id=astral-sh.uv -e"
    Write-Host ""
    Write-Host "  Then re-run .\setup.ps1"
    exit 1
}
$uvVersion = (& uv --version) -replace 'uv\s+',''
Ok "uv $uvVersion"

# ----- 2. Sync Python deps -----
Step "Installing Python dependencies"
& uv sync --quiet
Ok "Dependencies installed into .venv\"

# ----- 3. Install Playwright Chromium if missing -----
Step "Installing Playwright Chromium browser"
$pwCache = Join-Path $env:LOCALAPPDATA "ms-playwright"
if ((Test-Path $pwCache) -and ((Get-ChildItem $pwCache -Filter "chromium*" -ErrorAction SilentlyContinue).Count -gt 0)) {
    Ok "Chromium already installed at $pwCache"
} else {
    & uv run python -m playwright install chromium
    Ok "Chromium installed"
}

# ----- 4. Bootstrap config.yaml -----
Step "Bootstrapping config.yaml"
if (Test-Path "config.yaml") {
    Ok "config.yaml already exists — leaving it alone"
} else {
    Copy-Item "config.example.yaml" "config.yaml"
    Ok "Created config.yaml from config.example.yaml"
    Warn "Edit config.yaml with your real candidate details before running the bot"
}

# ----- 5. Bootstrap .env -----
Step "Bootstrapping .env"
if (Test-Path ".env") {
    Ok ".env already exists — leaving it alone"
} else {
    Copy-Item ".env.example" ".env"
    Ok "Created .env from .env.example"
    Warn "Fill in AF credentials, card details, Telegram token, SMTP creds in .env"
}

# Lock down .env so only you can read it (NTFS ACL).
try {
    $acl = Get-Acl ".env"
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        [System.Security.Principal.WindowsIdentity]::GetCurrent().Name,
        "FullControl", "Allow"
    )
    $acl.SetAccessRule($rule)
    Set-Acl ".env" $acl
    Ok ".env locked to current user only (NTFS ACL)"
} catch {
    Warn ".env perm lockdown skipped: $($_.Exception.Message)"
}

# ----- 6. Sanity-check imports + tests -----
Step "Smoke-testing the install"
try {
    & uv run python -c "import tcf_bot.booker, tcf_bot.notify, tcf_bot.scheduler, tcf_bot.scout" 2>$null
    Ok "All modules import"
} catch {
    Err "Module import failed"
    exit 1
}

$testOut = & uv run pytest -q --no-header 2>&1 | Select-Object -Last 1
if ($testOut -match "passed") {
    Ok "Unit tests pass"
} else {
    Warn "Some unit tests failed — investigate before relying on the bot"
}

# ----- 7. Next steps -----
Step "Setup complete"
Write-Host ""
Write-Host "Next steps:"
Write-Host ""
Write-Host "  1. Edit config.yaml — fill in your real candidate details:"
Write-Host "       notepad config.yaml"
Write-Host ""
Write-Host "  2. Edit .env — fill in your secrets:"
Write-Host "       notepad .env"
Write-Host ""
Write-Host "  3. Set up your Telegram bot (one-time, ~3 min):"
Write-Host "       a. In Telegram, find @BotFather, send /newbot, follow prompts."
Write-Host "       b. Paste the bot token into .env as TELEGRAM_BOT_TOKEN."
Write-Host "       c. Message your new bot once."
Write-Host "       d. Discover your chat_id:"
Write-Host "            uv run python -c `"import os,httpx;from dotenv import load_dotenv;load_dotenv();print(httpx.get(f'https://api.telegram.org/bot{os.environ[\``"TELEGRAM_BOT_TOKEN\``"]}/getUpdates').json())`""
Write-Host "       e. Paste the numeric `"id`" into config.yaml as notifications.telegram.chat_id"
Write-Host ""
Write-Host "  4. Smoke-test everything:"
Write-Host "       uv run tcf preflight"
Write-Host ""
Write-Host "  5. Verify scout sees upcoming exams:"
Write-Host "       uv run tcf scout"
Write-Host "       uv run tcf status"
Write-Host ""
Write-Host "  6. The night before the registration window, start the scheduler"
Write-Host "     in a foreground PowerShell window (keep it open through 12pm):"
Write-Host "       # Prevent Windows from sleeping for the next 12 hours:"
Write-Host "       powercfg /requestsoverride PROCESS python.exe DISPLAY SYSTEM"
Write-Host "       uv run tcf schedule"
Write-Host ""
Write-Host "  For background/durable operation, consider running it under Windows"
Write-Host "  Terminal in a tab you don't close, or schedule via Task Scheduler."
Write-Host ""
