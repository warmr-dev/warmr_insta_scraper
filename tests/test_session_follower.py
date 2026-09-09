"""The follow worker: rhythm gates, and what each failure does to the pool.

The transport is faked, so these assert the worker's decisions rather than
Instagram's behaviour - which is where the interesting logic actually lives.
"""

from __future__ import annotations

import time

import pytest

from stories_monitor import follow_assign
from stories_monitor.db.models import Cookie, SessionFollow
from stories_monitor.transport.base import (
    ChallengeRequiredError,
    FeedbackRequiredError,
    LoginRequiredError,
    RateLimitedError,
    UserNotFoundError,
)
from stories_monitor.webaccounts import WebAccount
from stories_monitor.workers.session_follower import SessionFollower


class FakeTransport:
    """Records follows, or raises whatever the test asked for."""

    def __init__(self, error: Exception | None = None, result: bool = True) -> None:
        self.error = error
        self.result = result
        self.followed: list[int] = []

    def user_follow(self, user_id: int, username: str | None = None) -> bool:
        if self.error:
            raise self.error
        self.followed.append(user_id)
        return self.result


@pytest.fixture()
def account(db):
    """One live session with cookies, plus targets in the pool."""

    def _make(name: str = "alive", targets: int = 10) -> WebAccount:
        with db() as session:
            session.add(
                Cookie(username=name, sessionid=f"sid-{name}", csrftoken="csrf", is_active=True)
            )
            session.commit()
        follow_assign.enqueue_targets([(2000 + i, f"t{i}") for i in range(targets)])
        return WebAccount(
            id=1, username=name, shard_id=0, cookies={"sessionid": "s", "csrftoken": "c"}
        )

    return _make


def _follower(transport: FakeTransport, monkeypatch, *, hour: int = 14) -> SessionFollower:
    """A follower whose clock sits inside the waking window.

    Without pinning the hour these tests pass or fail depending on the time of
    day the suite happens to run - the worker correctly refuses to follow at
    06:49 local, which looks like a bug in the worker and is not one.
    """
    import datetime as dt

    follower = SessionFollower()
    monkeypatch.setattr(
        follower, "_local_now", lambda: dt.datetime(2026, 6, 15, hour, 0, tzinfo=dt.UTC)
    )
    monkeypatch.setattr(WebAccount, "transport", lambda self: transport)
    return follower


def _state_of(db, target_id: int) -> tuple[str, str | None]:
    with db() as session:
        row = session.get(SessionFollow, target_id)
        return row.state, row.session_username


class TestHappyPath:
    def test_a_follow_is_recorded_in_the_pool(self, db, account, monkeypatch) -> None:
        acct = account()
        transport = FakeTransport()
        follower = _follower(transport, monkeypatch)

        assert follower.follow_one(acct) is True
        assert len(transport.followed) == 1
        assert _state_of(db, transport.followed[0]) == ("following", "alive")

    def test_a_private_target_is_recorded_as_requested(self, db, account, monkeypatch) -> None:
        acct = account()
        transport = FakeTransport(result=False)
        follower = _follower(transport, monkeypatch)
        follower.follow_one(acct)

        with db() as session:
            states = {r.state for r in session.query(SessionFollow).all()}
        assert "requested" in states

    def test_budget_is_spent_one_follow_at_a_time(self, db, account, monkeypatch) -> None:
        acct = account()
        follower = _follower(FakeTransport(), monkeypatch)
        follower.follow_one(acct)
        state = follower._state[acct.username]
        assert state.spent == 1


class TestRhythmGates:
    def test_a_session_rests_between_follows(self, db, account, monkeypatch) -> None:
        """The second call must not fire immediately - that is the metronome."""
        acct = account()
        transport = FakeTransport()
        follower = _follower(transport, monkeypatch)

        follower.follow_one(acct)
        follower.follow_one(acct)

        assert len(transport.followed) == 1, "a follow fired during the rest gap"

    def test_a_spent_budget_stops_following(self, db, account, monkeypatch) -> None:
        acct = account()
        transport = FakeTransport()
        follower = _follower(transport, monkeypatch)

        follower._state_for(acct)
        follower._state[acct.username].budget = 0
        follower._state[acct.username].next_action_at = 0

        assert follower.follow_one(acct) is False
        assert transport.followed == []

    def test_a_sleeping_session_does_not_follow(self, db, account, monkeypatch) -> None:
        """No follows at 04:00 from an account whose owner supposedly sleeps."""
        acct = account()
        transport = FakeTransport()
        follower = _follower(transport, monkeypatch, hour=4)

        assert follower.follow_one(acct) is False
        assert transport.followed == []


class TestFailureHandling:
    def test_an_action_block_stops_writes_and_frees_the_target(
        self, db, account, monkeypatch
    ) -> None:
        """The session is blocked, the target is innocent."""
        acct = account()
        follower = _follower(FakeTransport(error=FeedbackRequiredError("blocked")), monkeypatch)

        follower.follow_one(acct)

        state = follower._state[acct.username]
        assert state.blocked_until > time.monotonic()
        with db() as session:
            owned = session.query(SessionFollow).filter(
                SessionFollow.session_username.is_not(None)
            ).count()
        assert owned == 0, "a blocked session must not keep holding targets"

    def test_a_blocked_session_does_not_try_again(self, db, account, monkeypatch) -> None:
        acct = account()
        transport = FakeTransport(error=FeedbackRequiredError("blocked"))
        follower = _follower(transport, monkeypatch)

        follower.follow_one(acct)
        follower.follow_one(acct)  # must be refused by the block, not retried

        assert follower._state[acct.username].blocked_until > time.monotonic()

    def test_a_dead_session_frees_everything_it_held(self, db, account, monkeypatch) -> None:
        """The 256-followings case, end to end through the worker."""
        acct = account()
        follower = _follower(FakeTransport(), monkeypatch)

        # Build up some held work, then have the session die.
        follower.follow_one(acct)
        follower._state[acct.username].next_action_at = 0
        monkeypatch.setattr(WebAccount, "transport", lambda self: FakeTransport(
            error=LoginRequiredError("session dead")
        ))
        follower.follow_one(acct)

        with db() as session:
            owned = session.query(SessionFollow).filter(
                SessionFollow.session_username.is_not(None)
            ).count()
            cookie = session.get(Cookie, "alive")
        assert owned == 0, "a dead session must hold nothing"
        assert cookie.is_active is False

    def test_freed_targets_are_claimable_by_a_survivor(self, db, account, monkeypatch) -> None:
        acct = account()
        follower = _follower(FakeTransport(error=LoginRequiredError("dead")), monkeypatch)
        follower.follow_one(acct)

        with db() as session:
            session.add(
                Cookie(username="survivor", sessionid="sid2", csrftoken="c", is_active=True)
            )
            session.commit()

        assert follow_assign.claim_targets("survivor", 10), "survivor found nothing to do"

    def test_a_throttle_rests_briefly_and_keeps_the_target_clean(
        self, db, account, monkeypatch
    ) -> None:
        acct = account()
        follower = _follower(FakeTransport(error=RateLimitedError("429")), monkeypatch)
        follower.follow_one(acct)

        with db() as session:
            assert all(r.attempts == 0 for r in session.query(SessionFollow).all())

    def test_a_missing_target_is_terminal(self, db, account, monkeypatch) -> None:
        acct = account()
        follower = _follower(FakeTransport(error=UserNotFoundError("gone")), monkeypatch)
        follower.follow_one(acct)

        with db() as session:
            states = {r.state for r in session.query(SessionFollow).all()}
        assert "unavailable" in states

    def test_a_checkpoint_disables_the_session(self, db, account, monkeypatch) -> None:
        acct = account()
        follower = _follower(FakeTransport(error=ChallengeRequiredError("checkpoint")), monkeypatch)
        follower.follow_one(acct)

        with db() as session:
            assert session.get(Cookie, "alive").is_active is False


class TestLoop:
    def test_a_cycle_sweeps_dead_sessions_first(self, db, account, monkeypatch) -> None:
        """Freeing precedes claiming, so a survivor sees the work the same cycle."""
        account(name="ghost", targets=5)
        follow_assign.claim_targets("ghost", 5)
        with db() as session:
            session.query(Cookie).filter_by(username="ghost").update({"is_active": False})
            session.commit()

        follower = _follower(FakeTransport(), monkeypatch)
        follower.run_once()

        with db() as session:
            ghost_held = session.query(SessionFollow).filter_by(session_username="ghost").count()
        assert ghost_held == 0

    def test_an_empty_fleet_is_not_an_error(self, db, monkeypatch) -> None:
        follower = _follower(FakeTransport(), monkeypatch)
        assert follower.run_once() == 0
