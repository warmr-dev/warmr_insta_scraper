"""CLI entry points for the seven processes (SPEC section 6).

Each process is independently restartable and picks up state from Postgres.
No process may assume another is running.
"""

from __future__ import annotations

import asyncio
import json

import click

from .config import Q_ANALYZE, Q_BIZCHECK, Q_FETCH, Q_FETCH_DIRECT, Q_NOTIFY, get_settings
from .logging_setup import configure_logging, get_logger

log = get_logger(__name__)


@click.group()
@click.option("--log-level", default="INFO", help="DEBUG | INFO | WARNING | ERROR")
@click.option("--console-logs", is_flag=True, help="Human-readable logs instead of JSON")
def cli(log_level: str, console_logs: bool) -> None:
    """Instagram Stories Monitor."""
    configure_logging(level=log_level, json_output=not console_logs)


# --- the seven processes ---------------------------------------------------


@cli.command()
def poller() -> None:
    """Poll reels_tray per worker account, diff, enqueue (SPEC 7.1)."""
    from .workers.poller import run_poller

    asyncio.run(run_poller())


@cli.command()
def fetcher() -> None:
    """Drain the fetch queue, batch reels_media, write stories (SPEC 7.3)."""
    from .workers.fetcher import run_fetcher

    run_fetcher()


@cli.command()
def analyzer() -> None:
    """OCR + cheap model + smart model (SPEC 7.4)."""
    from .workers.analyzer import Analyzer

    Analyzer().run()


@cli.command()
def bizcheck() -> None:
    """Vendor / service-fit / geo / community checks (SPEC 7.5)."""
    from .workers.bizcheck import BizChecker

    BizChecker().run()


@cli.command()
def notifier() -> None:
    """Slack delivery with retry and idempotency (SPEC 7.6)."""
    from .workers.notifier import Notifier

    Notifier().run()


@cli.command()
def follower() -> None:
    """Bootstrap follows at a safe rate (SPEC 7.7)."""
    from .workers.follower import Follower

    Follower().run()


@cli.command()
def warden() -> None:
    """Account health, alerts, recovery orchestration (SPEC 7.8)."""
    from .workers.warden import Warden

    Warden().run()


# --- operational commands --------------------------------------------------


@cli.command("import-targets")
@click.argument("csv_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--dry-run", is_flag=True, help="Parse and report without writing")
def import_targets(csv_path: str, dry_run: bool) -> None:
    """Import the target CSV, normalise ids, assign shards (SPEC Phase 1)."""
    from .importer import import_csv

    report = import_csv(csv_path, dry_run=dry_run)
    click.echo(report.render())


@cli.command("probe-tray")
@click.option("--username", default=None, help="Worker account username")
@click.option("--from-env", is_flag=True, help="Read credentials from the environment")
@click.option("--paginate", is_flag=True, help="Follow tray cursors (SPEC 7.2)")
@click.option("--max-pages", default=20, show_default=True)
@click.option("--output-dir", default="probe_output", show_default=True)
def probe_tray(
    username: str | None,
    from_env: bool,
    paginate: bool,
    max_pages: int,
    output_dir: str,
) -> None:
    """Probe whether reels_tray truncates (SPEC 7.2 - blocks shard sizing)."""
    import pathlib
    import sys
    from dataclasses import asdict

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    from scripts.probe_tray import append_summary, probe

    out = pathlib.Path(output_dir)
    result = probe(
        username=username,
        from_env=from_env,
        paginate=paginate,
        max_pages=max_pages,
        output_dir=out,
    )
    append_summary(result, out / "summary.csv")
    click.echo(json.dumps(asdict(result), indent=2, default=str))


@cli.command()
def health() -> None:
    """Print current health alerts (SPEC 7.8)."""
    from .workers.warden import Warden

    alerts = Warden().check_health()
    if not alerts:
        click.echo("No alerts.")
        return
    for alert in alerts:
        click.echo(f"[{getattr(alert, 'severity', 'alert')}] {alert}")


@cli.command()
def progress() -> None:
    """Follow-graph bootstrap progress per shard (SPEC 7.7)."""
    from .workers.follower import progress_report

    click.echo(json.dumps(progress_report(), indent=2, default=str))


@cli.command()
def queues() -> None:
    """Show queue depths (SPEC section 10)."""
    from .queue import get_queue

    for name in (Q_FETCH, Q_FETCH_DIRECT, Q_ANALYZE, Q_BIZCHECK, Q_NOTIFY):
        click.echo(f"{name}: {get_queue(name).depth()}")


@cli.command()
def latency() -> None:
    """Detection latency p50/p95 - the number that proves the design (SPEC section 10)."""
    from .metrics import detection_latency_percentiles

    click.echo(json.dumps(detection_latency_percentiles(), indent=2))


@cli.command("gen-key")
def gen_key() -> None:
    """Generate a Fernet key for SECRET_KEY."""
    from .crypto import generate_key

    click.echo(generate_key())


@cli.command()
def config() -> None:
    """Show effective non-secret configuration."""
    settings = get_settings()
    secret_fields = {
        "secret_key",
        "anthropic_api_key",
        "slack_bot_token",
        "database_url",
        "redis_url",
    }
    shown = {
        k: ("***" if k in secret_fields and v else v)
        for k, v in settings.model_dump().items()
    }
    click.echo(json.dumps(shown, indent=2, default=str))


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
