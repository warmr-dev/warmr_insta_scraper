"""Per-session activity trail for the dashboard.

Structured logs already say what happened, but they live in the container and
disappear on redeploy. This writes the same story to Postgres, keyed by account,
so the dashboard can answer "what is session N doing right now" without anyone
opening a log viewer.

Three rules, all of which exist because this runs inside the collection loop:

- **Never raise.** A logging failure must not lose a cycle's stories. Every
  writer swallows its exception and reports through the structured log instead.
- **Never store cookie values.** Only usernames and target handles go in here;
  the table is read by the dashboard, and `logging_setup` redacts the same keys.
- **Bounded.** `record` truncates target lists and messages, and `prune` drops
  old rows - at 11 accounts a minute this table would otherwise grow without
  limit for data nobody reads after an hour.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import delete, text

from .db.models import ActivityLog
from .db.session import session_scope
from .logging_setup import get_logger

log = get_logger(__name__)

# A poll can touch 50+ handles. The dashboard shows a summary plus the first
# handful, so storing every one would bloat the row for no visible gain.
MAX_TARGETS = 40
MAX_MESSAGE = 500


def record(
    username: str,
    phase: str,
    *,
    status: str = "ok",
    message: str | None = None,
    targets: list[str] | None = None,
    item_count: int | None = None,
    duration_ms: int | None = None,
) -> None:
    """Append one activity row. Never raises - logging must not break collection."""
    trimmed: list[str] | None = None
    if targets:
        trimmed = [str(t)[:64] for t in targets[:MAX_TARGETS]]
        if len(targets) > MAX_TARGETS:
            trimmed.append(f"...+{len(targets) - MAX_TARGETS} more")

    try:
        with session_scope() as session:
            session.add(
                ActivityLog(
                    username=username,
                    phase=phase,
                    status=status,
                    message=message[:MAX_MESSAGE] if message else None,
                    targets=trimmed,
                    item_count=item_count,
                    duration_ms=duration_ms,
                )
            )
    except Exception as exc:  # noqa: BLE001 - the trail is never worth a cycle
        log.warning("activity_write_failed", phase=phase, error=str(exc)[:120])


def prune(older_than_hours: int = 48) -> int:
    """Drop stale rows. Returns how many went. Never raises."""
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(hours=older_than_hours)
    try:
        with session_scope() as session:
            result = session.execute(
                delete(ActivityLog).where(ActivityLog.occurred_at < cutoff)
            )
            return int(result.rowcount or 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("activity_prune_failed", error=str(exc)[:120])
        return 0


def recent(username: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Newest-first activity, for the CLI. The dashboard reads SQL directly."""
    sql = """
        SELECT username, phase, status, message, targets, item_count,
               duration_ms, occurred_at
          FROM activity_log
         {where}
         ORDER BY occurred_at DESC
         LIMIT :limit
    """.format(where="WHERE username = :username" if username else "")

    params: dict[str, Any] = {"limit": limit}
    if username:
        params["username"] = username

    try:
        with session_scope() as session:
            rows = session.execute(text(sql), params).mappings().all()
            return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.warning("activity_read_failed", error=str(exc)[:120])
        return []
