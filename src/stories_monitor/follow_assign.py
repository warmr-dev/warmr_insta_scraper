"""Who follows whom: claiming targets, and freeing them when a session dies.

The rule this module exists to enforce: **a target is never stranded**. If the
session that owned 256 follows dies, those 256 rows go back in the pool and the
surviving sessions pick them up. Nothing is lost, nothing needs a human, and no
target quietly stops being monitored because the account watching it expired.

Ownership is a nullable column (`session_follows.session_username`), so:

    release  = UPDATE ... SET session_username = NULL
    claim    = UPDATE ... SET session_username = :me WHERE session_username IS NULL

Both are single statements, which matters more than it looks: claiming with
`FOR UPDATE SKIP LOCKED` means two collectors racing for the same free row
cannot both win, so a target is never followed twice from different sessions
(wasted cap) or zero times (a monitoring hole).

Instagram caps an account at 7,500 followings, so claims are also bounded per
session - a session at its ceiling stops claiming rather than burning follows on
requests Instagram will reject anyway.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .db.models import Cookie, SessionFollow
from .db.session import session_scope
from .logging_setup import get_logger

log = get_logger(__name__)

__all__ = [
    "AssignmentStats",
    "claim_targets",
    "follow_ceiling",
    "mark_result",
    "reclaim_dead_sessions",
    "reclaim_stale_claims",
    "release_session",
    "stats",
]

# Instagram's hard ceiling on followings per account. Claiming past it only
# produces rejected follows, so it is a claim-time limit, not a follow-time one.
FOLLOW_CEILING = 7500

# Leave room under the ceiling: an account pinned at exactly 7,500 looks managed,
# and it has no space left for a human to use it normally.
CEILING_HEADROOM = 300

# A row claimed but not acted on within this long was almost certainly held by a
# process that died between claim and follow. Sweep it back.
STALE_CLAIM_SEC = 3600

# Give up on a target after this many failures - it is renamed or deleted, and
# retrying forever spends real follow budget on nothing.
MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class AssignmentStats:
    """A snapshot of the pool, for the dashboard and the CLI."""

    total: int
    free: int
    claimed: int
    following: int
    requested: int
    failed: int
    unavailable: int

    @property
    def done(self) -> int:
        return self.following + self.requested

    @property
    def remaining(self) -> int:
        return self.free + self.claimed


def follow_ceiling() -> int:
    """Most targets one session should ever hold."""
    return FOLLOW_CEILING - CEILING_HEADROOM


def claim_targets(session_username: str, limit: int) -> list[tuple[int, str]]:
    """Take up to `limit` free targets for this session.

    Returns (user_id, username) pairs, already marked `claimed` and owned by
    this session, so a concurrent collector cannot take them too.

    `SKIP LOCKED` is what makes this safe to run from several processes at once:
    a row another transaction is mid-claim on is stepped over rather than waited
    for, so collectors never serialise behind each other.
    """
    if limit <= 0:
        return []

    now = dt.datetime.now(dt.UTC)
    with session_scope() as session:
        held = session.scalar(
            select(func.count())
            .select_from(SessionFollow)
            .where(
                SessionFollow.session_username == session_username,
                SessionFollow.state.in_(("claimed", "following", "requested")),
            )
        ) or 0
        room = follow_ceiling() - int(held)
        if room <= 0:
            log.info("claim_at_ceiling", session=session_username, held=int(held))
            return []

        rows = session.execute(
            select(SessionFollow.target_user_id, SessionFollow.username)
            .where(
                SessionFollow.session_username.is_(None),
                SessionFollow.state == "free",
                SessionFollow.attempts < MAX_ATTEMPTS,
            )
            # Public accounts first: a private one yields only a pending request
            # whose stories stay invisible until a human approves, and requesting
            # is a stronger spam signal than following. Least-attempted next, so
            # a target that failed once does not block fresh ones.
            .order_by(
                SessionFollow.is_private,
                SessionFollow.attempts,
                SessionFollow.target_user_id,
            )
            .limit(min(limit, room))
            .with_for_update(skip_locked=True)
        ).all()
        if not rows:
            return []

        ids = [int(r[0]) for r in rows]
        session.execute(
            update(SessionFollow)
            .where(SessionFollow.target_user_id.in_(ids))
            .values(session_username=session_username, state="claimed", claimed_at=now)
        )
        log.info("targets_claimed", session=session_username, count=len(ids))
        return [(int(r[0]), str(r[1])) for r in rows]


def mark_result(
    target_user_id: int,
    *,
    state: str,
    error: str | None = None,
    by_session: str | None = None,
) -> None:
    """Record the outcome of one follow attempt.

    `following` / `requested` are terminal successes. `free` hands the row back
    without blaming the target - used when the SESSION failed (throttled,
    blocked), because the target is fine and should go to somebody else.
    """
    now = dt.datetime.now(dt.UTC)
    values: dict[str, object] = {"state": state, "last_error": (error or None) and error[:1000]}

    if state in ("following", "requested"):
        values["followed_at"] = now
        values["last_checked_at"] = now
        values["is_checking"] = False
        if by_session:
            # Denormalised so the dashboard can say who follows this account
            # without joining, and so it survives the row being released.
            values["followed_by"] = by_session
    elif state == "free":
        # The session failed, not the target: release ownership, do not count an
        # attempt against the target, or a bad session would exhaust every row's
        # retries before anyone noticed.
        values["session_username"] = None
        values["claimed_at"] = None
        values["is_checking"] = False

    with session_scope() as session:
        if state == "failed":
            # One statement, so the retry decision reads the SAME attempts value
            # it writes. Splitting this in two let a concurrent update land in
            # between and either strand the row or grant it an extra attempt.
            #
            # Below the ceiling the target deserves another try from whichever
            # session is free later, so it goes back to the pool; at the ceiling
            # it stays `failed` and is not claimed again.
            next_attempts = SessionFollow.attempts + 1
            session.execute(
                update(SessionFollow)
                .where(SessionFollow.target_user_id == target_user_id)
                .values(
                    attempts=next_attempts,
                    last_error=values["last_error"],
                    last_attempt_by=by_session or SessionFollow.session_username,
                    state=case(
                        (next_attempts < MAX_ATTEMPTS, "free"),
                        else_="failed",
                    ),
                    session_username=case(
                        (next_attempts < MAX_ATTEMPTS, None),
                        else_=SessionFollow.session_username,
                    ),
                    claimed_at=case(
                        (next_attempts < MAX_ATTEMPTS, None),
                        else_=SessionFollow.claimed_at,
                    ),
                    is_checking=False,
                )
            )
        elif state == "unavailable":
            # The target is private, deleted or renamed. Retrying cannot help,
            # so it is terminal and keeps no owner.
            session.execute(
                update(SessionFollow)
                .where(SessionFollow.target_user_id == target_user_id)
                .values(
                    state="unavailable",
                    is_checking=False,
                    attempts=SessionFollow.attempts + 1,
                    last_attempt_by=by_session or SessionFollow.session_username,
                    session_username=None,
                    claimed_at=None,
                    last_error=values["last_error"],
                )
            )
        else:
            session.execute(
                update(SessionFollow)
                .where(SessionFollow.target_user_id == target_user_id)
                .values(**values)
            )


def release_session(session_username: str, *, reason: str = "session died") -> int:
    """Free every unfinished follow this session owns. Returns how many.

    This is the answer to "one account with 256 followings is dead": those rows
    are handed back to the pool in one statement and any live session can claim
    them on its next cycle.

    Completed follows (`following` / `requested`) are deliberately NOT released.
    The session really is following those targets, and if the session ever comes
    back the work still counts; if it does not, `reap_session` is the stronger
    call that re-queues them for somebody else.
    """
    with session_scope() as session:
        result = session.execute(
            update(SessionFollow)
            .where(
                SessionFollow.session_username == session_username,
                SessionFollow.state.in_(("claimed", "failed")),
            )
            .values(
                session_username=None,
                state="free",
                claimed_at=None,
                is_checking=False,
                last_error=reason[:1000],
            )
        )
        freed = int(result.rowcount or 0)
    if freed:
        log.info("session_follows_released", session=session_username, freed=freed, reason=reason)
    return freed


def reap_session(session_username: str, *, reason: str = "session dead") -> int:
    """Free EVERYTHING this session held, including follows it had completed.

    Used when a session is confirmed dead rather than merely resting. Its
    completed follows are worthless now - nobody can read those stories through
    a dead cookie - so the targets go back in the pool for a live session to
    follow afresh. This is the 256-followings case in full.
    """
    with session_scope() as session:
        result = session.execute(
            update(SessionFollow)
            .where(SessionFollow.session_username == session_username)
            .values(
                session_username=None,
                state="free",
                claimed_at=None,
                followed_at=None,
                is_checking=False,
                # The dead session no longer follows this target; clearing it
                # stops the dashboard showing a corpse as the owner.
                followed_by=None,
                # Reset attempts: earlier failures were this dead session's
                # problem and should not count against the target's budget with
                # a healthy session.
                attempts=0,
                last_error=reason[:1000],
            )
        )
        freed = int(result.rowcount or 0)

        # A row this session FAILED was already released, so it no longer names
        # the session in `session_username` and the update above cannot see it -
        # yet it still carries attempts spent on a session that turned out to be
        # dying. `last_attempt_by` remembers who burned them, so those attempts
        # can be forgiven without also forgiving failures under healthy sessions
        # (which really do indicate a dead target).
        session.execute(
            update(SessionFollow)
            .where(
                SessionFollow.last_attempt_by == session_username,
                SessionFollow.session_username.is_(None),
                SessionFollow.attempts > 0,
            )
            .values(state="free", attempts=0, last_error=reason[:1000])
        )
    if freed:
        log.info("session_follows_reaped", session=session_username, freed=freed, reason=reason)
    return freed


def reclaim_dead_sessions() -> int:
    """Free the follows of every session no longer active. Returns rows freed.

    Runs on a timer, so a session that died while nobody was watching still gets
    its targets redistributed. Idempotent: a session already reaped owns nothing
    and contributes zero.
    """
    with session_scope() as session:
        dead = [
            str(u)
            for u in session.scalars(
                select(SessionFollow.session_username)
                .distinct()
                .where(SessionFollow.session_username.is_not(None))
                .where(
                    SessionFollow.session_username.not_in(
                        select(Cookie.username).where(Cookie.is_active.is_(True))
                    )
                )
            ).all()
        ]

    total = 0
    for username in dead:
        total += reap_session(username, reason="session no longer active")
    if total:
        log.info("dead_sessions_reclaimed", sessions=len(dead), freed=total)
    return total


def reclaim_stale_claims(older_than_sec: int = STALE_CLAIM_SEC) -> int:
    """Free rows claimed long ago but never acted on.

    A process killed between claim and follow leaves its rows reserved forever
    otherwise - a slow leak that would gradually starve the pool.
    """
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=older_than_sec)
    with session_scope() as session:
        result = session.execute(
            update(SessionFollow)
            .where(
                SessionFollow.state == "claimed",
                SessionFollow.claimed_at.is_not(None),
                SessionFollow.claimed_at < cutoff,
            )
            # is_checking too: a process killed mid-follow would otherwise leave
            # the dashboard claiming that session is still working on this row.
            .values(session_username=None, state="free", claimed_at=None, is_checking=False)
        )
        freed = int(result.rowcount or 0)
    if freed:
        log.info("stale_claims_reclaimed", freed=freed)
    return freed


def stats() -> AssignmentStats:
    """Pool counts by state."""
    with session_scope() as session:
        rows = session.execute(
            select(SessionFollow.state, func.count()).group_by(SessionFollow.state)
        ).all()
    by_state = {str(s): int(c) for s, c in rows}
    return AssignmentStats(
        total=sum(by_state.values()),
        free=by_state.get("free", 0),
        claimed=by_state.get("claimed", 0),
        following=by_state.get("following", 0),
        requested=by_state.get("requested", 0),
        failed=by_state.get("failed", 0),
        unavailable=by_state.get("unavailable", 0),
    )


def begin_check(target_user_id: int, session_username: str) -> None:
    """Mark a target as being worked on right now, by this session.

    Feeds the dashboard's live view. Best-effort: a failure here must never stop
    a follow, and `is_checking` is cleared by every outcome path anyway (plus by
    the stale-claim sweep, so a killed process cannot leave a row stuck 'true').
    """
    try:
        with session_scope() as session:
            session.execute(
                update(SessionFollow)
                .where(SessionFollow.target_user_id == target_user_id)
                .values(
                    is_checking=True,
                    followed_by=session_username,
                    last_checked_at=dt.datetime.now(dt.UTC),
                )
            )
    except Exception as exc:  # noqa: BLE001 - a status flag is not worth a cycle
        log.warning("begin_check_failed", target=target_user_id, error=str(exc)[:120])


def enqueue_targets(
    rows: list[tuple[int, str]] | list[dict[str, object]], batch_size: int = 1000
) -> int:
    """Put targets into the pool as free rows. Idempotent.

    Accepts either `(user_id, username)` pairs or dicts carrying the richer
    columns the import file provides (`full_name`, `is_private`,
    `source_account`).

    `ON CONFLICT DO NOTHING` means re-importing the same file never resets a
    target that is already followed - the import is a merge, not a reset.
    """
    inserted = 0
    with session_scope() as session:
        for start in range(0, len(rows), batch_size):
            chunk = rows[start : start + batch_size]
            if not chunk:
                continue
            values = [
                (
                    {"target_user_id": r[0], "username": r[1], "state": "free"}
                    if isinstance(r, tuple)
                    else {"state": "free", **r}
                )
                for r in chunk
            ]
            stmt = (
                pg_insert(SessionFollow)
                .values(values)
                .on_conflict_do_nothing(index_elements=["target_user_id"])
                # RETURNING, because `rowcount` on a multi-row INSERT ... ON
                # CONFLICT comes back as -1 and would report a negative count.
                .returning(SessionFollow.target_user_id)
            )
            inserted += len(session.execute(stmt).fetchall())
    log.info("targets_enqueued", offered=len(rows), inserted=inserted)
    return inserted
