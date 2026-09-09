"""Follows targets from cookie sessions, at a human rhythm.

Ties three pieces together:

- `follow_assign` decides WHICH targets this session owns, and hands them back
  to the pool when the session dies.
- `follow_rhythm` decides WHEN, modelling a person with a phone rather than a
  scheduler: bursts, long rests, sleep, per-session daily budgets.
- `WebTransport.user_follow` performs the single write, over the same warm HTTP/2
  connection the reads already use.

Deliberately NOT the existing `workers/follower.py`. That one drives
`worker_accounts` (password logins, mobile API) and is left untouched; this one
drives `cookies` (session tokens, web API), which is the fleet that actually
runs. They can coexist.

The loop is resumable at the granularity of one follow: every outcome is
committed before the next attempt, so killing the process loses at most nothing
and never double-follows.
"""

from __future__ import annotations

import datetime as dt
import random
import threading
import time
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from .. import activity, follow_assign
from ..config import get_settings
from ..follow_rhythm import (
    Verdict,
    daily_budget,
    in_waking_hours,
    next_gap_sec,
    plan_burst,
    seconds_until_waking,
)
from ..logging_setup import get_logger
from ..metrics import record_metric
from ..transport.base import (
    ChallengeRequiredError,
    FeedbackRequiredError,
    LoginRequiredError,
    PrivateAccountError,
    RateLimitedError,
    TransportError,
    UserNotFoundError,
)
from ..webaccounts import WebAccount, load_accounts, mark_failed, note_error

log = get_logger(__name__)

# How many targets to hold claimed at once. Small: a claim is a reservation, and
# a session holding thousands it will not reach today keeps them from sessions
# that would.
CLAIM_CHUNK = 25

# After an action block, this session does not write again for a day or two.
# Reads deliberately continue - they are far safer, and stopping them would cost
# story collection for a problem that only concerns writes.
BLOCK_REST_MIN_SEC = 24 * 3600
BLOCK_REST_MAX_SEC = 48 * 3600

# A throttle is minutes, not days.
THROTTLE_REST_MIN_SEC = 15 * 60
THROTTLE_REST_MAX_SEC = 60 * 60


@dataclass
class SessionState:
    """One session's follow rhythm for today. In memory; the DB holds the truth."""

    username: str
    budget: int = 0
    spent: int = 0
    day: dt.date | None = None
    burst_size: int = 0
    burst_position: int = 0
    next_action_at: float = 0.0
    blocked_until: float = 0.0
    queue: list[tuple[int, str]] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.spent)


class SessionFollower:
    """Runs follows across every live cookie session."""

    def __init__(self, timezone: str = "America/Los_Angeles") -> None:
        self._settings = get_settings()
        self._tz = ZoneInfo(timezone)
        self._state: dict[str, SessionState] = {}
        self._stopped = threading.Event()

    # --- rhythm ---

    def _local_now(self) -> dt.datetime:
        return dt.datetime.now(self._tz)

    def _state_for(self, account: WebAccount) -> SessionState:
        state = self._state.get(account.username)
        if state is None:
            state = SessionState(username=account.username)
            self._state[account.username] = state

        today = self._local_now().date()
        if state.day != today:
            # A new day: draw a fresh budget. `follows_done_ever` keeps a young
            # session on its warm-up ramp rather than opening at full rate.
            done_ever = self._follows_done_ever(account.username)
            state.day = today
            state.budget = daily_budget(
                account.username,
                self._local_now(),
                mean_per_day=self._settings.follows_per_day,
                follows_done_ever=done_ever,
            )
            state.spent = 0
            state.burst_size = 0
            state.burst_position = 0
            log.info(
                "follow_budget_drawn",
                session=account.username,
                budget=state.budget,
                follows_done_ever=done_ever,
            )
        return state

    def _follows_done_ever(self, username: str) -> int:
        from sqlalchemy import func, select

        from ..db.models import SessionFollow
        from ..db.session import session_scope

        with session_scope() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(SessionFollow)
                    .where(
                        SessionFollow.session_username == username,
                        SessionFollow.state.in_(("following", "requested")),
                    )
                )
                or 0
            )

    def _should_act(self, state: SessionState) -> tuple[Verdict, float]:
        """Whether this session may follow right now, and how long to wait if not."""
        now = time.monotonic()
        if now < state.blocked_until:
            return Verdict.BLOCKED, state.blocked_until - now

        local = self._local_now()
        if not in_waking_hours(
            local,
            self._settings.follow_window_start_hour,
            self._settings.follow_window_end_hour,
        ):
            return Verdict.ASLEEP, seconds_until_waking(
                local, self._settings.follow_window_start_hour
            )

        if state.remaining <= 0:
            # Done for today; look again after the local day rolls over.
            return Verdict.BUDGET_SPENT, seconds_until_waking(
                local, self._settings.follow_window_start_hour
            )

        if now < state.next_action_at:
            return Verdict.RESTING, state.next_action_at - now

        return Verdict.GO, 0.0

    # --- one follow ---

    def follow_one(self, account: WebAccount) -> bool:
        """Attempt exactly one follow for one session. True if a follow landed."""
        state = self._state_for(account)
        verdict, wait = self._should_act(state)
        if verdict is not Verdict.GO:
            log.debug(
                "follow_skipped", session=account.username, verdict=str(verdict), wait_sec=round(wait)
            )
            return False

        if not state.queue:
            state.queue = follow_assign.claim_targets(
                account.username, min(CLAIM_CHUNK, state.remaining)
            )
            if not state.queue:
                # Nothing free anywhere: the pool is exhausted or fully owned.
                state.next_action_at = time.monotonic() + random.uniform(300, 900)
                return False

        if state.burst_position >= state.burst_size:
            plan = plan_burst(account.username, self._local_now(), state.remaining)
            state.burst_size = max(1, plan.size)
            state.burst_position = 0

        target_id, target_name = state.queue.pop(0)
        # Flag it before the network call, so the dashboard shows what this
        # session is touching right now rather than only after the fact.
        follow_assign.begin_check(target_id, account.username)
        landed = self._do_follow(account, state, target_id, target_name)

        state.burst_position += 1
        state.next_action_at = time.monotonic() + next_gap_sec(
            account.username,
            self._local_now(),
            position_in_burst=state.burst_position,
            burst_size=state.burst_size,
        )
        return landed

    def _do_follow(
        self, account: WebAccount, state: SessionState, target_id: int, target_name: str
    ) -> bool:
        """The network call and every way it can go wrong."""
        try:
            transport = account.transport()
            followed = transport.user_follow(target_id, username=target_name)

        except FeedbackRequiredError as exc:
            # An action block. Stop writing from this session for a day or two,
            # and give the target back - it is fine, this session is not.
            rest = random.uniform(BLOCK_REST_MIN_SEC, BLOCK_REST_MAX_SEC)
            state.blocked_until = time.monotonic() + rest
            state.queue.clear()
            follow_assign.mark_result(target_id, state="free", error=str(exc)[:500])
            self._release_claims(account.username, "action blocked")
            note_error(account.username, f"follow blocked: {exc}"[:500])
            activity.record(
                account.username,
                "follow",
                status="blocked",
                message=f"action blocked while following {target_name}",
                target_user_id=target_id,
                target_username=target_name,
            )
            record_metric("follow_blocked", 1, {"session": account.username})
            log.warning(
                "follow_action_blocked",
                session=account.username,
                rest_hours=round(rest / 3600, 1),
            )
            return False

        except ChallengeRequiredError as exc:
            # A human must clear this. The session is not usable for writes and
            # probably not for reads either.
            state.blocked_until = time.monotonic() + BLOCK_REST_MAX_SEC
            follow_assign.mark_result(target_id, state="free", error=str(exc)[:500])
            self._release_claims(account.username, "checkpoint")
            mark_failed(account.username, f"checkpoint during follow: {exc}"[:500])
            log.error("follow_checkpoint", session=account.username, error=str(exc)[:200])
            return False

        except LoginRequiredError as exc:
            # The session is dead. This is the 256-followings case: everything it
            # owned goes back to the pool for the live sessions to pick up.
            #
            # Release this target FIRST, so it is unowned before the sweep runs;
            # `mark_failed` then deactivates the session and reaps the rest. The
            # reaping lives there rather than here because a session can equally
            # be found dead by the collector on a read, and both routes must free
            # the follows.
            state.queue.clear()
            follow_assign.mark_result(target_id, state="free", error=str(exc)[:500])
            mark_failed(account.username, f"session dead during follow: {exc}"[:500])
            log.error("follow_session_dead", session=account.username)
            return False

        except RateLimitedError as exc:
            # Minutes, not days, and the target keeps its attempt count clean.
            rest = random.uniform(THROTTLE_REST_MIN_SEC, THROTTLE_REST_MAX_SEC)
            state.next_action_at = time.monotonic() + rest
            follow_assign.mark_result(target_id, state="free", error=str(exc)[:500])
            log.info(
                "follow_throttled", session=account.username, rest_min=round(rest / 60)
            )
            return False

        except (PrivateAccountError, UserNotFoundError) as exc:
            # The target, not the session. Terminal - retrying cannot help.
            follow_assign.mark_result(
                target_id, state="unavailable", error=str(exc)[:500], by_session=account.username
            )
            return False

        except TransportError as exc:
            follow_assign.mark_result(
                target_id, state="failed", error=str(exc)[:500], by_session=account.username
            )
            log.warning(
                "follow_failed", session=account.username, target=target_id, error=str(exc)[:200]
            )
            activity.record(
                account.username,
                "follow",
                status="error",
                message=str(exc)[:200],
                target_user_id=target_id,
                target_username=target_name,
            )
            return False

        # `user_follow` reports True for a landed follow OR a pending request to
        # a private target. Both consumed real budget and must not be retried.
        follow_assign.mark_result(
            target_id,
            state="following" if followed else "requested",
            by_session=account.username,
        )
        state.spent += 1
        record_metric("follows_done", 1, {"session": account.username})
        activity.record(
            account.username,
            "follow",
            message=f"followed {target_name}",
            target_user_id=target_id,
            target_username=target_name,
        )
        log.info(
            "follow_done",
            session=account.username,
            target=target_id,
            spent=state.spent,
            budget=state.budget,
        )
        return True

    def _release_claims(self, username: str, reason: str) -> None:
        """Hand back everything this session claimed but has not acted on."""
        try:
            follow_assign.release_session(username, reason=reason)
        except Exception as exc:  # noqa: BLE001 - releasing must never kill the loop
            log.warning("release_failed", session=username, error=str(exc)[:200])

    # --- loop ---

    def run_once(self) -> int:
        """One pass over every live session. Returns follows performed."""
        # Sweep first: sessions that died since the last pass, and rows a killed
        # process left reserved, both become available before anyone claims.
        follow_assign.reclaim_dead_sessions()
        follow_assign.reclaim_stale_claims()

        accounts = load_accounts()
        if not accounts:
            log.info("no_live_sessions")
            return 0

        # Shuffled so the same session is not always first to the free pool.
        random.shuffle(accounts)
        done = 0
        for account in accounts:
            if self._stopped.is_set():
                break
            try:
                if self.follow_one(account):
                    done += 1
            except Exception as exc:  # noqa: BLE001 - one bad session must not stop the fleet
                log.warning(
                    "session_follow_error", session=account.username, error=str(exc)[:200]
                )
        return done

    def run(self, tick_sec: float = 30.0) -> None:
        """Run until stopped."""
        log.info("session_follower_started", follows_per_day=self._settings.follows_per_day)
        while not self._stopped.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - the loop outlives any one failure
                log.error("session_follower_cycle_failed", error=str(exc)[:300])
            # Jittered so the fleet does not tick in lockstep.
            self._stopped.wait(tick_sec * random.uniform(0.75, 1.25))

    def stop(self) -> None:
        self._stopped.set()
