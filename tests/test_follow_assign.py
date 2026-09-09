"""Claiming targets, and freeing them when a session dies.

The property under test throughout: **a target is never stranded**. Whatever
happens to the session holding it, the target ends up either followed or back in
the pool where another session can take it.
"""

from __future__ import annotations

import datetime as dt

import pytest

from stories_monitor import follow_assign
from stories_monitor.db.models import Cookie, SessionFollow


@pytest.fixture()
def pool(db):
    """A live session and 10 free targets."""

    def _make(session_names: tuple[str, ...] = ("alive",), targets: int = 10):
        with db() as session:
            for name in session_names:
                session.add(
                    Cookie(username=name, sessionid=f"sid-{name}", csrftoken="csrf", is_active=True)
                )
            session.commit()
        follow_assign.enqueue_targets([(1000 + i, f"target{i}") for i in range(targets)])

    return _make


def _states(db) -> dict[int, tuple[str, str | None]]:
    with db() as session:
        return {
            int(r.target_user_id): (r.state, r.session_username)
            for r in session.query(SessionFollow).all()
        }


class TestEnqueue:
    def test_targets_land_free_and_unowned(self, db, pool) -> None:
        pool()
        rows = _states(db)
        assert len(rows) == 10
        assert all(state == "free" and owner is None for state, owner in rows.values())

    def test_reimporting_does_not_reset_progress(self, db, pool) -> None:
        """Re-importing the same CSV must not un-follow anything."""
        pool()
        claimed = follow_assign.claim_targets("alive", 3)
        follow_assign.mark_result(claimed[0][0], state="following")

        follow_assign.enqueue_targets([(1000 + i, f"target{i}") for i in range(10)])

        assert _states(db)[claimed[0][0]] == ("following", "alive")


class TestClaim:
    def test_claiming_takes_ownership(self, db, pool) -> None:
        pool()
        claimed = follow_assign.claim_targets("alive", 4)
        assert len(claimed) == 4
        for target_id, _ in claimed:
            assert _states(db)[target_id] == ("claimed", "alive")

    def test_a_claimed_target_is_not_offered_twice(self, db, pool) -> None:
        """Two sessions must never both follow the same target."""
        pool(session_names=("alive", "other"))
        first = {t for t, _ in follow_assign.claim_targets("alive", 6)}
        second = {t for t, _ in follow_assign.claim_targets("other", 6)}
        assert not (first & second)

    def test_claiming_stops_at_the_ceiling(self, db, pool, monkeypatch) -> None:
        """Instagram caps followings; claiming past it only wastes budget."""
        monkeypatch.setattr(follow_assign, "FOLLOW_CEILING", 5)
        monkeypatch.setattr(follow_assign, "CEILING_HEADROOM", 2)
        pool()
        claimed = follow_assign.claim_targets("alive", 10)
        assert len(claimed) == 3

    def test_empty_pool_yields_nothing(self, db, pool) -> None:
        pool(targets=0)
        assert follow_assign.claim_targets("alive", 5) == []


class TestOutcomes:
    def test_success_is_terminal(self, db, pool) -> None:
        pool()
        target_id = follow_assign.claim_targets("alive", 1)[0][0]
        follow_assign.mark_result(target_id, state="following")
        assert _states(db)[target_id] == ("following", "alive")

    def test_session_failure_frees_the_target_without_blame(self, db, pool) -> None:
        """A throttled session is not the target's fault."""
        pool()
        target_id = follow_assign.claim_targets("alive", 1)[0][0]
        follow_assign.mark_result(target_id, state="free", error="throttled")

        assert _states(db)[target_id] == ("free", None)
        with db() as session:
            row = session.get(SessionFollow, target_id)
            assert row.attempts == 0, "the target must not be blamed for a session fault"

    def test_a_failure_returns_the_target_to_the_pool(self, db, pool) -> None:
        pool()
        target_id = follow_assign.claim_targets("alive", 1)[0][0]
        follow_assign.mark_result(target_id, state="failed", error="boom")

        state, owner = _states(db)[target_id]
        assert (state, owner) == ("free", None)

    def test_a_target_is_abandoned_after_repeated_failures(self, db, pool) -> None:
        """Retrying a deleted account forever spends real follow budget."""
        pool()
        target_id = follow_assign.claim_targets("alive", 1)[0][0]
        for _ in range(follow_assign.MAX_ATTEMPTS):
            follow_assign.mark_result(target_id, state="failed", error="boom")

        state, _ = _states(db)[target_id]
        assert state == "failed"
        assert follow_assign.claim_targets("alive", 5) != []  # others still claimable
        assert target_id not in {t for t, _ in follow_assign.claim_targets("alive", 9)}

    def test_unavailable_is_terminal_and_unowned(self, db, pool) -> None:
        pool()
        target_id = follow_assign.claim_targets("alive", 1)[0][0]
        follow_assign.mark_result(target_id, state="unavailable", error="deleted")
        assert _states(db)[target_id] == ("unavailable", None)


class TestReclaim:
    def test_a_dead_session_frees_everything_it_held(self, db, pool) -> None:
        """The headline case: 256 follows on a dead account go back to the pool."""
        pool()
        claimed = follow_assign.claim_targets("alive", 6)
        follow_assign.mark_result(claimed[0][0], state="following")
        follow_assign.mark_result(claimed[1][0], state="requested")

        freed = follow_assign.reap_session("alive", reason="dead")

        assert freed == 6
        for target_id, _ in claimed:
            assert _states(db)[target_id] == ("free", None)

    def test_freed_targets_go_to_a_surviving_session(self, db, pool) -> None:
        """Uninterrupted operation: the survivor picks up the dead one's work."""
        pool(session_names=("alive", "survivor"))
        claimed = {t for t, _ in follow_assign.claim_targets("alive", 10)}
        follow_assign.reap_session("alive", reason="dead")

        picked_up = {t for t, _ in follow_assign.claim_targets("survivor", 10)}
        assert picked_up == claimed

    def test_reaping_clears_the_dead_sessions_attempt_history(self, db, pool) -> None:
        """Failures under a dying session should not count against the target."""
        pool()
        target_id = follow_assign.claim_targets("alive", 1)[0][0]
        follow_assign.mark_result(target_id, state="failed", error="boom")
        follow_assign.reap_session("alive", reason="dead")

        with db() as session:
            assert session.get(SessionFollow, target_id).attempts == 0

    def test_release_keeps_completed_follows(self, db, pool) -> None:
        """A resting session has not lost the follows it already made."""
        pool()
        claimed = follow_assign.claim_targets("alive", 3)
        follow_assign.mark_result(claimed[0][0], state="following")

        follow_assign.release_session("alive", reason="resting")

        assert _states(db)[claimed[0][0]] == ("following", "alive")
        assert _states(db)[claimed[1][0]] == ("free", None)

    def test_inactive_sessions_are_swept_automatically(self, db, pool) -> None:
        """A session that died unobserved still gets its targets redistributed."""
        pool()
        follow_assign.claim_targets("alive", 5)
        with db() as session:
            session.query(Cookie).filter_by(username="alive").update({"is_active": False})
            session.commit()

        freed = follow_assign.reclaim_dead_sessions()

        assert freed == 5
        assert all(owner is None for _, owner in _states(db).values())

    def test_live_sessions_are_left_alone(self, db, pool) -> None:
        pool()
        follow_assign.claim_targets("alive", 4)
        assert follow_assign.reclaim_dead_sessions() == 0

    def test_stale_claims_are_swept(self, db, pool) -> None:
        """A process killed between claim and follow must not reserve rows forever."""
        pool()
        claimed = follow_assign.claim_targets("alive", 3)
        old = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=follow_assign.STALE_CLAIM_SEC + 60)
        with db() as session:
            session.query(SessionFollow).update({"claimed_at": old})
            session.commit()

        assert follow_assign.reclaim_stale_claims() == 3
        for target_id, _ in claimed:
            assert _states(db)[target_id] == ("free", None)

    def test_fresh_claims_survive_the_sweep(self, db, pool) -> None:
        pool()
        follow_assign.claim_targets("alive", 3)
        assert follow_assign.reclaim_stale_claims() == 0

    def test_deleting_a_session_row_frees_its_targets(self, db, pool) -> None:
        """ON DELETE SET NULL: removing cookies must not delete the targets."""
        pool()
        follow_assign.claim_targets("alive", 4)
        with db() as session:
            session.query(Cookie).filter_by(username="alive").delete()
            session.commit()

        rows = _states(db)
        assert len(rows) == 10, "targets are the expensive asset - never cascade-deleted"
        assert all(owner is None for _, owner in rows.values())


class TestStats:
    def test_counts_by_state(self, db, pool) -> None:
        pool()
        claimed = follow_assign.claim_targets("alive", 4)
        follow_assign.mark_result(claimed[0][0], state="following")
        follow_assign.mark_result(claimed[1][0], state="requested")

        snapshot = follow_assign.stats()
        assert snapshot.total == 10
        assert snapshot.following == 1
        assert snapshot.requested == 1
        assert snapshot.done == 2
        assert snapshot.free == 6
