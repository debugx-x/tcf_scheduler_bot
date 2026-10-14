#!/usr/bin/env bash
# Idempotent setup for the TCF Bot on a fresh machine.
# Safe to re-run; never overwrites an existing config.yaml or .env.

set -euo pipefail

# Resolve the script directory so this works regardless of where it's invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Pretty output helpers.
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

step()  { printf "${BLUE}${BOLD}==>${NC} ${BOLD}%s${NC}\n" "$1"; }
ok()    { printf "  ${GREEN}✓${NC} %s\n" "$1"; }
warn()  { printf "  ${YELLOW}!${NC} %s\n" "$1"; }
err()   { printf "  ${RED}✗${NC} %s\n" "$1"; }

# ----- 1. Check uv -----
step "Checking for uv"
if ! command -v uv >/dev/null 2>&1; then
  err "uv is not installed."
  echo
  echo "  Install it with one of:"
  echo "    curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "    brew install uv"
  echo
  echo "  Then re-run ./setup.sh"
  exit 1
fi
UV_VERSION="$(uv --version | awk '{print $2}')"
ok "uv $UV_VERSION"

# ----- 2. Sync Python deps (uv manages 3.11 automatically) -----
step "Installing Python dependencies"
uv sync --quiet
ok "Dependencies installed into .venv/"

# ----- 3. Install Playwright Chromium (no-op if this revision is present) -----
step "Installing Playwright Chromium browser"
uv run python -m playwright install chromium
ok "Chromium ready"

# ----- 4. Bootstrap config.yaml (no overwrite) -----
step "Bootstrapping config.yaml"
if [[ -f config.yaml ]]; then
  ok "config.yaml already exists — leaving it alone"
else
  cp config.example.yaml config.yaml
  ok "Created config.yaml from config.example.yaml"
  warn "Edit config.yaml with your real candidate details before running the bot"
fi

# ----- 5. Bootstrap .env (no overwrite) and lock down perms -----
step "Bootstrapping .env"
if [[ -f .env ]]; then
  ok ".env already exists — leaving it alone"
else
  cp .env.example .env
  ok "Created .env from .env.example"
  warn "Fill in AF credentials, card details, Telegram token, SMTP creds in .env"
fi
chmod 600 .env
ok ".env perms locked to 600 (owner read/write only)"

# ----- 6. Sanity-check imports + run unit tests -----
step "Smoke-testing the install"
if uv run python -c "import tcf_bot.booker, tcf_bot.notify, tcf_bot.scheduler, tcf_bot.scout" 2>/dev/null; then
  ok "All modules import"
else
  err "Module import failed — see error above"
  exit 1
fi

if uv run pytest -q --no-header 2>&1 | tail -1 | grep -q "passed"; then
  ok "Unit tests pass"
else
  warn "Some unit tests failed — investigate before relying on the bot"
fi

# ----- 7. Print next steps -----
step "Setup complete"
cat <<'EOF'

Next steps:

  1. Edit config.yaml — fill in your real candidate details:
       open config.yaml

  2. Edit .env — fill in your secrets (AF login, card, Telegram, SMTP):
       open .env

  3. Set up your Telegram bot (one-time, ~3 min):
       a. Open Telegram, find @BotFather, send /newbot, follow prompts.
       b. Paste the returned bot token into .env as TELEGRAM_BOT_TOKEN.
       c. Message your new bot once (any text).
       d. Run this to discover your chat_id:
            uv run python -c "import os,httpx;from dotenv import load_dotenv;load_dotenv();\
              print(httpx.get(f'https://api.telegram.org/bot{os.environ[\"TELEGRAM_BOT_TOKEN\"]}/getUpdates').json())"
       e. Find "chat":{"id":NNNN} → paste NNNN into config.yaml under
          notifications.telegram.chat_id

  4. Verify the live scout finds upcoming AF Canada exams:
       uv run tcf scout
       uv run tcf status

  5. Run a dry-run against AF San Diego (no real charge):
       uv run tcf dry-run --site sandiego
     This walks the full SD flow and stops before the final submit.
     The first dry-run also dumps every form field it sees to
     state/runs/.../discovered_fields.json.

  6. The night before the registration window opens, start the scheduler
     in tmux so it survives a closed terminal:
       tmux new -s tcf
       caffeinate -i uv run tcf schedule
       # Detach with Ctrl-b d; re-attach with: tmux attach -t tcf

  See README.md for the full pre-strike runbook.

EOF
