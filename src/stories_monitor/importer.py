"""CSV importer for the ~57k monitored targets (SPEC Phase 1).

Runs entirely offline: no Instagram calls, ever. Rows lacking a numeric `user_id`
are still imported when a username is present, marked `status='unreachable'` so a
later resolution pass can fill in the pk - that pass is a separate process.

Idempotent: re-running upserts username/instagram_url/shard_id but NEVER touches
`last_reel_media_ts`, which is the live change-detection watermark owned by the
poller.
"""

from __future__ import annotations

import csv
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from .config import get_settings
from .db.models import Target
from .db.session import session_scope
from .logging_setup import configure_logging, get_logger

log = get_logger(__name__)

__all__ = ["ImportReport", "ImportRow", "import_csv", "main", "parse_csv"]

# SPEC section 2: Instagram caps following at 7,500/account -> 8 shards minimum for 57k.
MIN_SHARDS = 8
DEFAULT_BATCH_SIZE = 1000

# Header aliases, matched case-insensitively after stripping non-alphanumerics.
_USER_ID_KEYS = ("userid", "pk", "id", "instagramid", "igid", "profileid")
_USERNAME_KEYS = ("username", "user", "handle", "login", "screenname", "account")
_URL_KEYS = ("instagramurl", "url", "link", "profileurl", "instagramlink", "profile")

_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_URL_USERNAME_RE = re.compile(
    r"(?:instagram\.com|instagr\.am)/+(?:p/)?([A-Za-z0-9._]{1,30})", re.IGNORECASE
)
# Path segments that are Instagram routes, not usernames.
_RESERVED_USERNAMES = {
    "p",
    "reel",
    "reels",
    "stories",
    "explore",
    "accounts",
    "direct",
    "tv",
    "s",
    "web",
}


def _norm_header(raw: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (raw or "").strip().lower())


@dataclass(slots=True)
class ImportRow:
    """One accepted CSV row, normalised."""

    user_id: int | None
    username: str
    instagram_url: str | None
    needs_resolution: bool = False


@dataclass(slots=True)
class ImportReport:
    """Count report printed at the end of an import run (SPEC Phase 1 acceptance)."""

    path: str = ""
    rows_read: int = 0
    valid: int = 0
    duplicates_dropped: int = 0
    needs_resolution: int = 0
    skipped: int = 0
    skip_reasons: Counter[str] = field(default_factory=Counter)
    inserted: int = 0
    updated: int = 0
    shard_count: int = 0
    per_shard: Counter[int] = field(default_factory=Counter)

    def render(self) -> str:
        lines = [
            "Target import report",
            f"  source                : {self.path}",
            f"  rows read             : {self.rows_read}",
            f"  valid                 : {self.valid}",
            f"  duplicates dropped    : {self.duplicates_dropped}",
            f"  needing pk resolution : {self.needs_resolution}",
            f"  skipped               : {self.skipped}",
        ]
        for reason, count in sorted(self.skip_reasons.items(), key=lambda kv: -kv[1]):
            lines.append(f"      - {reason}: {count}")
        lines += [
            f"  inserted              : {self.inserted}",
            f"  updated               : {self.updated}",
            f"  shards                : {self.shard_count}",
        ]
        for shard in sorted(self.per_shard):
            lines.append(f"      shard {shard:>3}: {self.per_shard[shard]}")
        return "\n".join(lines)


# --- parsing ------------------------------------------------------------------


def _detect_columns(fieldnames: list[str]) -> dict[str, str]:
    """Map logical field -> actual CSV header. Order-independent, case-insensitive."""
    mapping: dict[str, str] = {}
    normalised = {_norm_header(f): f for f in fieldnames if f}

    for logical, keys in (
        ("user_id", _USER_ID_KEYS),
        ("username", _USERNAME_KEYS),
        ("instagram_url", _URL_KEYS),
    ):
        for key in keys:
            if key in normalised:
                mapping[logical] = normalised[key]
                break
    return mapping


def _parse_user_id(raw: str | None) -> int | None:
    if raw is None:
        return None
    value = raw.strip().strip('"').replace(",", "").replace(" ", "")
    if not value:
        return None
    # Tolerate values exported as floats by spreadsheets ("12345.0").
    if value.endswith(".0"):
        value = value[:-2]
    if not value.isdigit():
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def _username_from_url(url: str) -> str | None:
    match = _URL_USERNAME_RE.search(url)
    if not match:
        return None
    candidate = match.group(1).lower()
    if candidate in _RESERVED_USERNAMES:
        return None
    return candidate


def _normalise_username(raw: str | None, url: str | None) -> str | None:
    value = (raw or "").strip().lstrip("@")
    if value and "/" in value:
        # Someone put a URL in the username column.
        value = _username_from_url(value) or ""
    if not value and url:
        value = _username_from_url(url) or ""
    value = value.strip().lower()
    if not value or not _USERNAME_RE.match(value):
        return None
    return value


def _normalise_url(raw: str | None, username: str | None) -> str | None:
    value = (raw or "").strip()
    if value:
        if not value.lower().startswith(("http://", "https://")):
            value = f"https://{value.lstrip('/')}"
        return value
    if username:
        return f"https://www.instagram.com/{username}/"
    return None


def parse_csv(path: Path, report: ImportReport) -> list[ImportRow]:
    """Read and normalise the CSV. No network calls, no DB access.

    Duplicates are dropped by user_id, and by username for rows with no user_id.
    """
    rows: list[ImportRow] = []
    seen_ids: set[int] = set()
    seen_usernames: set[str] = set()

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise click.ClickException(f"{path} has no header row")

        columns = _detect_columns(list(reader.fieldnames))
        if not columns:
            raise click.ClickException(
                f"{path}: could not find a user_id/username/url column among "
                f"{reader.fieldnames}"
            )
        log.info("importer.columns_detected", **columns)

        for raw_row in reader:
            report.rows_read += 1

            user_id = _parse_user_id(
                raw_row.get(columns["user_id"]) if "user_id" in columns else None
            )
            raw_url = raw_row.get(columns["instagram_url"]) if "instagram_url" in columns else None
            username = _normalise_username(
                raw_row.get(columns["username"]) if "username" in columns else None,
                raw_url,
            )
            url = _normalise_url(raw_url, username)

            if user_id is None and username is None:
                report.skipped += 1
                report.skip_reasons["no usable user_id, username or url"] += 1
                continue

            if user_id is not None:
                if user_id in seen_ids:
                    report.duplicates_dropped += 1
                    continue
                seen_ids.add(user_id)
                if username is None:
                    # We have the pk; a placeholder username is fine, the poller
                    # keys on user_id and a later pass can backfill the handle.
                    username = f"id_{user_id}"
                else:
                    seen_usernames.add(username)
                rows.append(
                    ImportRow(user_id=user_id, username=username, instagram_url=url)
                )
            else:
                assert username is not None
                if username in seen_usernames:
                    report.duplicates_dropped += 1
                    continue
                seen_usernames.add(username)
                report.needs_resolution += 1
                rows.append(
                    ImportRow(
                        user_id=None,
                        username=username,
                        instagram_url=url,
                        needs_resolution=True,
                    )
                )

    report.valid = len(rows)
    return rows


# --- sharding -----------------------------------------------------------------


def compute_shard_count(total: int, max_per_account: int | None = None) -> int:
    """ceil(total / max_follows_per_account), never fewer than 8 (SPEC section 2)."""
    cap = max_per_account or get_settings().max_follows_per_account
    if cap <= 0:
        raise ValueError("max_follows_per_account must be positive")
    needed = math.ceil(total / cap) if total > 0 else 0
    return max(MIN_SHARDS, needed)


def assign_shards(rows: list[ImportRow], shard_count: int) -> list[int]:
    """Balanced round-robin by position - deterministic for a given input order.

    Round-robin keeps every shard within one row of every other, so no shard can
    exceed max_follows_per_account while the total is within shard_count * cap.
    """
    return [index % shard_count for index in range(len(rows))]


# --- persistence --------------------------------------------------------------


def _upsert_batch(
    session: Session, rows: list[ImportRow], shards: list[int]
) -> None:
    """INSERT ... ON CONFLICT (user_id) DO UPDATE.

    last_reel_media_ts, last_seen_in_tray and status are deliberately excluded from
    the update set: they are live state owned by the poller, not import data.
    """
    payload: list[dict[str, Any]] = []
    for row, shard in zip(rows, shards, strict=True):
        payload.append(
            {
                "user_id": row.user_id,
                "username": row.username,
                "instagram_url": row.instagram_url,
                "shard_id": shard,
                "status": "unreachable" if row.needs_resolution else "active",
            }
        )

    stmt = pg_insert(Target).values(payload)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Target.user_id],
        set_={
            "username": stmt.excluded.username,
            "instagram_url": stmt.excluded.instagram_url,
            "shard_id": stmt.excluded.shard_id,
        },
    )
    session.execute(stmt)


def _next_synthetic_id(session: Session) -> int:
    """Negative surrogate pks for rows awaiting user_id resolution.

    `targets.user_id` is the primary key and cannot be null, and real Instagram pks
    are always positive - so negatives are unambiguously "not yet resolved" and can
    never collide with a real pk.
    """
    lowest = session.execute(select(func.min(Target.user_id))).scalar_one_or_none()
    if lowest is None or lowest >= 0:
        return -1
    return int(lowest) - 1


def import_csv(
    path: str | Path,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    shard_count: int | None = None,
) -> ImportReport:
    """Parse `path` and upsert into `targets`. Returns the count report."""
    csv_path = Path(path).expanduser()
    if not csv_path.is_file():
        raise click.ClickException(f"CSV not found: {csv_path}")

    report = ImportReport(path=str(csv_path))
    rows = parse_csv(csv_path, report)

    resolved_count = sum(1 for r in rows if r.user_id is not None)
    report.shard_count = shard_count or compute_shard_count(len(rows))
    shards = assign_shards(rows, report.shard_count)
    report.per_shard.update(shards)

    log.info(
        "importer.parsed",
        rows_read=report.rows_read,
        valid=report.valid,
        resolved=resolved_count,
        needs_resolution=report.needs_resolution,
        shards=report.shard_count,
    )

    if dry_run or not rows:
        return report

    with session_scope() as session:
        existing_ids = _existing_ids(session, [r.user_id for r in rows if r.user_id])

        # Assign surrogate pks to unresolved rows so they land in the table at all.
        next_synthetic = _next_synthetic_id(session)
        existing_usernames = _existing_usernames(
            session, [r.username for r in rows if r.user_id is None]
        )
        for row in rows:
            if row.user_id is not None:
                continue
            known = existing_usernames.get(row.username)
            if known is not None:
                row.user_id = known
            else:
                row.user_id = next_synthetic
                next_synthetic -= 1

        for start in range(0, len(rows), batch_size):
            chunk = rows[start : start + batch_size]
            chunk_shards = shards[start : start + batch_size]
            _upsert_batch(session, chunk, chunk_shards)

        report.updated = sum(
            1
            for row in rows
            if row.user_id in existing_ids
            or (row.needs_resolution and row.username in existing_usernames)
        )
        report.inserted = len(rows) - report.updated

    log.info(
        "importer.done",
        inserted=report.inserted,
        updated=report.updated,
        shards=report.shard_count,
    )
    return report


def _existing_ids(session: Session, user_ids: list[int]) -> set[int]:
    """Which of these pks already exist, so we can report insert vs update."""
    found: set[int] = set()
    for start in range(0, len(user_ids), 5000):
        chunk = user_ids[start : start + 5000]
        if not chunk:
            continue
        rows = session.execute(
            select(Target.user_id).where(Target.user_id.in_(chunk))
        ).scalars()
        found.update(int(uid) for uid in rows)
    return found


def _existing_usernames(session: Session, usernames: list[str]) -> dict[str, int]:
    """Map username -> existing pk, for rows imported earlier without a user_id."""
    found: dict[str, int] = {}
    for start in range(0, len(usernames), 5000):
        chunk = usernames[start : start + 5000]
        if not chunk:
            continue
        rows = session.execute(
            select(Target.username, Target.user_id).where(Target.username.in_(chunk))
        ).all()
        for username, user_id in rows:
            found[str(username)] = int(user_id)
    return found


# --- CLI ----------------------------------------------------------------------


@click.command(name="import-targets")
@click.argument("csv_path", type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--batch-size",
    default=DEFAULT_BATCH_SIZE,
    show_default=True,
    help="Rows per INSERT statement.",
)
@click.option(
    "--shards",
    "shard_count",
    type=int,
    default=None,
    help="Override the computed shard count. Default: max(8, ceil(rows / 7000)).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Parse and report without touching the database.",
)
def main(csv_path: Path, batch_size: int, shard_count: int | None, dry_run: bool) -> None:
    """Import monitored targets from CSV_PATH into the `targets` table."""
    configure_logging(json_output=False)
    report = import_csv(
        csv_path, batch_size=batch_size, dry_run=dry_run, shard_count=shard_count
    )
    click.echo(report.render())
    if dry_run:
        click.echo("\n(dry run - nothing written)")
    if report.skipped:
        sys.exit(0)


if __name__ == "__main__":
    main()
