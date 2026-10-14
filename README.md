# TCF BOT

Personal bot that books a TCF Canada exam slot at Alliance Française Vancouver the
moment the registration window opens.

It logs in to your AF account, watches the page, clicks **Begin Registration** within
milliseconds, fills the form, and submits payment — all before a human can refresh the
page. The 10-minute candidate hold means we only need to win the first click; form fill
and payment happen with seconds to spare.

## What you need before running

- Your existing **AF account** (username + password).
- Your **credit card** details (will be auto-submitted at checkout).
- A **Telegram account** for status pings + interactive form-fill (5 min setup).
- Optionally, an **SMTP email** account for durable receipts (Gmail app password works).

## One-time setup (5 min)

Easiest path — the setup script handles dependencies, Playwright, the
example-file copies, and a smoke test. Safe to re-run; never overwrites your
filled-in `config.yaml` or `.env`.

```bash
cd "/path/to/TCF BOT"
./setup.sh
```

It will tell you exactly what to fill in next. If you'd rather do it by hand:

```bash
uv sync
uv run python -m playwright install chromium
cp config.example.yaml config.yaml
cp .env.example .env
chmod 600 .env
$EDITOR config.yaml
$EDITOR .env
```

> Prerequisite: `uv` (Python project manager). Install with
> `curl -LsSf https://astral.sh/uv/install.sh | sh` or `brew install uv`.
> The script checks for it and bails with instructions if missing.

### On Windows? Use `setup.ps1` instead

In PowerShell, from the repo root:

```powershell
.\setup.ps1
```

If you get "running scripts is disabled", first run:
`Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`.

The PowerShell script does the equivalent of `setup.sh`:
- Checks for `uv` (install with `irm https://astral.sh/uv/install.ps1 | iex`)
- `uv sync` + `playwright install chromium`
- Copies `config.example.yaml` → `config.yaml` and `.env.example` → `.env` (only
  if they don't already exist)
- Locks `.env` to your user account via NTFS ACL (the Windows equivalent of
  `chmod 600`)
- Runs the smoke test
- Prints Windows-specific next steps

**Where Windows differs from macOS at runtime:**

| Thing | macOS | Windows |
|---|---|---|
| Edit config files | `open config.yaml` | `notepad config.yaml` |
| Keep machine awake during strike | `caffeinate -i uv run tcf schedule` | `powercfg /requestsoverride PROCESS python.exe DISPLAY SYSTEM` then `uv run tcf schedule` |
| Long-lived session | `tmux new -s tcf` | Windows Terminal (keep tab open) or Task Scheduler |
| Find Playwright browser window | Cmd-Tab → Chromium | Alt-Tab → "Chromium" |
| Telegram chat_id lookup one-liner | bash quoting | PowerShell quoting (uses `` ` `` for escapes — see `setup.ps1` output) |

The Python code itself is platform-agnostic. The bot has been tested running on macOS;
core paths (Playwright launch, autocomplete typing, signal handling, time formatting)
have been verified to work on Windows but the full strike has only been exercised on
macOS so far.

### Telegram bot setup (one-time, 3 min)

1. Open Telegram → search for **@BotFather** → `/newbot` → follow prompts.
2. Copy the bot **token** → paste into `.env` as `TELEGRAM_BOT_TOKEN`.
3. Open your new bot in Telegram and send it any message (e.g. "hi").
4. Get your **chat_id**:
   ```bash
   uv run python -c "
   import os, httpx
   from dotenv import load_dotenv; load_dotenv()
   r = httpx.get(f'https://api.telegram.org/bot{os.environ[\"TELEGRAM_BOT_TOKEN\"]}/getUpdates')
   print(r.json())
   "
   ```
   Find `\"chat\": {\"id\": NNNNN}` → paste `NNNNN` into `config.yaml` under
   `notifications.telegram.chat_id`.

## Verify it's working

```bash
# Tests pass against saved AF HTML fixtures.
uv run pytest -v

# Live scout against AF Canada — should print upcoming exams.
uv run tcf scout

# Table view of what's known.
uv run tcf status

# Optional: page-load latency from your network.
uv run tcf benchmark
```

## The actual booking (the only thing that matters)

You have two options.

### Option A — fully scheduled (recommended)

```bash
# Run this BEFORE the registration window. Keep terminal open.
caffeinate -i uv run tcf schedule
```

The scheduler:
- Re-scouts every 15 min, so newly announced windows get picked up.
- Schedules a Strike for `(opens_at − 90 s)` for each future window.
- Telegram-pings you when a window is < 24 h away.
- Blocks; Ctrl-C to stop. `caffeinate -i` keeps your Mac awake.

When the strike fires, it logs into AF, lands on the TCF page 90 s early, polls
the DOM, clicks the moment the Begin Registration link appears, fills the form,
and pays. You'll get a Telegram + Email confirmation (with screenshot).

### Option B — one-shot fire (e.g. if you know the time and want manual control)

```bash
# Run within seconds of the window opening, or in a `sleep`+command chain.
caffeinate -i uv run tcf strike --earliest

# Or against a known exam_id (after the window has opened).
caffeinate -i uv run tcf strike --exam-id 42
```

### Dry-run

Before the real window, test the full flow end-to-end against the AF San Diego
sister site (same platform, no real exam at stake). The dry-run aborts before the
final "Pay" click — nothing is charged.

```bash
uv run tcf dry-run --site sandiego
```

The first dry-run also dumps every form field it encounters to
`state/runs/<timestamp>-dry-sandiego-<id>/discovered_fields.json`. Open that file
and add any fields you didn't anticipate to `config.yaml` under `candidate:`.

## Choosing an exam site

AF Vancouver now runs the TCF at two sites — **Vancouver** (6161 Cambie St.)
and **New Westminster** (320 Columbia St.). Both list the same exam dates as
separate rows, and each site opens registration on its own day.

Set your preference in `config.yaml`:

```yaml
date_strategy:
  locations: [new_westminster, vancouver]   # either site, New Westminster first
  target_dates:
    - "December 2, 2026"                    # either site, per the list above
    - date: "December 23, 2026"
      locations: [vancouver]                # this date: Vancouver only
```

- `locations: []` (the default) means either site.
- With both sites allowed, the bot races for your first choice and falls back
  to the other site for the same date if the first sells out.
- Successful bookings are recorded in `state/booked.json`, so a later release
  (the other site's) won't book a date you already have. Delete that file to
  start over. `tcf status` shows each exam's site and what's been booked.

## Pre-strike runbook (30 min before window opens)

1. `uv run tcf scout` — confirm the exam still shows in the list.
2. `uv run tcf status` — confirm `opens_at` is what you expect.
3. Open Telegram, confirm you can receive a message from the bot:
   ```bash
   uv run python -c "
   from tcf_bot.notify import send_outcome
   send_outcome(True, {'event': 'pre-flight check'})
   "
   ```
4. If you ran `tcf schedule` already, leave it running. Otherwise start it now in tmux:
   ```bash
   tmux new -s tcf
   caffeinate -i uv run tcf schedule
   # Detach with Ctrl-b d
   ```
5. Plug your laptop in. Disable Wi-Fi power-saving. Close bandwidth-heavy apps.

## During the strike

You'll see Rich-formatted log lines in the terminal. The Telegram bot will message
you the moment it succeeds or fails. If it asks for an unknown form field, **reply
in Telegram within 120 s** — the bot blocks waiting for your answer, then continues.

If a **3-D Secure** prompt is required, the bot sends you the URL on Telegram —
open it in your phone browser, complete the SMS code, and the booking lands.

## After the strike

- `state/runs/<timestamp>-*/` has the full log, screenshots at every step, and the
  set of fields the form actually had.
- `tcf status` shows the latest scout state.
- Any field you answered via Telegram has been auto-saved into `config.yaml` for
  next time.

## Troubleshooting

- **Login indicator missing**: the AF login form selectors may have shifted.
  Open `src/tcf_bot/selectors.py` and tune `login_*`. Re-run dry-run.
- **No exam_id discovered**: the JS hasn't swapped the countdown to a link yet,
  or the window already closed. The booker reloads ~1×/s; if 10 min pass with
  no link, the row is given up on.
- **Telegram not delivering**: confirm chat_id by re-running the "send any message
  to your bot then /getUpdates" check in setup.
- **`tcf schedule` died overnight**: macOS may have killed it. Use
  `caffeinate -i` (the runbook does) and run inside tmux so a crashed terminal
  doesn't take the process down.

## What's NOT built (and why)

By design — the original PRD described a multi-user SaaS, but this is a
personal-use tool:

- No user accounts, web UI, NPS, or analytics.
- No cloud hosting (your Mac is fine; the runbook keeps it awake).
- No slot cancellation flow (AF bookings are non-refundable per their policy).
- Scout polls every 15 min — once we know `opens_at`, we schedule, we don't
  blind-poll.

## Sharing this with a friend

The whole repo is reusable as-is. A friend can:

1. Clone the repo.
2. Run the setup commands above.
3. Use their own `config.yaml` + `.env` (everyone runs their own copy; no
   shared backend, no auth).

## Architectural notes

Read `src/tcf_bot/{scout,booker,scheduler,notify}.py`. The selectors live in
one file (`selectors.py`) so AF DOM changes are a one-place fix. Both AF Canada
and AF San Diego are on the same Oncord platform — that's why the same parser
handles both.

## Use responsibly

This is a personal tool for booking your own exam. AF Vancouver warns that
opening multiple tabs or repeatedly refreshing may cancel a reservation; the
bot is built around that (one browser, one tab, and a race loop that polls the
public listing rather than refreshing your session). Run one instance per
person, and only for your own registration.
