"""No two sessions ever follow the same account.

The claim query already guarantees this through `SKIP LOCKED`, but "the query is
written correctly" is a weaker promise than "the database refuses". These tests
assert both, and the concurrency case is run with real threads against real
Postgres rather than simulated - a race that only shows up under contention is
exactly the kind this needs to catch.
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy.exc import IntegrityError

from stories_monitor import follow_assign
from stories_monitor.db.models import Cookie, SessionFollow


@pytest.fixture()
def fleet(db):
    """Seven live sessions and a pool of targets - the real shape."""

    def _make(sessions: int = 7, targets: int = 40) -> list[str]:
        names = [f"session{i}" for i in range(sessions)]
        with db() as session:
            for name in names:
                session.add(
                    Cookie(username=name, sessionid=f"sid-{name}", csrftoken="c", is_active=True)
                )
            session.commit()
        follow_assign.enqueue_targets(
            [{"target_user_id": 5000 + i, "username": f"t{i}"} for i in range(targets)]
        )
        return names

    return _make


class TestDatabaseEnforcesOneOwner:
    def test_a_second_owner_is_rejected_by_the_database(self, db, fleet) -> None:
        """Even a hand-written UPDATE cannot give a target two sessions."""
        fleet(sessions=2, targets=1)
        follow_assign.claim_targets("session0", 1)

        with pytest.raises(IntegrityError):
            with db() as session:
                # Deliberately bypassing claim_targets: this is the check that
                # a future code path cannot quietly break the invariant.
                session.execute(
                    SessionFollow.__table__.insert().values(
                        target_user_id=5000,
                        username="t0",
                        session_username="session1",
                        state="claimed",
                    )
                )
                session.commit()

    def test_a_released_target_can_be_owned_again(self, db, fleet) -> None:
        """The unique index is partial - it must not block reassignment."""
        fleet(sessions=2, targets=1)
        follow_assign.claim_targets("session0", 1)
        follow_assign.reap_session("session0")

        assert follow_assign.claim_targets("session1", 1), "a freed target must be claimable"


class TestConcurrentClaiming:
    def test_seven_sessions_racing_never_share_a_target(self, db, fleet) -> None:
        """The real scenario: every session claiming at once, all day."""
        names = fleet(sessions=7, targets=40)
        claimed: dict[str, list[int]] = {}
        errors: list[Exception] = []
        barrier = threading.Barrier(len(names))

        def worker(name: str) -> None:
            try:
                barrier.wait(timeout=10)  # maximise contention
                got = follow_assign.claim_targets(name, 10)
                claimed[name] = [t for t, _ in got]
            except Exception as exc:  # noqa: BLE001 - surfaced in the assert below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"claiming raised under contention: {errors}"

        everything = [t for ids in claimed.values() for t in ids]
        assert len(everything) == len(set(everything)), "a target was handed to two sessions"
        assert len(everything) <= 40

    def test_every_target_ends_with_at_most_one_owner(self, db, fleet) -> None:
        names = fleet(sessions=7, targets=40)
        threads = [
            threading.Thread(target=follow_assign.claim_targets, args=(n, 10)) for n in names
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        with db() as session:
            rows = session.query(SessionFollow).all()
            owners = [r.session_username for r in rows if r.session_username]
        # One owner per row is structural; this asserts the count is sane too.
        assert len(owners) == len({r.target_user_id for r in rows if r.session_username})

    def test_contention_does_not_lose_targets(self, db, fleet) -> None:
        """SKIP LOCKED must skip, not drop: unclaimed rows stay claimable."""
        names = fleet(sessions=7, targets=40)
        threads = [
            threading.Thread(target=follow_assign.claim_targets, args=(n, 3)) for n in names
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        snapshot = follow_assign.stats()
        assert snapshot.total == 40
        assert snapshot.claimed + snapshot.free == 40


class TestClaimOrdering:
    def test_public_accounts_are_claimed_before_private_ones(self, db, fleet) -> None:
        """A private follow is only a pending request, and a louder spam signal."""
        fleet(sessions=1, targets=0)
        follow_assign.enqueue_targets(
            [
                {"target_user_id": 9001, "username": "priv1", "is_private": True},
                {"target_user_id": 9002, "username": "priv2", "is_private": True},
                {"target_user_id": 9003, "username": "pub1", "is_private": False},
                {"target_user_id": 9004, "username": "pub2", "is_private": False},
            ]
        )

        first_two = {name for _, name in follow_assign.claim_targets("session0", 2)}
        assert first_two == {"pub1", "pub2"}

    def test_private_accounts_are_still_reached_eventually(self, db, fleet) -> None:
        fleet(sessions=1, targets=0)
        follow_assign.enqueue_targets(
            [
                {"target_user_id": 9001, "username": "priv1", "is_private": True},
                {"target_user_id": 9003, "username": "pub1", "is_private": False},
            ]
        )
        assert len(follow_assign.claim_targets("session0", 10)) == 2


class TestCheckingFlag:
    def test_begin_check_marks_the_session_and_target(self, db, fleet) -> None:
        fleet(sessions=1, targets=2)
        target_id = follow_assign.claim_targets("session0", 1)[0][0]

        follow_assign.begin_check(target_id, "session0")

        with db() as session:
            row = session.get(SessionFollow, target_id)
        assert row.is_checking is True
        assert row.followed_by == "session0"
        assert row.last_checked_at is not None

    def test_a_completed_follow_clears_the_flag(self, db, fleet) -> None:
        fleet(sessions=1, targets=2)
        target_id = follow_assign.claim_targets("session0", 1)[0][0]
        follow_assign.begin_check(target_id, "session0")

        follow_assign.mark_result(target_id, state="following", by_session="session0")

        with db() as session:
            row = session.get(SessionFollow, target_id)
        assert row.is_checking is False
        assert row.followed_by == "session0", "the dashboard still needs to know who follows it"

    def test_a_stale_claim_sweep_clears_the_flag(self, db, fleet) -> None:
        """A killed process must not leave a row stuck 'being checked' forever."""
        import datetime as dt

        fleet(sessions=1, targets=2)
        target_id = follow_assign.claim_targets("session0", 1)[0][0]
        follow_assign.begin_check(target_id, "session0")

        old = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=follow_assign.STALE_CLAIM_SEC + 60)
        with db() as session:
            session.query(SessionFollow).update({"claimed_at": old})
            session.commit()
        follow_assign.reclaim_stale_claims()

        with db() as session:
            assert session.get(SessionFollow, target_id).is_checking is False

    def test_reaping_clears_the_dead_sessions_ownership(self, db, fleet) -> None:
        fleet(sessions=2, targets=2)
        target_id = follow_assign.claim_targets("session0", 1)[0][0]
        follow_assign.mark_result(target_id, state="following", by_session="session0")

        follow_assign.reap_session("session0")

        with db() as session:
            row = session.get(SessionFollow, target_id)
        assert row.followed_by is None, "a dead session must not still show as the follower"
        assert row.is_checking is False


class TestProvenance:
    def test_source_account_is_kept_separate_from_followed_by(self, db, fleet) -> None:
        """The file's `followed_by` is where a target was scraped FROM."""
        fleet(sessions=1, targets=0)
        follow_assign.enqueue_targets(
            [{"target_user_id": 7001, "username": "x", "source_account": "kenmcelroyofficial"}]
        )
        target_id = follow_assign.claim_targets("session0", 1)[0][0]
        follow_assign.mark_result(target_id, state="following", by_session="session0")

        with db() as session:
            row = session.get(SessionFollow, target_id)
        assert row.source_account == "kenmcelroyofficial"
        assert row.followed_by == "session0"
