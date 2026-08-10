"""Tray truncation probe (SPEC 7.2) - resolve this before anything else.

We do not know whether `feed/reels_tray/` returns every following with an active
story, or a ranked subset. The entire architecture depends on the answer.

Given one worker account, this dumps the FULL tray response and reports:

  - total entry count
  - entries with `latest_reel_media` within the last 24h
  - highlight entries vs real user entries
  - whether `next_max_id` is present (i.e. the tray paginates)

The raw JSON goes to probe_output/tray_<username>_<utc_timestamp>.json for fixture
creation, and one summary line is appended to probe_output/summary.csv. Run this
repeatedly across a day and watch whether the entry count plateaus - a plateau at a
round number is the truncation signature.

Usage:
    python scripts/probe_tray.py                        # fixture mode, any account
    python scripts/probe_tray.py --username worker_01   # load creds from the DB
    IG_USERNAME=... IG_PASSWORD=... python scripts/probe_tray.py --from-env

Never imports instagrapi (SPEC section 11) - everything goes through the transport.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import click

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stories_monitor.config import get_settings  # noqa: E402
from stories_monitor.logging_setup import configure_logging, get_logger  # noqa: E402
from stories_monitor.transport import get_transport  # noqa: E402

log = get_logger("probe_tray")

PROBE_DIR = Path("probe_output")
SUMMARY_CSV = PROBE_DIR / "summary.csv"
DAY_SECONDS = 24 * 60 * 60

SUMMARY_FIELDS = [
    "probed_at",
    "username",
    "transport",
    "pages_fetched",
    "total_entries",
    "user_entries",
    "highlight_entries",
    "fresh_24h",
    "with_latest_reel_media",
    "with_prefetched_items",
    "next_max_id_present",
    "next_max_id",
    "raw_file",
    "error",
]


@dataclass(slots=True)
class ProbeResult:
    probed_at: str
    username: str
    transport: str
    pages_fetched: int = 0
    total_entries: int = 0
    user_entries: int = 0
    highlight_entries: int = 0
    fresh_24h: int = 0
    with_latest_reel_media: int = 0
    with_prefetched_items: int = 0
    next_max_id_present: bool = False
    next_max_id: str = ""
    raw_file: str = ""
    error: str = ""

    def render(self) -> str:
        return "\n".join(
            [
                "Tray probe (SPEC 7.2)",
                f"  probed at (UTC)          : {self.probed_at}",
                f"  worker account           : {self.username}",
                f"  transport                : {self.transport}",
                f"  pages fetched            : {self.pages_fetched}",
                f"  total tray entries       : {self.total_entries}",
                f"    user entries           : {self.user_entries}",
                f"    highlight entries      : {self.highlight_entries}",
                f"  with latest_reel_media   : {self.with_latest_reel_media}",
                f"  fresh within 24h         : {self.fresh_24h}",
                f"  with prefetched items    : {self.with_prefetched_items}",
                f"  next_max_id present      : {self.next_max_id_present}"
                + (f" ({self.next_max_id})" if self.next_max_id else ""),
                f"  raw JSON                 : {self.raw_file or '(not written)'}",
            ]
            + ([f"  error                    : {self.error}"] if self.error else [])
        )


# --- credentials ---------------------------------------------------------------


def _creds_from_db(username: str) -> tuple[str, str, dict[str, Any] | None]:
    """Load a worker account's password (decrypted) and saved session from Postgres."""
    from sqlalchemy import select

    from stories_monitor.crypto import SecretBox
    from stories_monitor.db import WorkerAccount, session_scope

    with session_scope() as session:
        account = session.execute(
            select(WorkerAccount).where(WorkerAccount.username == username)
        ).scalar_one_or_none()
        if account is None:
            raise click.ClickException(f"no worker_account with username {username!r}")
        password = SecretBox().decrypt(account.password_enc)
        return account.username, password, account.session_json


def _creds_from_env() -> tuple[str, str, dict[str, Any] | None]:
    username = os.environ.get("IG_USERNAME", "")
    password = os.environ.get("IG_PASSWORD", "")
    if not username or not password:
        raise click.ClickException(
            "--from-env needs IG_USERNAME and IG_PASSWORD in the environment"
        )
    return username, password, None


# --- probe ---------------------------------------------------------------------


def _entry_dicts(tray: Any) -> list[dict[str, Any]]:
    """Best-effort raw dict per entry, for the fixture dump."""
    out: list[dict[str, Any]] = []
    for entry in getattr(tray, "entries", []) or []:
        raw = getattr(entry, "raw", None)
        if raw:
            out.append(raw)
        else:
            out.append(
                {
                    "id": getattr(entry, "id", None),
                    "latest_reel_media": getattr(entry, "latest_reel_media", None),
                    "seen": getattr(entry, "seen", None),
                    "user": getattr(entry, "user", {}),
                    "items": getattr(entry, "items", []),
                }
            )
    return out


def probe(
    *,
    username: str | None,
    from_env: bool,
    paginate: bool,
    max_pages: int,
    output_dir: Path,
) -> ProbeResult:
    settings = get_settings()
    now = dt.datetime.now(dt.UTC)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")

    label = username or os.environ.get("IG_USERNAME") or "fixture"
    result = ProbeResult(
        probed_at=now.isoformat(),
        username=label,
        transport=settings.ig_transport,
    )

    session_json: dict[str, Any] | None = None
    password = ""
    if not settings.is_fixture_mode:
        if from_env:
            label, password, session_json = _creds_from_env()
        elif username:
            label, password, session_json = _creds_from_db(username)
        else:
            raise click.ClickException(
                "live mode needs --username (loaded from DB) or --from-env"
            )
        result.username = label

    transport = get_transport()
    try:
        if not settings.is_fixture_mode:
            if session_json:
                transport.load_session(session_json)
            else:
                # SPEC section 8: log in only when no saved session exists.
                transport.login(label, password)

        pages: list[dict[str, Any]] = []
        all_entries: list[dict[str, Any]] = []
        max_id: str | None = None
        cold_start = True

        while True:
            tray = transport.reels_tray(cold_start=cold_start, max_id=max_id)
            cold_start = False
            result.pages_fetched += 1

            page_raw = getattr(tray, "raw", {}) or {}
            pages.append(page_raw)
            entries = _entry_dicts(tray)
            all_entries.extend(entries)

            for entry in getattr(tray, "entries", []) or []:
                if getattr(entry, "is_user_entry", False):
                    result.user_entries += 1
                else:
                    result.highlight_entries += 1

                latest = getattr(entry, "latest_reel_media", None)
                if latest:
                    result.with_latest_reel_media += 1
                    if now.timestamp() - float(latest) <= DAY_SECONDS:
                        result.fresh_24h += 1

                if getattr(entry, "has_prefetched_items", False):
                    result.with_prefetched_items += 1

            next_max_id = getattr(tray, "next_max_id", None)
            if result.pages_fetched == 1:
                result.next_max_id_present = bool(next_max_id)
                result.next_max_id = next_max_id or ""

            if not (paginate and next_max_id) or result.pages_fetched >= max_pages:
                break
            max_id = next_max_id

        result.total_entries = len(all_entries)

        output_dir.mkdir(parents=True, exist_ok=True)
        safe_label = "".join(c if c.isalnum() or c in "._-" else "_" for c in label)
        raw_path = output_dir / f"tray_{safe_label}_{stamp}.json"
        raw_path.write_text(
            json.dumps(
                {
                    "probed_at": result.probed_at,
                    "username": label,
                    "transport": settings.ig_transport,
                    "summary": {
                        k: v for k, v in asdict(result).items() if k != "raw_file"
                    },
                    "pages": pages,
                    "entries": all_entries,
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        result.raw_file = str(raw_path)

    except Exception as exc:  # noqa: BLE001 - the probe must always record an outcome
        result.error = f"{type(exc).__name__}: {exc}"
        log.error("probe.failed", error=result.error)
    finally:
        try:
            transport.close()
        except Exception:  # noqa: BLE001
            pass

    return result


def append_summary(result: ProbeResult, summary_path: Path) -> None:
    """One line per run, so a day of probes is a single readable table."""
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not summary_path.exists()
    with summary_path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow({k: asdict(result).get(k, "") for k in SUMMARY_FIELDS})


@click.command(name="probe-tray")
@click.option("--username", default=None, help="Worker account username, loaded from the DB.")
@click.option("--from-env", is_flag=True, help="Use IG_USERNAME / IG_PASSWORD instead of the DB.")
@click.option(
    "--paginate/--no-paginate",
    default=False,
    help="Follow next_max_id to measure the true total (SPEC 7.2).",
)
@click.option("--max-pages", default=20, show_default=True, help="Pagination safety stop.")
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=PROBE_DIR,
    show_default=True,
)
def main(
    username: str | None,
    from_env: bool,
    paginate: bool,
    max_pages: int,
    output_dir: Path,
) -> None:
    """Dump a full reels_tray response and record truncation evidence."""
    configure_logging(json_output=False)
    result = probe(
        username=username,
        from_env=from_env,
        paginate=paginate,
        max_pages=max_pages,
        output_dir=output_dir,
    )
    append_summary(result, output_dir / SUMMARY_CSV.name)
    click.echo(result.render())
    click.echo(f"\nSummary appended to {output_dir / SUMMARY_CSV.name}")
    if result.error:
        sys.exit(1)


if __name__ == "__main__":
    main()
