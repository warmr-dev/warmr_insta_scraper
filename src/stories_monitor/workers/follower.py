"""The follower (SPEC 7.7) - the long pole.

Bootstrapping ~57,000 follows at a safe ~150/day/account takes 6-10 weeks. This
process must therefore be a long-running, fully resumable job: killing it mid-run
loses nothing and, critically, must not exceed the daily cap after a restart.

Every rate-limit decision is read from `daily_action_counters` (SPEC section 5),
never from an in-process counter. The counter is incremented in the SAME
transaction as the `target_follows` state change, so the ledger and the follow
graph can never disagree - a crash either commits both or neither.
"""

from __future__ import annotations

import datetime as dt
import random
import time
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import func, select, update

from ..config import get_settings
from ..crypto import SecretBox
from ..db.models import Target, TargetFollow, WorkerAccount
from ..db.session import session_scope
from ..logging_setup import get_logger
from ..metrics import record_metric
from ..transport import (
    ChallengeRequiredError,
    FeedbackRequiredError,
    InstagramTransport,
    LoginRequiredError,
    PleaseWaitError,
    PrivateAccountError,
    ProxyBlockedError,
    RateLimitedError,
    TransportError,
    UserNotFoundError,
    get_transport,
)
from ._common import (
    _bump_counter,
    _mark_account,
    account_timezone,
    latest_event_at,
    read_follows_done,
)

log = get_logger(__name__)

# A follow that keeps failing is almost always a dead/renamed target, not a transient
# fault. Give up rather than burn cap on it forever.
MAX_FOLLOW_ATTEMPTS = 3

# SPEC 7.7: feedback_required stops that account's FOLLOWS for 24h. Reading (the
# poller) continues - reading is much safer than writing.
FEEDBACK_PAUSE_SEC = 24 * 3600

# The friendship sweep is deliberately slow: it is a write-adjacent read against
# targets we already requested, and there is no hurry to notice an approval.
SWEEP_BATCH_SIZE = 25
SWEEP_GAP_MIN_SEC = 20.0
SWEEP_GAP_MAX_SEC = 60.0


class FollowOutcome(str, Enum):
    """Why one follow attempt ended. Tests drive `follow_one` and assert on these."""

    FOLLOWED = "followed"           # new follow committed
    REQUESTED = "requested"         # private target, outgoing request pending
    ALREADY = "already"             # user_follow() returned False - not an error
    CAP_REACHED = "cap_reached"     # daily ledger says stop
    OUTSIDE_WINDOW = "outside_window"
    PAUSED = "paused"               # feedback_required cooldown still active
    NO_WORK = "no_work"             # no queued rows for this account
    FAILED = "failed"               # attempt limit exhausted -> state 'failed'
    RETRY = "retry"                 # transient, row stays 'queued'
    ACCOUNT_LOST = "account_lost"   # challenge/proxy block - warden takes over


@dataclass(slots=True)
class FollowStep:
    """Result of one `follow_one` call."""

    worker_account_id: int
    outcome: FollowOutcome
    target_user_id: int | None = None
    follows_done_today: int = 0
    detail: str | None = None

    @property
    def consumed_cap(self) -> bool:
        return self.outcome in (FollowOutcome.FOLLOWED, FollowOutcome.REQUESTED)


@dataclass(slots=True)
class ShardProgress:
    """Weekly-status numbers for one shard (SPEC 7.7)."""

    shard_id: int
    active_workers: int
    follows_completed: int
    follows_remaining: int
    follows_failed: int
    daily_capacity: int
    days_remaining: float | None
    projected_completion: dt.date | None


# --- follow window ------------------------------------------------------------


def in_follow_window(now_utc: dt.datetime, tz: dt.tzinfo, start_hour: int, end_hour: int) -> bool:
    """True when the account's *claimed local* time is inside the operating window.

    Humans sleep (SPEC 7.7). A window that wraps midnight (e.g. 22->6) is supported.
    """
    local_hour = now_utc.astimezone(tz).hour
    if start_hour == end_hour:
        return True  # degenerate config means "always"
    if start_hour < end_hour:
        return start_hour <= local_hour < end_hour
    return local_hour >= start_hour or local_hour < end_hour


def seconds_until_window(
    now_utc: dt.datetime, tz: dt.tzinfo, start_hour: int, end_hour: int
) -> float:
    """How long to sleep before the window reopens. Bounded so the loop stays responsive."""
    if in_follow_window(now_utc, tz, start_hour, end_hour):
        return 0.0
    local = now_utc.astimezone(tz)
    target = local.replace(hour=start_hour % 24, minute=0, second=0, microsecond=0)
    if target <= local:
        target += dt.timedelta(days=1)
    # Cap the sleep so config changes and stop() are picked up within the hour.
    return min((target - local).total_seconds(), 3600.0)


class Follower:
    """Drives follows for the active worker accounts, one safe step at a time.

    `follow_one` / `run_once` / `sweep_requested` are all independently callable so
    tests can drive single steps without entering `run`'s loop.
    """

    def __init__(self, transports: dict[int, InstagramTransport] | None = None):
        self.settings = get_settings()
        self._transports: dict[int, InstagramTransport] = transports or {}
        self._stopped = False
        # Per-account earliest next follow time (monotonic). Purely a pacing aid -
        # correctness never depends on it; the DB ledger is the authority.
        self._next_follow_at: dict[int, float] = {}

    # --- transport lifecycle (one account = one transport = one identity) ---

    def transport_for(self, worker_account_id: int) -> InstagramTransport:
        """Restore the saved session rather than logging in.

        Repeated logins are the single strongest ban signal (SPEC section 8), and the
        device settings / proxy are whatever the account was created with - never
        regenerated, never rotated.
        """
        existing = self._transports.get(worker_account_id)
        if existing is not None:
            return existing

        with session_scope() as session:
            account = session.get(WorkerAccount, worker_account_id)
            if account is None:
                raise RuntimeError(f"worker account {worker_account_id} not found")
            username = account.username
            device_settings = dict(account.device_settings or {})
            proxy_url = account.proxy_url
            session_json = dict(account.session_json) if account.session_json else None
            password_enc = account.password_enc

        transport = get_transport(
            username=username,
            device_settings=device_settings,
            proxy_url=proxy_url,
        )
        if session_json:
            transport.load_session(session_json)
        else:
            password = SecretBox().decrypt(password_enc)
            dumped = transport.login(username, password)
            with session_scope() as session:
                session.execute(
                    update(WorkerAccount)
                    .where(WorkerAccount.id == worker_account_id)
                    .values(
                        session_json=dumped,
                        last_login_at=dt.datetime.now(dt.UTC),
                    )
                )
        self._transports[worker_account_id] = transport
        return transport

    def _drop_transport(self, worker_account_id: int) -> None:
        transport = self._transports.pop(worker_account_id, None)
        if transport is not None:
            try:
                transport.close()
            except Exception:  # noqa: BLE001 - closing must never break the loop
                pass

    # --- gates: every one of these is answered from the database ---

    def _paused_until(self, session, worker_account_id: int) -> dt.datetime | None:
        """Feedback-required cooldown, derived from `account_events`.

        Persisting the pause as an event row (rather than in memory) is what makes it
        survive a restart: the last `feedback_required` event plus 24h IS the pause.
        """
        last = latest_event_at(session, worker_account_id, "feedback_required")
        if last is None:
            return None
        until = last + dt.timedelta(seconds=FEEDBACK_PAUSE_SEC)
        now = dt.datetime.now(dt.UTC)
        return until if until > now else None

    def _claim_next_target(self, session, worker_account_id: int) -> int | None:
        """Take one `queued` row for this account, skipping rows another worker holds.

        SKIP LOCKED means two follower instances never hand the same target to the
        same account twice.
        """
        row = session.execute(
            select(TargetFollow.target_user_id)
            .where(
                TargetFollow.worker_account_id == worker_account_id,
                TargetFollow.state == "queued",
                TargetFollow.attempts < MAX_FOLLOW_ATTEMPTS,
            )
            .order_by(TargetFollow.target_user_id)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).first()
        return int(row[0]) if row else None

    # --- one follow ---

    def follow_one(self, worker_account_id: int) -> FollowStep:
        """Attempt exactly one follow for one account. The unit of resumability.

        Order matters: the cap is re-read from the ledger immediately before the
        network call, and the ledger increment is committed in the same transaction
        as the state change immediately after it.
        """
        settings = self.settings
        now = dt.datetime.now(dt.UTC)

        with session_scope() as session:
            account = session.get(WorkerAccount, worker_account_id)
            if account is None or account.status != "active":
                return FollowStep(worker_account_id, FollowOutcome.ACCOUNT_LOST,
                                  detail="account not active")
            tz = account_timezone(account.device_settings)
            day = now.astimezone(tz).date()

            paused_until = self._paused_until(session, worker_account_id)
            if paused_until is not None:
                return FollowStep(
                    worker_account_id,
                    FollowOutcome.PAUSED,
                    detail=f"feedback_required until {paused_until.isoformat()}",
                )

            if not in_follow_window(
                now, tz, settings.follow_window_start_hour, settings.follow_window_end_hour
            ):
                return FollowStep(worker_account_id, FollowOutcome.OUTSIDE_WINDOW)

            # Hard cap, read fresh from the table before EVERY follow (SPEC 7.7).
            done = read_follows_done(session, worker_account_id, day)
            if done >= settings.follows_per_day:
                return FollowStep(
                    worker_account_id, FollowOutcome.CAP_REACHED, follows_done_today=done
                )

            target_user_id = self._claim_next_target(session, worker_account_id)
            if target_user_id is None:
                return FollowStep(
                    worker_account_id, FollowOutcome.NO_WORK, follows_done_today=done
                )

        # Network call happens OUTSIDE the transaction - never hold a row lock across
        # an Instagram request.
        try:
            created = self.transport_for(worker_account_id).user_follow(target_user_id)
        except PrivateAccountError:
            # A private target that accepted an outgoing request: treat exactly like
            # a successful request, cap included.
            return self._commit_follow(
                worker_account_id, target_user_id, day, state="requested",
                outcome=FollowOutcome.REQUESTED,
            )
        except UserNotFoundError as exc:
            return self._fail_row(worker_account_id, target_user_id, str(exc), terminal=True)
        except Exception as exc:  # noqa: BLE001 - classified below
            return self._handle_follow_error(worker_account_id, target_user_id, exc)

        if created:
            # user_follow() returns True on a NEW follow or a NEW outgoing request.
            # Private targets land in 'requested' and are promoted by the sweep.
            state, outcome = self._state_for_new_follow(target_user_id)
            return self._commit_follow(
                worker_account_id, target_user_id, day, state=state, outcome=outcome
            )

        # False means already following or already pending - NOT an error (SPEC 7.7).
        # Settle the row so it is never retried, and do not spend daily cap on it.
        return self._commit_already(worker_account_id, target_user_id)

    def _state_for_new_follow(self, target_user_id: int) -> tuple[str, FollowOutcome]:
        """Private targets go to 'requested'; public ones straight to 'following'."""
        with session_scope() as session:
            status = session.scalar(
                select(Target.status).where(Target.user_id == target_user_id)
            )
        if status == "private":
            return "requested", FollowOutcome.REQUESTED
        return "following", FollowOutcome.FOLLOWED

    # --- commits: state change + ledger increment, always one transaction ---

    def _commit_follow(
        self,
        worker_account_id: int,
        target_user_id: int,
        day: dt.date,
        *,
        state: str,
        outcome: FollowOutcome,
    ) -> FollowStep:
        """The resumability guarantee lives here.

        `target_follows.state` and `daily_action_counters.follows_done` move together
        in one transaction. A crash before commit leaves the row 'queued' and the
        counter untouched (we retry, cap intact); a crash after commit leaves both
        advanced (we never re-follow, cap already spent).
        """
        now = dt.datetime.now(dt.UTC)
        with session_scope() as session:
            values: dict[str, object] = {
                "state": state,
                "attempts": TargetFollow.attempts + 1,
                "last_error": None,
                "requested_at": now,
            }
            if state == "following":
                values["confirmed_at"] = now
            session.execute(
                update(TargetFollow)
                .where(
                    TargetFollow.worker_account_id == worker_account_id,
                    TargetFollow.target_user_id == target_user_id,
                )
                .values(**values)
            )
            _bump_counter(session, worker_account_id, day, follows=1)
            if state == "following":
                session.execute(
                    update(WorkerAccount)
                    .where(WorkerAccount.id == worker_account_id)
                    .values(follows_count=WorkerAccount.follows_count + 1)
                )
            done = read_follows_done(session, worker_account_id, day)

        record_metric("follows_done", 1, {"worker_account_id": worker_account_id, "state": state})
        log.info(
            "follow_committed",
            worker_account_id=worker_account_id,
            target_user_id=target_user_id,
            state=state,
            follows_done_today=done,
        )
        return FollowStep(
            worker_account_id, outcome, target_user_id=target_user_id, follows_done_today=done
        )

    def _commit_already(self, worker_account_id: int, target_user_id: int) -> FollowStep:
        """user_follow() returned False: already following/pending. Settle, do not retry."""
        now = dt.datetime.now(dt.UTC)
        with session_scope() as session:
            session.execute(
                update(TargetFollow)
                .where(
                    TargetFollow.worker_account_id == worker_account_id,
                    TargetFollow.target_user_id == target_user_id,
                )
                .values(
                    state="following",
                    confirmed_at=now,
                    attempts=TargetFollow.attempts + 1,
                    last_error=None,
                )
            )
        record_metric("follows_already", 1, {"worker_account_id": worker_account_id})
        log.info(
            "follow_already_present",
            worker_account_id=worker_account_id,
            target_user_id=target_user_id,
        )
        return FollowStep(
            worker_account_id, FollowOutcome.ALREADY, target_user_id=target_user_id
        )

    def _fail_row(
        self, worker_account_id: int, target_user_id: int, error: str, *, terminal: bool = False
    ) -> FollowStep:
        """Bump attempts; move to 'failed' when terminal or the attempt limit is hit.

        No cap is consumed - a failed attempt never reached a new follow.
        """
        with session_scope() as session:
            row = session.get(TargetFollow, (worker_account_id, target_user_id))
            attempts = (row.attempts if row else 0) + 1
            give_up = terminal or attempts >= MAX_FOLLOW_ATTEMPTS
            session.execute(
                update(TargetFollow)
                .where(
                    TargetFollow.worker_account_id == worker_account_id,
                    TargetFollow.target_user_id == target_user_id,
                )
                .values(
                    state="failed" if give_up else "queued",
                    attempts=attempts,
                    last_error=error[:1000],
                )
            )

        if give_up:
            record_metric("follows_failed", 1, {"worker_account_id": worker_account_id})
            log.warning(
                "follow_failed",
                worker_account_id=worker_account_id,
                target_user_id=target_user_id,
                attempts=attempts,
                error=error[:200],
            )
            return FollowStep(
                worker_account_id, FollowOutcome.FAILED,
                target_user_id=target_user_id, detail=error[:200],
            )
        return FollowStep(
            worker_account_id, FollowOutcome.RETRY,
            target_user_id=target_user_id, detail=error[:200],
        )

    # --- error classification (mirrors poller._handle_error) ---

    def _handle_follow_error(
        self, worker_account_id: int, target_user_id: int, exc: Exception
    ) -> FollowStep:
        error = str(exc)

        if isinstance(exc, FeedbackRequiredError):
            # SPEC 7.7: stop this account's FOLLOWS for 24h, log the event, keep
            # polling. The event row IS the pause - a restart re-derives it.
            _mark_account(
                worker_account_id, status=None, error=error, event="feedback_required"
            )
            self._release_row(worker_account_id, target_user_id, error)
            record_metric("follow_feedback_required", 1, {"worker_account_id": worker_account_id})
            log.warning(
                "follow_feedback_required",
                worker_account_id=worker_account_id,
                pause_hours=FEEDBACK_PAUSE_SEC // 3600,
                alert=True,
            )
            return FollowStep(
                worker_account_id, FollowOutcome.PAUSED,
                target_user_id=target_user_id, detail=error[:200],
            )

        if isinstance(exc, ChallengeRequiredError):
            # NEVER auto-solve (SPEC 7.8). Hand the account to the warden.
            _mark_account(
                worker_account_id, status="challenged", error=error, event="challenge"
            )
            self._release_row(worker_account_id, target_user_id, error)
            self._drop_transport(worker_account_id)
            log.error("challenge_required", worker_account_id=worker_account_id, alert=True)
            return FollowStep(
                worker_account_id, FollowOutcome.ACCOUNT_LOST,
                target_user_id=target_user_id, detail=error[:200],
            )

        if isinstance(exc, ProxyBlockedError):
            _mark_account(worker_account_id, status="challenged", error=error, event="ban")
            self._release_row(worker_account_id, target_user_id, error)
            self._drop_transport(worker_account_id)
            log.error("proxy_blocked", worker_account_id=worker_account_id, alert=True)
            return FollowStep(
                worker_account_id, FollowOutcome.ACCOUNT_LOST,
                target_user_id=target_user_id, detail=error[:200],
            )

        if isinstance(exc, LoginRequiredError):
            # Warden owns re-login policy; drop the session so it is rebuilt with the
            # SAME device settings and SAME proxy.
            _mark_account(
                worker_account_id, status=None, error=error, event="login_required"
            )
            self._drop_transport(worker_account_id)
            self._release_row(worker_account_id, target_user_id, error)
            return FollowStep(
                worker_account_id, FollowOutcome.RETRY,
                target_user_id=target_user_id, detail=error[:200],
            )

        if isinstance(exc, (PleaseWaitError, RateLimitedError)):
            # Soft limit: leave the row queued, just wait longer before the next one.
            self._release_row(worker_account_id, target_user_id, error)
            self._next_follow_at[worker_account_id] = time.monotonic() + random.uniform(300, 1800)
            log.warning("follow_rate_limited", worker_account_id=worker_account_id)
            return FollowStep(
                worker_account_id, FollowOutcome.RETRY,
                target_user_id=target_user_id, detail=error[:200],
            )

        if isinstance(exc, TransportError):
            return self._fail_row(worker_account_id, target_user_id, error)

        log.exception(
            "follow_unexpected_error",
            worker_account_id=worker_account_id,
            target_user_id=target_user_id,
            error=error[:200],
        )
        return self._fail_row(worker_account_id, target_user_id, error)

    def _release_row(self, worker_account_id: int, target_user_id: int, error: str) -> None:
        """Leave the row 'queued' but record why - the account, not the target, failed."""
        with session_scope() as session:
            session.execute(
                update(TargetFollow)
                .where(
                    TargetFollow.worker_account_id == worker_account_id,
                    TargetFollow.target_user_id == target_user_id,
                )
                .values(state="queued", last_error=error[:1000])
            )

    # --- the slow sweep: 'requested' -> 'following' ---

    def sweep_requested(self, worker_account_id: int, limit: int = SWEEP_BATCH_SIZE) -> int:
        """Promote approved follow requests (SPEC 7.7).

        Deliberately separate from `follow_one` and deliberately slow: a pending
        request costs nothing while it waits, so there is no reason to hammer
        `user_friendship`. Does NOT consume the follow cap - it is a read.
        """
        with session_scope() as session:
            account = session.get(WorkerAccount, worker_account_id)
            if account is None or account.status != "active":
                return 0
            target_ids = [
                int(uid)
                for uid in session.scalars(
                    select(TargetFollow.target_user_id)
                    .where(
                        TargetFollow.worker_account_id == worker_account_id,
                        TargetFollow.state == "requested",
                    )
                    .order_by(TargetFollow.requested_at)
                    .limit(limit)
                ).all()
            ]

        if not target_ids:
            return 0

        promoted = 0
        transport = self.transport_for(worker_account_id)
        for target_user_id in target_ids:
            if self._stopped:
                break
            try:
                friendship = transport.user_friendship(target_user_id)
            except (ChallengeRequiredError, ProxyBlockedError) as exc:
                _mark_account(
                    worker_account_id, status="challenged", error=str(exc), event="challenge"
                )
                self._drop_transport(worker_account_id)
                log.error("sweep_account_lost", worker_account_id=worker_account_id, alert=True)
                break
            except TransportError as exc:
                log.warning(
                    "sweep_friendship_error",
                    worker_account_id=worker_account_id,
                    target_user_id=target_user_id,
                    error=str(exc)[:200],
                )
                continue

            if friendship.get("following"):
                promoted += self._promote_requested(worker_account_id, target_user_id)
            elif not friendship.get("outgoing_request", True):
                # Neither following nor pending: the request was withdrawn or declined.
                self._mark_rejected(worker_account_id, target_user_id)

            time.sleep(random.uniform(SWEEP_GAP_MIN_SEC, SWEEP_GAP_MAX_SEC))

        if promoted:
            record_metric("follows_promoted", promoted, {"worker_account_id": worker_account_id})
            log.info("sweep_promoted", worker_account_id=worker_account_id, promoted=promoted)
        return promoted

    def _promote_requested(self, worker_account_id: int, target_user_id: int) -> int:
        now = dt.datetime.now(dt.UTC)
        with session_scope() as session:
            result = session.execute(
                update(TargetFollow)
                .where(
                    TargetFollow.worker_account_id == worker_account_id,
                    TargetFollow.target_user_id == target_user_id,
                    TargetFollow.state == "requested",
                )
                .values(state="following", confirmed_at=now, last_error=None)
            )
            if result.rowcount:
                session.execute(
                    update(WorkerAccount)
                    .where(WorkerAccount.id == worker_account_id)
                    .values(follows_count=WorkerAccount.follows_count + 1)
                )
            return int(result.rowcount or 0)

    def _mark_rejected(self, worker_account_id: int, target_user_id: int) -> None:
        with session_scope() as session:
            session.execute(
                update(TargetFollow)
                .where(
                    TargetFollow.worker_account_id == worker_account_id,
                    TargetFollow.target_user_id == target_user_id,
                    TargetFollow.state == "requested",
                )
                .values(state="rejected", last_error="request not accepted")
            )

    # --- driving the accounts ---

    def active_worker_ids(self) -> list[int]:
        with session_scope() as session:
            return [
                int(wid)
                for wid in session.scalars(
                    select(WorkerAccount.id)
                    .where(WorkerAccount.status == "active")
                    .order_by(WorkerAccount.id)
                ).all()
            ]

    def run_once(self) -> list[FollowStep]:
        """One pass over every active account. At most one follow per account.

        Returning the steps (rather than logging and discarding them) is what lets a
        test assert the cap and window behaviour without touching the loop.
        """
        steps: list[FollowStep] = []
        now_mono = time.monotonic()
        for worker_account_id in self.active_worker_ids():
            if self._stopped:
                break
            # Randomised gap between follows, per account. No fixed cadence (SPEC 7.7).
            ready_at = self._next_follow_at.get(worker_account_id, 0.0)
            if now_mono < ready_at:
                continue
            step = self.follow_one(worker_account_id)
            steps.append(step)
            if step.consumed_cap:
                self._next_follow_at[worker_account_id] = time.monotonic() + self.follow_gap()
            elif step.outcome in (
                FollowOutcome.NO_WORK,
                FollowOutcome.CAP_REACHED,
                FollowOutcome.OUTSIDE_WINDOW,
                FollowOutcome.PAUSED,
                FollowOutcome.ACCOUNT_LOST,
            ):
                # Nothing to do for a while; re-check in a few minutes, not instantly.
                self._next_follow_at[worker_account_id] = time.monotonic() + random.uniform(
                    240, 600
                )
            else:
                # ALREADY / RETRY / FAILED cost no cap, but still pace like a human.
                self._next_follow_at[worker_account_id] = time.monotonic() + random.uniform(
                    30, 120
                )
        return steps

    def follow_gap(self) -> float:
        """Randomised 4-12 min gap. Real randomness, not a fixed offset (SPEC section 8)."""
        return random.uniform(
            self.settings.follow_gap_min_sec, self.settings.follow_gap_max_sec
        )

    def run(self, sweep_every_sec: float = 3600.0, tick_sec: float = 30.0) -> None:
        """The long-running loop. Every iteration re-reads state from Postgres, so
        killing and restarting the process is indistinguishable from a slow tick."""
        log.info("follower_starting", follows_per_day=self.settings.follows_per_day)
        next_sweep = time.monotonic() + sweep_every_sec

        while not self._stopped:
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                log.exception("follower_tick_error", error=str(exc)[:200])

            if time.monotonic() >= next_sweep:
                try:
                    for worker_account_id in self.active_worker_ids():
                        if self._stopped:
                            break
                        self.sweep_requested(worker_account_id)
                except Exception as exc:  # noqa: BLE001
                    log.exception("follower_sweep_error", error=str(exc)[:200])
                next_sweep = time.monotonic() + sweep_every_sec

            deadline = time.monotonic() + tick_sec
            while not self._stopped and time.monotonic() < deadline:
                time.sleep(min(1.0, deadline - time.monotonic()))

    def stop(self) -> None:
        self._stopped = True


# --- progress reporting (the weekly question) ---------------------------------


def progress_report() -> list[ShardProgress]:
    """Follows completed, remaining, and projected completion date PER SHARD.

    Projection is deliberately simple and honest: remaining work divided by
    `follows_per_day * active workers in that shard`. It assumes today's active
    worker count holds; a shard that loses an account slips, which is exactly the
    signal the project owner wants to see (SPEC 7.7).
    """
    settings = get_settings()
    today = dt.datetime.now(dt.UTC).date()
    reports: list[ShardProgress] = []

    with session_scope() as session:
        active_by_shard = dict(
            session.execute(
                select(WorkerAccount.shard_id, func.count(WorkerAccount.id))
                .where(WorkerAccount.status == "active")
                .group_by(WorkerAccount.shard_id)
            ).all()
        )
        shard_by_worker = dict(
            session.execute(select(WorkerAccount.id, WorkerAccount.shard_id)).all()
        )
        # Follow states rolled up per worker, then folded into shards.
        state_rows = session.execute(
            select(
                TargetFollow.worker_account_id,
                TargetFollow.state,
                func.count(),
            ).group_by(TargetFollow.worker_account_id, TargetFollow.state)
        ).all()

    completed: dict[int, int] = {}
    remaining: dict[int, int] = {}
    failed: dict[int, int] = {}
    for worker_account_id, state, count in state_rows:
        shard_id = shard_by_worker.get(worker_account_id)
        if shard_id is None:
            continue
        if state in ("following", "requested"):
            completed[shard_id] = completed.get(shard_id, 0) + int(count)
        elif state == "queued":
            remaining[shard_id] = remaining.get(shard_id, 0) + int(count)
        elif state in ("failed", "rejected"):
            failed[shard_id] = failed.get(shard_id, 0) + int(count)

    for shard_id in sorted(set(completed) | set(remaining) | set(failed) | set(active_by_shard)):
        workers = int(active_by_shard.get(shard_id, 0))
        left = remaining.get(shard_id, 0)
        capacity = settings.follows_per_day * workers

        if left == 0:
            days: float | None = 0.0
            eta: dt.date | None = today
        elif capacity <= 0:
            # No active workers: the shard is stalled, not "finishing eventually".
            days, eta = None, None
        else:
            days = left / capacity
            eta = today + dt.timedelta(days=int(-(-left // capacity)))

        reports.append(
            ShardProgress(
                shard_id=shard_id,
                active_workers=workers,
                follows_completed=completed.get(shard_id, 0),
                follows_remaining=left,
                follows_failed=failed.get(shard_id, 0),
                daily_capacity=capacity,
                days_remaining=days,
                projected_completion=eta,
            )
        )

    for report in reports:
        record_metric("follow_progress_remaining", report.follows_remaining,
                      {"shard_id": report.shard_id})
        record_metric("follow_progress_completed", report.follows_completed,
                      {"shard_id": report.shard_id})

    return reports


def follows_done_today(worker_account_id: int) -> int:
    """Convenience read of the ledger - used by the CLI and by tests."""
    with session_scope() as session:
        account = session.get(WorkerAccount, worker_account_id)
        tz = account_timezone(account.device_settings) if account else dt.UTC
        day = dt.datetime.now(dt.UTC).astimezone(tz).date()
        return read_follows_done(session, worker_account_id, day)


def run_follower() -> None:
    """Entry point (SPEC section 6): one follower process for the whole fleet."""
    Follower().run()


__all__ = [
    "FollowOutcome",
    "FollowStep",
    "Follower",
    "MAX_FOLLOW_ATTEMPTS",
    "ShardProgress",
    "follows_done_today",
    "in_follow_window",
    "progress_report",
    "run_follower",
    "seconds_until_window",
]
