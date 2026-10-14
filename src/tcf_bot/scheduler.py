"""APScheduler wiring + a simple wait-and-strike for one-shot use.

`tcf schedule` runs a blocking scheduler that:
  1. Refreshes Scout every cfg.scout.poll_interval_minutes.
  2. For each newly discovered upcoming registration window, schedules a Strike
     for (opens_at - cfg.strike.prewarm_seconds).
  3. Sends Telegram a heads-up when a window is < notify_when_opens_within_hours away.

This is intentionally simple: one process, blocking. If the process dies, the
strike is lost — so the README's runbook recommends running under `caffeinate`
inside a tmux/screen session for the actual window day.

A PID-file lock guarantees only ONE scheduler runs at a time — the 2026-06-15
postmortem traced a double-strike to two `tcf schedule` instances running in
parallel after a tmux re-attach.
"""

from __future__ import annotations

import atexit
import os
import signal
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Set

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from rich.console import Console
from rich.table import Table

from .config import load_config
from .scout import (
    Exam,
    in_target_window,
    load_bookings,
    load_state,
    location_rank,
    parse_exam_date,
    run_once,
)

console = Console()

_scheduled_keys: Set[str] = set()

# System-wide lock — placing this in $HOME means a second tcf schedule
# from ANY repo clone on the same machine sees the same lock. The
# previous per-repo `state/scheduler.pid` was bypassed by the 2026-06-17
# postmortem because the user had two repos cloned.
LOCK_PATH = Path.home() / ".tcf-scheduler.lock"


def _strike_job(date: str) -> None:
    from .booker import strike_earliest

    console.print(f"[bold green]→ Firing strike for {date}[/bold green]")
    try:
        strike_earliest(dry_run=False)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Strike crashed: {exc}[/red]")


def _heads_up(e: Exam) -> None:
    try:
        from .notify import send_outcome

        send_outcome(
            success=True,  # not a true outcome, just an info ping
            details={
                "event": "Heads-up",
                "exam": e.date,
                "opens_at": str(e.opens_at),
                "price": e.price,
            },
            screenshot_path=None,
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Heads-up notify failed: {exc}[/yellow]")


def _refresh_and_schedule(sched: BackgroundScheduler) -> None:
    cfg = load_config()
    try:
        exams = run_once()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Scout fetch failed: {exc}[/red]")
        return

    now = datetime.now()
    notify_threshold = now + timedelta(hours=cfg.scout.notify_when_opens_within_hours)

    bookings = load_bookings()
    booked_targets = {b.get("target") for b in bookings}

    def _is_wanted(e: Exam) -> bool:
        # target_dates takes precedence; else the min/max window. In
        # target_dates mode, treat a date as relevant if it's an exact target
        # OR within substitute_tolerance_days of one, at a site allowed for
        # that target — so the strike still gets scheduled when AF publishes
        # slightly-shifted dates and the booker substitutes at fire time.
        # Over-including here is harmless: the booker does the precise slot
        # assignment. Targets already in state/booked.json are ignored.
        if e.sold_out:
            return False
        ds = cfg.date_strategy
        if ds.target_dates:
            ed = parse_exam_date(e.date)
            if ed is None:
                return False
            tol = max(0, ds.substitute_tolerance_days)
            for raw, allowed in ds.target_specs():
                if raw in booked_targets:
                    continue
                wanted = parse_exam_date(raw)
                if (
                    wanted is not None
                    and abs((ed - wanted).days) <= tol
                    and location_rank(e, allowed) is not None
                ):
                    return True
            return False
        if bookings:  # earliest mode books a single exam
            return False
        return in_target_window(e, ds) and location_rank(e, ds.locations) is not None

    # ONE strike job per opens_at timestamp — not per exam. Two dates that
    # release at the same moment must be handled by a single sequential
    # strike (strike_earliest iterates all targets). Two parallel strike
    # jobs = two browsers = the "multiple tabs" behavior AF explicitly warns
    # cancels reservations, and the root cause of the 2026-06-15 cascade.
    by_window: dict[datetime, list[Exam]] = {}
    for e in exams:
        if not e.opens_at or e.opens_at <= now:
            continue
        if not _is_wanted(e):
            continue
        by_window.setdefault(e.opens_at, []).append(e)

    for opens_at, group in sorted(by_window.items()):
        key = opens_at.isoformat()
        if key in _scheduled_keys:
            continue
        prewarm = timedelta(seconds=cfg.strike.prewarm_seconds)
        run_at = opens_at - prewarm
        if run_at <= now:
            run_at = now + timedelta(seconds=5)
        dates = ", ".join(e.label for e in group)
        sched.add_job(
            _strike_job,
            trigger=DateTrigger(run_date=run_at),
            args=[dates],
            id=f"strike-{key}",
            replace_existing=True,
        )
        _scheduled_keys.add(key)
        console.print(
            f"[cyan]Scheduled ONE strike at {run_at.isoformat()} covering: {dates} "
            f"(opens_at={opens_at})[/cyan]"
        )
        for e in group:
            if e.opens_at <= notify_threshold:
                _heads_up(e)


def _process_alive(pid: int) -> bool:
    """Return True if the given PID is still running."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't own it — treat as alive (refuse to start).
        return True
    return True


def _acquire_lock() -> None:
    """Refuse to start if another `tcf schedule` is running ANYWHERE on this
    machine. System-wide lock at ~/.tcf-scheduler.lock plus atomic
    `O_CREAT|O_EXCL` creation closes both:
      - the cross-clone case (2026-06-17 postmortem: user had two repo clones,
        each scheduler saw its own per-repo lock and didn't notice the other)
      - the within-clone TOCTOU race (two processes both check then both write)
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    acquired = False
    for _attempt in range(2):  # one retry after clearing a stale lock
        try:
            fd = os.open(str(LOCK_PATH), flags, 0o644)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            try:
                other_pid = int(LOCK_PATH.read_text().strip())
            except (ValueError, OSError):
                other_pid = None
            if other_pid and _process_alive(other_pid):
                console.print(
                    f"[red]Another `tcf schedule` is already running "
                    f"(PID {other_pid}).[/red]\n"
                    f"[red]Refusing to start a second instance.[/red]\n"
                    f"[yellow]If you're sure the other process is gone:\n"
                    f"  rm {LOCK_PATH}[/yellow]"
                )
                sys.exit(1)
            console.print(
                f"[yellow]Stale lock {LOCK_PATH} (PID {other_pid}) — removing.[/yellow]"
            )
            try:
                LOCK_PATH.unlink()
            except OSError:
                pass
    if not acquired:
        console.print(f"[red]Could not acquire {LOCK_PATH} after retries.[/red]")
        sys.exit(1)

    def _cleanup(*_args) -> None:
        try:
            if LOCK_PATH.exists() and LOCK_PATH.read_text().strip() == str(os.getpid()):
                LOCK_PATH.unlink()
        except Exception:  # noqa: BLE001
            pass

    atexit.register(_cleanup)
    # Also cleanup on SIGTERM / Ctrl-C so we don't leave a stale lock.
    # SIGHUP doesn't exist on Windows — use getattr so accessing it doesn't
    # raise AttributeError there.
    sig_names = ("SIGTERM", "SIGINT", "SIGHUP")
    for name in sig_names:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, lambda *a: (_cleanup(), sys.exit(0)))
        except (ValueError, OSError):
            pass  # not main thread, etc.


def run() -> None:
    _acquire_lock()
    cfg = load_config()
    sched = BackgroundScheduler()
    sched.add_job(
        _refresh_and_schedule,
        trigger=IntervalTrigger(minutes=cfg.scout.poll_interval_minutes),
        args=[sched],
        id="scout-refresh",
        next_run_time=datetime.now(),  # run once immediately
    )
    sched.start()
    console.print(
        f"[green]Scheduler running (PID {os.getpid()}). "
        f"Scout every {cfg.scout.poll_interval_minutes} min. Ctrl-C to stop.[/green]"
    )
    try:
        while True:
            import time

            time.sleep(60)
    except KeyboardInterrupt:
        sched.shutdown(wait=False)
        console.print("[yellow]Scheduler stopped.[/yellow]")


def status() -> None:
    cfg = load_config()
    console.print(f"[bold]TCF Bot status[/bold] (target_strategy={cfg.date_strategy.mode.value})")
    exams = load_state()
    if not exams:
        console.print("[yellow]No scout state yet. Run `tcf scout` first.[/yellow]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Date")
    table.add_column("Location")
    table.add_column("Opens at")
    table.add_column("Closes at")
    table.add_column("Price")
    table.add_column("Status")
    table.add_column("exam_id")
    for e in exams:
        table.add_row(
            e.date,
            e.location_key or "?",
            str(e.opens_at) if e.opens_at else "?",
            str(e.closes_at) if e.closes_at else "?",
            e.price,
            "sold out" if e.sold_out else e.status,
            str(e.exam_id) if e.exam_id else "—",
        )
    console.print(table)
    bookings = load_bookings()
    if bookings:
        console.print("[bold]Booked:[/bold] " + ", ".join(
            f"{b.get('date')} @ {b.get('location') or '?'}" for b in bookings
        ))
