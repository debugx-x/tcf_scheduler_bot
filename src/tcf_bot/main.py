"""CLI entrypoint. Typer subcommands wire to the rest of the package."""

from __future__ import annotations

import typer
from rich.console import Console

app = typer.Typer(help="TCF Canada exam booking bot.", no_args_is_help=True)
console = Console()


@app.command()
def scout() -> None:
    """Fetch the AF Canada TCF page once, parse exam list, write state/exams.json."""
    from .scout import run_once

    exams = run_once()
    console.print(f"[green]Scout found {len(exams)} exam(s).[/green]")
    for e in exams:
        console.print(
            f"  {e.label}  exam_id={e.exam_id}  opens={e.opens_at}  price={e.price}"
            f"{'  SOLD OUT' if e.sold_out else ''}"
        )


@app.command()
def schedule() -> None:
    """Schedule strikes for all upcoming registration windows."""
    from .scheduler import run

    run()


@app.command()
def strike(
    exam_id: int | None = typer.Option(None, help="specific exam_id to strike"),
    earliest: bool = typer.Option(False, help="strike earliest available, fall back chronologically"),
    dry_run: bool = typer.Option(False, help="abort before final payment"),
) -> None:
    """Fire a strike attempt. Use --exam-id N or --earliest."""
    from .booker import strike_earliest, strike_one

    if earliest:
        strike_earliest(dry_run=dry_run)
    elif exam_id is not None:
        strike_one(exam_id=exam_id, dry_run=dry_run)
    else:
        raise typer.BadParameter("specify --exam-id N or --earliest")


@app.command("dry-run")
def dry_run(
    site: str = typer.Option("sandiego", help="sandiego (default) or canada"),
    exam_id: int | None = typer.Option(None, help="exam_id to attempt"),
) -> None:
    """End-to-end test against AF San Diego. Aborts before final payment."""
    from .booker import dry_run as _dry_run

    _dry_run(site=site, exam_id=exam_id)


@app.command()
def status() -> None:
    """Show scheduled strikes and latest scout output."""
    from .scheduler import status as _status

    _status()


@app.command()
def benchmark() -> None:
    """Measure page-load + DOM-poll latency from this network."""
    from .booker import benchmark as _benchmark

    _benchmark()


@app.command()
def preflight() -> None:
    """Fast (<30s) smoke test of every code path. Run before any real strike."""
    import sys as _sys

    from .preflight import run as _run

    _sys.exit(_run())


if __name__ == "__main__":
    app()
