"""Notifications: Telegram (send + receive) and Email.

Designed to be sync-friendly so the booker (which uses sync Playwright) can
block-and-wait for a Telegram reply during interactive form fill without
introducing asyncio. Uses Telegram Bot HTTP API directly via httpx — no SDK
required.
"""

from __future__ import annotations

import smtplib
import time
from email.message import EmailMessage
from pathlib import Path

import httpx
from rich.console import Console

from .config import Secrets, load_config, load_secrets

console = Console()

TELEGRAM_API = "https://api.telegram.org"
_HTTP_TIMEOUT = 15.0
_GETUPDATES_LONG_POLL = 30  # seconds


def _bot_url(secrets: Secrets, method: str) -> str:
    return f"{TELEGRAM_API}/bot{secrets.telegram_bot_token.get_secret_value()}/{method}"


# --------------------------------------------------------------------------- #
# Send
# --------------------------------------------------------------------------- #


def _telegram_send_message(text: str, *, secrets: Secrets, chat_id: str) -> None:
    try:
        r = httpx.post(
            _bot_url(secrets, "sendMessage"),
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=_HTTP_TIMEOUT,
        )
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Telegram send failed: {exc}[/yellow]")


def _telegram_send_photo(
    photo_path: Path, caption: str, *, secrets: Secrets, chat_id: str
) -> None:
    try:
        with photo_path.open("rb") as f:
            files = {"photo": (photo_path.name, f, "image/png")}
            r = httpx.post(
                _bot_url(secrets, "sendPhoto"),
                data={"chat_id": chat_id, "caption": caption[:1024]},
                files=files,
                timeout=_HTTP_TIMEOUT,
            )
            r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Telegram photo send failed: {exc}[/yellow]")


def _email_send(subject: str, body: str, *, secrets: Secrets, to_addr: str, from_addr: str) -> None:
    if not (secrets.smtp_host and secrets.smtp_user):
        return  # email not configured
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)
    try:
        with smtplib.SMTP(secrets.smtp_host, secrets.smtp_port, timeout=_HTTP_TIMEOUT) as smtp:
            smtp.starttls()
            smtp.login(secrets.smtp_user, secrets.smtp_pass.get_secret_value())
            smtp.send_message(msg)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Email send failed: {exc}[/yellow]")


def send_outcome(success: bool, details: dict, screenshot_path: Path | None = None) -> None:
    cfg = load_config()
    secrets = load_secrets()
    status = "✅ BOOKED" if success else "❌ FAILED"
    summary_lines = [f"<b>TCF Bot — {status}</b>"]
    for k, v in details.items():
        summary_lines.append(f"<b>{k}:</b> {v}")
    text = "\n".join(summary_lines)

    _telegram_send_message(text, secrets=secrets, chat_id=cfg.notifications.telegram.chat_id)
    if screenshot_path and screenshot_path.exists():
        _telegram_send_photo(
            screenshot_path,
            caption=status,
            secrets=secrets,
            chat_id=cfg.notifications.telegram.chat_id,
        )

    _email_send(
        subject=f"TCF Bot {status}",
        body="\n".join(f"{k}: {v}" for k, v in details.items()),
        secrets=secrets,
        to_addr=str(cfg.notifications.email.to),
        from_addr=cfg.notifications.email.from_,
    )


# --------------------------------------------------------------------------- #
# Receive (interactive fallback)
# --------------------------------------------------------------------------- #


def _get_latest_update_id(secrets: Secrets) -> int:
    """Snapshot the current Telegram update offset so we ignore stale messages."""
    try:
        r = httpx.get(_bot_url(secrets, "getUpdates"), params={"limit": 1}, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        results = r.json().get("result", [])
        if not results:
            return 0
        return int(results[-1]["update_id"])
    except Exception:
        return 0


def _wait_for_reply(
    *,
    secrets: Secrets,
    chat_id: str,
    after_update_id: int,
    timeout_seconds: int,
) -> str | None:
    deadline = time.time() + timeout_seconds
    offset = after_update_id + 1
    while time.time() < deadline:
        remaining = int(deadline - time.time())
        timeout = min(_GETUPDATES_LONG_POLL, max(1, remaining))
        try:
            r = httpx.get(
                _bot_url(secrets, "getUpdates"),
                params={"offset": offset, "timeout": timeout},
                timeout=timeout + 5,
            )
            r.raise_for_status()
            updates = r.json().get("result", [])
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]Telegram poll failed: {exc}[/yellow]")
            time.sleep(2)
            continue

        for upd in updates:
            offset = max(offset, int(upd["update_id"]) + 1)
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            if str(msg.get("chat", {}).get("id")) != str(chat_id):
                continue
            text = msg.get("text")
            if text:
                return text.strip()
    return None


def ask_for_field(
    field_name: str,
    label: str,
    options: list[str] | None = None,
    timeout_seconds: int = 120,
) -> str | None:
    """Prompt the user via Telegram for a value and block until they reply."""
    cfg = load_config()
    secrets = load_secrets()
    chat_id = cfg.notifications.telegram.chat_id

    prompt_lines = [
        "<b>TCF Bot — input needed</b>",
        f"<b>Field:</b> <code>{field_name}</code>",
    ]
    if label:
        prompt_lines.append(f"<b>Label:</b> {label}")
    if options:
        prompt_lines.append("<b>Options:</b>\n" + "\n".join(f"• {o}" for o in options[:25]))
    prompt_lines.append(f"<i>Reply within {timeout_seconds}s. Send the value as a plain message.</i>")
    prompt = "\n".join(prompt_lines)

    baseline = _get_latest_update_id(secrets)
    _telegram_send_message(prompt, secrets=secrets, chat_id=chat_id)

    reply = _wait_for_reply(
        secrets=secrets,
        chat_id=chat_id,
        after_update_id=baseline,
        timeout_seconds=timeout_seconds,
    )
    if reply is None:
        console.print(f"[yellow]No Telegram reply for field {field_name} within {timeout_seconds}s[/yellow]")
        return None

    # Persist the learned value back into config.candidate so future runs skip the prompt.
    try:
        cand_dict = cfg.candidate.model_dump()
        cand_dict[field_name] = reply
        # Re-validate via candidate model (it accepts extras).
        new_cfg = cfg.model_copy(update={"candidate": type(cfg.candidate).model_validate(cand_dict)})
        from .config import save_config

        save_config(new_cfg)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Could not persist learned field {field_name}: {exc}[/yellow]")

    return reply
