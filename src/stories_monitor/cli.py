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


@cli.command("session-follower")
def session_follower() -> None:
    """Follow targets from live cookie sessions, at a human rhythm."""
    from .workers.session_follower import SessionFollower

    SessionFollower().run()


@cli.command()
def warden() -> None:
    """Account health, alerts, recovery orchestration (SPEC 7.8)."""
    from .workers.warden import Warden

    Warden().run()


# --- operational commands --------------------------------------------------


@cli.command("import-targets")
@click.argument("csv_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--dry-run", is_flag=True, help="Parse and report without writing")
@click.option(
    "--no-follow-queue",
    is_flag=True,
    help="Import targets only; do not queue them to be followed",
)
def import_targets(csv_path: str, dry_run: bool, no_follow_queue: bool) -> None:
    """Import targets from a CSV or Excel file and queue them to be followed."""
    from .importer import import_csv

    report = import_csv(csv_path, dry_run=dry_run, enqueue_for_follow=not no_follow_queue)
    click.echo(report.render())


@cli.command("follow-status")
def follow_status_cmd() -> None:
    """Follow-pool progress, and who owns what."""
    from sqlalchemy import func, select

    from .db.models import SessionFollow
    from .db.session import session_scope
    from .follow_assign import stats

    snapshot = stats()
    if snapshot.total == 0:
        click.echo("The follow pool is empty. Run `import-targets` first.")
        return

    pct = 100.0 * snapshot.done / snapshot.total
    click.echo("Follow pool")
    click.echo(f"  targets total : {snapshot.total}")
    click.echo(f"  followed      : {snapshot.following}")
    click.echo(f"  requested     : {snapshot.requested}  (private, awaiting approval)")
    click.echo(f"  free          : {snapshot.free}")
    click.echo(f"  claimed       : {snapshot.claimed}")
    click.echo(f"  failed        : {snapshot.failed}")
    click.echo(f"  unavailable   : {snapshot.unavailable}")
    click.echo(f"  progress      : {pct:.1f}%")

    with session_scope() as session:
        rows = session.execute(
            select(SessionFollow.session_username, func.count())
            .where(SessionFollow.session_username.is_not(None))
            .group_by(SessionFollow.session_username)
            .order_by(func.count().desc())
        ).all()
    if rows:
        click.echo("\nPer session")
        for username, count in rows:
            click.echo(f"  {username:<28} {count}")


@cli.command("follow-reclaim")
@click.option("--session", "session_username", help="Free one session's targets by name")
@click.option(
    "--all-dead",
    is_flag=True,
    help="Free the targets of every session no longer active",
)
def follow_reclaim_cmd(session_username: str | None, all_dead: bool) -> None:
    """Hand a dead session's targets back to the pool.

    This is what makes a dead account's 256 follows available to the survivors.
    The follower does it automatically every cycle; this is the manual lever.
    """
    from .follow_assign import reap_session, reclaim_dead_sessions, reclaim_stale_claims

    if not session_username and not all_dead:
        raise click.ClickException("Pass --session <username> or --all-dead")

    if session_username:
        freed = reap_session(session_username, reason="manual reclaim")
        click.echo(f"Freed {freed} targets held by {session_username}.")
    if all_dead:
        freed = reclaim_dead_sessions()
        stale = reclaim_stale_claims()
        click.echo(f"Freed {freed} targets from inactive sessions, {stale} stale claims.")


@cli.command("seed-worker")
@click.option("--username", default=None, help="Defaults to IG_WORKER_USERNAME")
@click.option("--password", default=None, help="Defaults to IG_WORKER_PASSWORD")
@click.option("--proxy-url", default=None, help="Defaults to IG_WORKER_PROXY_URL")
@click.option("--shard-id", default=0, show_default=True)
@click.option(
    "--status",
    default="warming",
    show_default=True,
    type=click.Choice(["warming", "active", "reserve"]),
)
@click.option(
    "--allow-no-proxy",
    is_flag=True,
    help="Bind the local IP instead of a proxy. Testing only (SPEC section 8).",
)
@click.option(
    "--force-device",
    is_flag=True,
    help="Regenerate device settings / rotate proxy. SPEC section 8 forbids this "
    "outside recovery - it burns accounts.",
)
def seed_worker_cmd(
    username: str | None,
    password: str | None,
    proxy_url: str | None,
    shard_id: int,
    status: str,
    allow_no_proxy: bool,
    force_device: bool,
) -> None:
    """Insert a worker account (offline - no Instagram call)."""
    from .seed import seed_worker

    settings = get_settings()
    report = seed_worker(
        username=username or settings.ig_worker_username,
        password=password or settings.ig_worker_password,
        proxy_url=proxy_url or settings.ig_worker_proxy_url,
        shard_id=shard_id,
        status=status,
        force_device=force_device,
        allow_no_proxy=allow_no_proxy,
    )
    click.echo(json.dumps(report, indent=2, default=str))


@cli.command("accounts")
def accounts_cmd() -> None:
    """List seeded worker accounts and which one is active in .env."""
    from sqlalchemy import select

    from .db.models import WorkerAccount
    from .db.session import session_scope

    active = get_settings().ig_worker_username
    with session_scope() as session:
        rows = session.scalars(select(WorkerAccount).order_by(WorkerAccount.id)).all()
        if not rows:
            click.echo("No accounts seeded. Run `stories seed-worker`.")
            return
        click.echo(f"{'':2} {'id':>3}  {'username':22} {'status':10} {'session':8} bound to")
        for account in rows:
            mark = "->" if account.username == active else "  "
            has_session = "saved" if account.session_json else "-"
            bound = account.proxy_url or "local IP"
            click.echo(
                f"{mark} {account.id:>3}  {account.username:22} {account.status:10} "
                f"{has_session:8} {bound}"
            )
    click.echo("\n-> = active in .env (IG_WORKER_USERNAME)")


@cli.command("use-account")
@click.argument("username")
def use_account_cmd(username: str) -> None:
    """Point .env at an already-seeded account, keeping its stored password.

    Credentials live encrypted in the database, so switching never needs the
    password typed again - and the account's immutable device settings and bound
    proxy travel with it (SPEC section 8).
    """
    import pathlib
    import re

    from sqlalchemy import select

    from .crypto import SecretBox
    from .db.models import WorkerAccount
    from .db.session import session_scope

    with session_scope() as session:
        account = session.scalars(
            select(WorkerAccount).where(WorkerAccount.username == username)
        ).first()
        if account is None:
            raise click.ClickException(
                f"{username} is not seeded. Run `stories seed-worker` for it first."
            )
        password = SecretBox().decrypt(account.password_enc)
        proxy = account.proxy_url or ""
        has_session = account.session_json is not None
        status = account.status

    env_path = pathlib.Path(".env")
    if not env_path.is_file():
        raise click.ClickException(".env not found")

    text = env_path.read_text()
    for key, value in (
        ("IG_WORKER_USERNAME", username),
        ("IG_WORKER_PASSWORD", password),
        ("IG_WORKER_PROXY_URL", proxy),
    ):
        pattern = rf"^{key}=.*$"
        replacement = f"{key}={value}"
        text = (
            re.sub(pattern, replacement, text, flags=re.M)
            if re.search(pattern, text, flags=re.M)
            else text.rstrip("\n") + f"\n{replacement}\n"
        )
    env_path.write_text(text)
    env_path.chmod(0o600)

    click.echo(f"switched to {username} (status={status})")
    click.echo(f"  bound to     : {proxy or 'local IP'}")
    click.echo(
        "  session      : "
        + ("saved - no login needed" if has_session else "none - run scripts/login_now.py")
    )


@cli.command("web-add")
@click.argument("username")
@click.option("--cookies", default=None, help="Строка куки из браузера")
@click.option("--cookies-file", default=None, help="Файл со строкой куки")
@click.option(
    "--user-agent",
    default=None,
    help="User-Agent браузера, из которого скопированы куки (datr привязан к нему)",
)
def web_add_cmd(
    username: str, cookies: str | None, cookies_file: str | None, user_agent: str | None
) -> None:
    """Сохранить веб-куки для аккаунта (мобильную сессию не трогает)."""
    import pathlib

    from .transport.web import COOKIE_NAMES
    from .webaccounts import save_cookies

    raw = cookies or ""
    if cookies_file:
        path = pathlib.Path(cookies_file)
        if not path.is_file():
            raise click.ClickException(f"file not found: {path}")
        raw = path.read_text()
    if not raw:
        raise click.ClickException("pass --cookies or --cookies-file")

    account = save_cookies(username, raw, user_agent=user_agent)
    click.echo(f"saved for @{account.username} (shard {account.shard_id})")
    click.echo(f"  cookies: {len(account.cookies)}/{len(COOKIE_NAMES)}")
    if not user_agent:
        click.echo(
            "  ВНИМАНИЕ: не указан --user-agent. datr привязан к браузеру, "
            "и без совпадающего UA сессия живёт заметно меньше."
        )
    if account.missing_cookies:
        click.echo(f"  MISSING: {', '.join(account.missing_cookies)}")
        click.echo("  without these the feeds usually answer 302")


@cli.command("web-list")
@click.option("--check", is_flag=True, help="Проверить каждый аккаунт живым запросом")
def web_list_cmd(check: bool) -> None:
    """Аккаунты с сохранёнными веб-куки."""
    from .webaccounts import check_alive, load_accounts

    accounts = load_accounts()
    if not accounts:
        click.echo("No web cookies stored for any account.")
        click.echo("Add one: stories web-add <username> --cookies-file cookies.txt")
        return

    for account in accounts:
        missing = (
            f" | missing: {','.join(account.missing_cookies)}"
            if account.missing_cookies
            else ""
        )
        line = f"  @{account.username:22} shard={account.shard_id}{missing}"
        if check:
            alive, detail = check_alive(account)
            line += f"\n      {'РАБОТАЕТ' if alive else 'НЕ РАБОТАЕТ'}: {detail}"
        click.echo(line)

    if not check:
        click.echo("\n--check verifies cookies with a live request")


@cli.command("logs")
@click.option("--account", default=None, help="Только этот аккаунт")
@click.option("--limit", default=40, show_default=True)
def logs_cmd(account: str | None, limit: int) -> None:
    """Активность сессий - то же, что на вкладке Activity Logs в дашборде."""
    from .activity import recent

    rows = recent(account, limit)
    if not rows:
        click.echo("no activity yet - start the collector")
        return

    # Oldest first: reads like a transcript of the cycle.
    for r in reversed(rows):
        ts = r["occurred_at"].strftime("%H:%M:%S") if r["occurred_at"] else "--:--:--"
        targets = r["targets"] or []
        tail = f"  [{', '.join(targets[:5])}{'...' if len(targets) > 5 else ''}]" if targets else ""
        took = f" ({r['duration_ms'] / 1000:.1f}s)" if r["duration_ms"] else ""
        click.echo(f"{ts}  @{r['username']:<20} {r['phase']:<14} {r['message'] or ''}{took}{tail}")


@cli.command("skipped")
@click.option("--limit", default=30, show_default=True)
def skipped_cmd(limit: int) -> None:
    """Кого перестали анализировать и почему - и сколько это сэкономило."""
    from sqlalchemy import text

    from .db.session import session_scope

    with session_scope() as session:
        summary = session.execute(
            text(
                "SELECT status, count(*) ev, sum(coalesce(item_count,0)) items "
                "FROM activity_log WHERE phase='skipped' "
                "AND occurred_at > now() - interval '24 hours' "
                "GROUP BY status ORDER BY 3 DESC"
            )
        ).all()
        if not summary:
            click.echo("nothing was skipped in the last 24h")
            return

        click.echo("Skipped in 24h (photos that never reached the AI):")
        for status, events, items in summary:
            click.echo(f"  {int(items or 0):>5} x {status:<12} ({events} events)")

        rows = session.execute(
            text(
                "SELECT jsonb_array_elements_text(targets) handle, "
                "count(*) times, sum(coalesce(item_count,0)) photos, max(occurred_at) last "
                "FROM activity_log WHERE phase='skipped' AND status='irrelevant' "
                "AND targets IS NOT NULL GROUP BY 1 ORDER BY 3 DESC LIMIT :lim"
            ),
            {"lim": limit},
        ).all()

    if rows:
        click.echo("\nTargets with no signal (rechecked periodically):")
        for handle, times, photos, last in rows:
            when = last.strftime("%d.%m %H:%M") if last else "-"
            click.echo(f"  @{handle:<28} {int(photos or 0):>4} photos  x{times}  last {when}")


@cli.command("web-remove")
@click.argument("username")
def web_remove_cmd(username: str) -> None:
    """Удалить веб-куки аккаунта (мобильная сессия остаётся)."""
    from .webaccounts import clear_cookies

    if clear_cookies(username):
        click.echo(f"web cookies for @{username} removed")
    else:
        click.echo(f"@{username} had no web cookies")


@cli.command("login-test")
@click.option("--username", default=None, help="Worker account username")
@click.option(
    "--allow-no-proxy",
    is_flag=True,
    help="Log in from this machine's IP with no proxy bound. Testing only - the "
    "local IP becomes the account's identity (SPEC section 8).",
)
@click.option(
    "--verification-code",
    default=None,
    help="6-digit code from the authenticator app, if the account has 2FA. "
    "Read it immediately before running - codes expire in ~30s.",
)
@click.confirmation_option(
    prompt="This performs a REAL Instagram login. Repeated logins are the strongest "
    "ban signal (SPEC section 8). Continue?"
)
def login_test(
    username: str | None, allow_no_proxy: bool, verification_code: str | None
) -> None:
    """One real login through the account's bound proxy, then stop.

    Deliberately minimal: it logs in, persists the session so no further login
    is needed, and makes no other API call. It never calls media/seen/ or any
    write endpoint (SPEC section 11).
    """
    from .smoke import run_login_test

    def ask_for_code() -> str:
        """Prompt only once everything slow is done, so the code stays fresh."""
        click.echo("")
        click.echo("This account has 2FA. Open your authenticator app now.")
        click.echo("Everything else is ready - the code is used immediately.")
        return click.prompt("6-digit code", type=str).strip()

    result = run_login_test(
        username or get_settings().ig_worker_username or None,
        verification_code=verification_code,
        code_prompt=ask_for_code,
        allow_no_proxy=allow_no_proxy,
    )
    click.echo(json.dumps(result, indent=2, default=str))


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
