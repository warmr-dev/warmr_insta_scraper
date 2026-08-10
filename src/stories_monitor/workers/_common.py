"""Helpers shared by the worker processes (poller, follower, warden).

These live here rather than in poller.py because the follower and warden need the
same daily-counter ledger and the same account-incident audit trail. SPEC section 5
makes `daily_action_counters` the single source of truth for rate limits precisely
so that a process restart cannot reset a counter.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..db.models import AccountEvent, DailyActionCounter, WorkerAccount
from ..db.session import session_scope


def _bump_counter(
    session: Session,
    worker_account_id: int,
    day: dt.date,
    *,
    requests: int = 0,
    follows: int = 0,
) -> None:
    """Upsert the daily rate-limit ledger. Survives restarts (SPEC section 5)."""
    stmt = (
        pg_insert(DailyActionCounter)
        .values(
            worker_account_id=worker_account_id,
            day=day,
            requests_done=requests,
            follows_done=follows,
        )
        .on_conflict_do_update(
            index_elements=["worker_account_id", "day"],
            set_={
                "requests_done": DailyActionCounter.requests_done + requests,
                "follows_done": DailyActionCounter.follows_done + follows,
            },
        )
    )
    session.execute(stmt)


def read_follows_done(session: Session, worker_account_id: int, day: dt.date) -> int:
    """Follows already committed today for this account. 0 when no row exists yet.

    The follower reads this before EVERY follow, never a cached in-process number:
    the cap must hold across restarts and across concurrent follower instances.
    """
    value = session.scalar(
        select(DailyActionCounter.follows_done).where(
            DailyActionCounter.worker_account_id == worker_account_id,
            DailyActionCounter.day == day,
        )
    )
    return int(value or 0)


def _mark_account(
    worker_account_id: int, *, status: str | None, error: str, event: str
) -> None:
    with session_scope() as session:
        values: dict[str, object] = {"last_error": error[:1000]}
        if status:
            values["status"] = status
        session.execute(
            update(WorkerAccount).where(WorkerAccount.id == worker_account_id).values(**values)
        )
        session.add(
            AccountEvent(
                worker_account_id=worker_account_id,
                event_type=event,
                detail=error[:2000],
            )
        )


def log_account_event(
    worker_account_id: int, event_type: str, detail: str, *, session: Session | None = None
) -> None:
    """Append to the incident audit trail without touching account status."""
    event = AccountEvent(
        worker_account_id=worker_account_id,
        event_type=event_type,
        detail=detail[:2000],
    )
    if session is not None:
        session.add(event)
        return
    with session_scope() as own:
        own.add(event)


def latest_event_at(
    session: Session, worker_account_id: int, event_type: str
) -> dt.datetime | None:
    """Timestamp of the most recent event of this type, or None.

    This is how a follower pause survives a restart: the pause is not held in memory,
    it is derived from the last `feedback_required` row in `account_events`.
    """
    occurred = session.scalar(
        select(AccountEvent.occurred_at)
        .where(
            AccountEvent.worker_account_id == worker_account_id,
            AccountEvent.event_type == event_type,
        )
        .order_by(AccountEvent.occurred_at.desc())
        .limit(1)
    )
    if occurred is None:
        return None
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=dt.UTC)
    return occurred


def account_timezone(device_settings: dict | None) -> dt.tzinfo:
    """The account's claimed timezone, derived from its immutable device_settings.

    Humans sleep, so the follower only acts inside a local-time window (SPEC 7.7).
    device_settings carry either an IANA name (`timezone`) or the Instagram-style
    `timezone_offset` in seconds. Falls back to UTC - never regenerate device
    settings just to add one (SPEC section 8).
    """
    settings = device_settings or {}

    name = settings.get("timezone") or settings.get("tz")
    if isinstance(name, str) and name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 - unknown zone falls through to the offset
            pass

    offset = settings.get("timezone_offset")
    if offset is not None:
        try:
            return dt.timezone(dt.timedelta(seconds=int(offset)))
        except (TypeError, ValueError, OverflowError):
            pass

    return dt.UTC
