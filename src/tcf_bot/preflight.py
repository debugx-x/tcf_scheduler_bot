"""`tcf preflight` — fast (<30s) smoke test that exercises every code path
that could regress between strikes. Built after the 2026-06-15 postmortem,
where a one-line AttributeError (`SELECTORS.generic_card_number`) crashed
the strike one second after winning the race.

Checks (in order, stop on first failure):
  1. All modules import.
  2. Every SELECTORS.* attribute referenced in booker.py actually exists.
  3. config.yaml validates.
  4. .env loads — every required secret present.
  5. Scout fetches AF Canada and parses ≥1 exam.
  6. Playwright Chromium launches and reaches a known URL.
  7. Telegram bot token + chat_id work (sends a one-line ping).
  8. SMTP creds connect (without sending anything if --quiet).
"""

from __future__ import annotations

import re
import time

from playwright.sync_api import sync_playwright
from rich.console import Console

from .config import REPO_ROOT, load_config, load_secrets

console = Console()


def _check(label: str, fn) -> bool:
    """Run a check, print OK/FAIL with timing. Return True if passed."""
    t0 = time.time()
    try:
        fn()
        dt = (time.time() - t0) * 1000
        console.print(f"  [green]✓[/green] {label}  [dim]({dt:.0f} ms)[/dim]")
        return True
    except Exception as exc:  # noqa: BLE001
        dt = (time.time() - t0) * 1000
        console.print(f"  [red]✗ {label}  ({dt:.0f} ms)\n     {exc}[/red]")
        return False


def _check_imports() -> None:
    # Import every public module — catches syntax errors + top-level NameErrors.
    import tcf_bot.booker  # noqa: F401
    import tcf_bot.notify  # noqa: F401
    import tcf_bot.scheduler  # noqa: F401
    import tcf_bot.scout  # noqa: F401
    import tcf_bot.selectors  # noqa: F401


def _check_selector_references() -> None:
    """Scan booker.py for every `SELECTORS.X` reference and verify X exists.

    This is the test that would have caught BUG 1 from 2026-06-15.
    """
    from tcf_bot.selectors import SELECTORS

    booker_src = (REPO_ROOT / "src" / "tcf_bot" / "booker.py").read_text()
    referenced = set(re.findall(r"SELECTORS\.([A-Za-z_]+)", booker_src))
    missing = sorted(name for name in referenced if not hasattr(SELECTORS, name))
    if missing:
        raise AssertionError(
            f"booker.py references {len(missing)} selector(s) that don't exist on "
            f"AFSelectors: {missing}. This is exactly the BUG 1 class of regression."
        )


def _check_config() -> None:
    cfg = load_config()
    if not cfg.candidate.first_name or not cfg.candidate.last_name:
        raise AssertionError("candidate.first_name/last_name still placeholders?")
    if cfg.notifications.telegram.chat_id in ("", "123456789"):
        raise AssertionError("Telegram chat_id is placeholder — bot can't notify you")


def _check_secrets() -> None:
    s = load_secrets()
    for name, value in {
        "AF_USERNAME": s.af_username,
        "AF_PASSWORD": s.af_password.get_secret_value(),
        "CARD_NUMBER": s.card_number.get_secret_value(),
        "CARD_CVV": s.card_cvv.get_secret_value(),
        "CARD_EXPIRY": s.card_expiry,
        "CARD_HOLDER": s.card_holder,
        "TELEGRAM_BOT_TOKEN": s.telegram_bot_token.get_secret_value(),
    }.items():
        if not value:
            raise AssertionError(f"{name} is empty in .env")


def _check_scout() -> None:
    from tcf_bot.scout import run_once

    exams = run_once()
    if not exams:
        raise AssertionError("Scout found 0 exams — selector drift on AF Canada?")


def _check_playwright() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto("https://example.com", wait_until="domcontentloaded", timeout=10_000)
            if "Example" not in page.title():
                raise AssertionError("Playwright loaded but page title unexpected")
            ctx.close()
        finally:
            browser.close()


def _check_af_login() -> None:
    """Drive the booker's real _login flow against AF Canada. Catches the BUG 3
    class of regression (login selectors / submission strategy stale after
    Oncord redesigns). Uses the SAME code path as the strike so a pass here
    means the strike's login will pass too.

    On failure, full diagnostics are dumped to state/preflight/<timestamp>/.
    """
    import json as _json

    from .booker import RunDirs, USER_AGENT, _login, _site_urls
    from .config import TargetSite
    from .selectors import SELECTORS

    cfg = load_config()
    secrets = load_secrets()
    urls = _site_urls(cfg, TargetSite.AF_CANADA)
    runs = RunDirs.create("login-check", base_dir=REPO_ROOT / "state" / "preflight")

    # Also capture network POSTs for the same diagnostic context we had before.
    network_posts: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(
                user_agent=USER_AGENT,
                locale="en-CA",
                timezone_id="America/Vancouver",
            )
            page = ctx.new_page()

            def _on_response(resp):
                if resp.request.method in ("POST", "PUT") and "alliancefrancaise" in resp.url:
                    network_posts.append(
                        {"url": resp.url, "method": resp.request.method, "status": resp.status}
                    )

            page.on("response", _on_response)

            ok = _login(page, urls, secrets, runs)
            final_url = page.url
            (runs.root / "login_diagnostic.json").write_text(
                _json.dumps(
                    {
                        "ok": ok,
                        "final_url": final_url,
                        "network_posts": network_posts,
                        "success_indicator_count": page.locator(
                            SELECTORS.login_success_indicator
                        ).count(),
                        "still_on_login_page": SELECTORS.login_path in final_url,
                    },
                    indent=2,
                )
            )
            ctx.close()
        finally:
            browser.close()

    if not ok:
        raise AssertionError(
            f"booker._login() returned False. Diagnostics in {runs.root}\n"
            f"  - run.log (every submission strategy attempted)\n"
            f"  - 01-login.png / 02-after-login.png\n"
            f"  - login_diagnostic.json (network POSTs, final URL)\n"
            f"  - login_page_inputs.json (dumped automatically on fail)"
        )


def _check_telegram() -> None:
    """Send a one-line ping to confirm the bot can talk to you."""
    from tcf_bot.notify import _bot_url, _telegram_send_message

    cfg = load_config()
    secrets = load_secrets()
    # Quick reachability check first.
    import httpx

    r = httpx.get(_bot_url(secrets, "getMe"), timeout=10)
    r.raise_for_status()
    if not r.json().get("ok"):
        raise AssertionError(f"Telegram getMe failed: {r.text}")
    # Send actual ping.
    _telegram_send_message(
        "✅ TCF preflight: bot can reach your Telegram.",
        secrets=secrets,
        chat_id=cfg.notifications.telegram.chat_id,
    )


def _check_smtp() -> None:
    """Connect+auth to SMTP without sending anything."""
    import smtplib

    secrets = load_secrets()
    with smtplib.SMTP(secrets.smtp_host, secrets.smtp_port, timeout=10) as smtp:
        smtp.starttls()
        smtp.login(secrets.smtp_user, secrets.smtp_pass.get_secret_value())
        smtp.noop()


def run() -> int:
    """Run all preflight checks. Returns exit code (0 = all pass, 1 = any fail)."""
    console.print("[bold]TCF preflight[/bold] — fast smoke test")
    console.print()
    started = time.time()

    checks = [
        ("modules import cleanly", _check_imports),
        ("every SELECTORS.* in booker.py exists  [dim](would catch BUG 1)[/dim]", _check_selector_references),
        ("config.yaml valid + filled in", _check_config),
        (".env loaded, all required secrets present", _check_secrets),
        ("scout fetches AF Canada + parses exams", _check_scout),
        ("Playwright Chromium launches", _check_playwright),
        ("AF Canada login succeeds  [dim](would catch BUG 3)[/dim]", _check_af_login),
        ("Telegram bot reachable + sends ping", _check_telegram),
        ("SMTP creds connect + authenticate", _check_smtp),
    ]
    failures = 0
    for label, fn in checks:
        if not _check(label, fn):
            failures += 1

    total_ms = (time.time() - started) * 1000
    console.print()
    if failures:
        console.print(f"[bold red]{failures} of {len(checks)} checks failed.[/bold red]  [dim]({total_ms:.0f} ms)[/dim]")
        return 1
    console.print(f"[bold green]All {len(checks)} checks passed.[/bold green]  [dim]({total_ms:.0f} ms)[/dim]")
    return 0
