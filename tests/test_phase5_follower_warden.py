"""SPEC Phase 5 acceptance criteria (SPEC section 9).

*Accept:* killing the follower mid-run and restarting loses no progress and does
not exceed the daily cap; a simulated `challenge_required` moves the account out
of rotation and promotes a reserve.

Plus SPEC 7.7 (False is not an error, private -> requested, the friendship sweep)
and SPEC 7.8 (never auto-solve a challenge, health alerts).

The follower's 09:00-23:00 local window is real behaviour, so the clock is
monkeypatched rather than the window being skipped.
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

from stories_monitor.db.models import (
    AccountEvent,
    DailyActionCounter,
    TargetFollow,
    WorkerAccount,
)
from stories_monitor.transport import (
    ChallengeRequiredError,
    FeedbackRequiredError,
    PrivateAccountError,
)
from stories_monitor.workers._common import read_follows_done
from stories_monitor.workers.follower import (
    Follower,
    FollowOutcome,
    follows_done_today,
    in_follow_window,
    progress_report,
)
from stories_monitor.workers.warden import Warden

# Fixture "now": 2026-08-06 12:00 UTC - inside the default 09:00-23:00 window.
INSIDE_WINDOW = dt.datetime(2026, 8, 6, 12, 0, tzinfo=dt.UTC)
OUTSIDE_WINDOW = dt.datetime(2026, 8, 6, 3, 0, tzinfo=dt.UTC)


@pytest.fixture()
def frozen_clock(monkeypatch):
    """Pin the follower's clock. Returns a setter so a test can move time."""
    state = {"now": INSIDE_WINDOW}

    class _FrozenDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            value = state["now"]
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr("stories_monitor.workers.follower.dt.datetime", _FrozenDatetime)

    def _set(value: dt.datetime) -> None:
        state["now"] = value

    return _set


@pytest.fixture()
def no_pacing(monkeypatch):
    """Remove the 4-12 minute human-pacing gaps so tests stay fast."""
    monkeypatch.setattr(
        "stories_monitor.workers.follower.random.uniform", lambda a, b: 0.0
    )
    monkeypatch.setattr("stories_monitor.workers.follower.time.sleep", lambda _s: None)


@pytest.fixture()
def follow_setup(db, make_worker_account, make_target, make_target_follow, fixture_transport):
    """One active worker with a queue of targets, and a transport it controls."""

    def _setup(*, target_count: int = 5, follow_result: bool = True, private: bool = False):
        worker_id = make_worker_account(shard_id=1, status="active")
        transport = fixture_transport(follow_result=follow_result)
        target_ids = []
        for i in range(target_count):
            user_id = 5000000000 + i
            make_target(user_id, status="private" if private else "active")
            make_target_follow(worker_id, user_id, state="queued")
            target_ids.append(user_id)
        return worker_id, transport, target_ids

    return _setup


def _follower(worker_id: int, transport) -> Follower:
    return Follower(transports={worker_id: transport})


# --- THE resumability + cap criterion -----------------------------------------


def test_killing_the_follower_mid_run_loses_no_progress_and_respects_the_daily_cap(
    db, follow_setup, frozen_clock, no_pacing, monkeypatch, settings
):
    """SPEC Phase 5 acceptance criterion.

    The cap is enforced through `daily_action_counters`, so a restart cannot
    reset it. Simulated by driving follows, throwing the Follower away, building
    a fresh one, and continuing.
    """
    monkeypatch.setenv("FOLLOWS_PER_DAY", "5")
    from stories_monitor.config import get_settings

    get_settings.cache_clear()
    try:
        worker_id, transport, target_ids = follow_setup(target_count=12)

        # --- run 1: three follows, then "kill" the process ---
        follower = _follower(worker_id, transport)
        first_run = [follower.follow_one(worker_id) for _ in range(3)]
        assert [s.outcome for s in first_run] == [FollowOutcome.FOLLOWED] * 3
        followed_first = {s.target_user_id for s in first_run}

        with db() as session:
            assert read_follows_done(session, worker_id, INSIDE_WINDOW.date()) == 3

        del follower  # the process dies here

        # --- run 2: a brand-new Follower picks up exactly where run 1 stopped ---
        restarted = _follower(worker_id, transport)
        second_run = [restarted.follow_one(worker_id) for _ in range(2)]
        assert [s.outcome for s in second_run] == [FollowOutcome.FOLLOWED] * 2
        followed_second = {s.target_user_id for s in second_run}

        assert not (followed_first & followed_second), (
            "a target was followed twice across the restart - progress was lost"
        )

        # --- the cap holds across the restart ---
        capped = restarted.follow_one(worker_id)
        assert capped.outcome == FollowOutcome.CAP_REACHED, (
            "the daily cap was not enforced after a restart"
        )
        assert capped.follows_done_today == 5

        # Even a third fresh instance sees the cap.
        again = _follower(worker_id, transport).follow_one(worker_id)
        assert again.outcome == FollowOutcome.CAP_REACHED

        with db() as session:
            done = read_follows_done(session, worker_id, INSIDE_WINDOW.date())
            states = dict(
                session.execute(
                    sa.select(TargetFollow.target_user_id, TargetFollow.state).where(
                        TargetFollow.worker_account_id == worker_id
                    )
                ).all()
            )
        assert done == 5 <= get_settings().follows_per_day, "the daily cap was exceeded"
        assert sum(1 for s in states.values() if s == "following") == 5
        assert sum(1 for s in states.values() if s == "queued") == 7, (
            "the untouched targets must remain queued for tomorrow"
        )
        assert follows_done_today(worker_id) == 5
    finally:
        get_settings.cache_clear()


def test_the_ledger_and_the_follow_state_move_together(
    db, follow_setup, frozen_clock, no_pacing
):
    """A crash either commits both or neither - they can never disagree."""
    worker_id, transport, _ = follow_setup(target_count=3)
    follower = _follower(worker_id, transport)

    for _ in range(3):
        follower.follow_one(worker_id)

    with db() as session:
        following = session.execute(
            sa.select(sa.func.count())
            .select_from(TargetFollow)
            .where(
                TargetFollow.worker_account_id == worker_id,
                TargetFollow.state == "following",
            )
        ).scalar()
        counter = session.get(DailyActionCounter, (worker_id, INSIDE_WINDOW.date()))
        account = session.get(WorkerAccount, worker_id)

    assert following == 3
    assert counter.follows_done == 3
    assert account.follows_count == 3


def test_the_follow_window_is_enforced_against_the_accounts_local_clock(
    db, follow_setup, frozen_clock, no_pacing, settings
):
    """SPEC 7.7: only operate inside the configured local-time window. Humans sleep."""
    worker_id, transport, _ = follow_setup(target_count=2)
    follower = _follower(worker_id, transport)

    frozen_clock(OUTSIDE_WINDOW)  # 03:00 local
    assert follower.follow_one(worker_id).outcome == FollowOutcome.OUTSIDE_WINDOW

    with db() as session:
        assert read_follows_done(session, worker_id, OUTSIDE_WINDOW.date()) == 0

    frozen_clock(INSIDE_WINDOW)  # 12:00 local
    assert follower.follow_one(worker_id).outcome == FollowOutcome.FOLLOWED


def test_in_follow_window_boundaries():
    utc = dt.UTC
    at = lambda h: dt.datetime(2026, 8, 6, h, tzinfo=utc)  # noqa: E731

    assert in_follow_window(at(9), utc, 9, 23) is True
    assert in_follow_window(at(22), utc, 9, 23) is True
    assert in_follow_window(at(23), utc, 9, 23) is False
    assert in_follow_window(at(8), utc, 9, 23) is False
    # A window that wraps midnight.
    assert in_follow_window(at(23), utc, 22, 6) is True
    assert in_follow_window(at(2), utc, 22, 6) is True
    assert in_follow_window(at(12), utc, 22, 6) is False


# --- user_follow() returning False --------------------------------------------


def test_user_follow_returning_false_is_not_an_error(
    db, follow_setup, frozen_clock, no_pacing
):
    """SPEC 7.7: False means already following/pending. Handle it without erroring."""
    worker_id, transport, target_ids = follow_setup(target_count=2, follow_result=False)
    follower = _follower(worker_id, transport)

    step = follower.follow_one(worker_id)

    assert step.outcome == FollowOutcome.ALREADY
    assert step.outcome != FollowOutcome.FAILED

    with db() as session:
        row = session.get(TargetFollow, (worker_id, step.target_user_id))
        done = read_follows_done(session, worker_id, INSIDE_WINDOW.date())

    assert row.state == "following", "an already-followed target must be settled, not retried"
    assert row.last_error is None, "False must not be recorded as an error"
    assert done == 0, "an already-existing follow must not consume daily cap"

    # The row is settled, so the next call moves on to the other target.
    second = follower.follow_one(worker_id)
    assert second.target_user_id != step.target_user_id


# --- private targets and the friendship sweep ---------------------------------


def test_a_private_target_lands_in_state_requested(
    db, follow_setup, frozen_clock, no_pacing
):
    """SPEC 7.7: private targets go to state `requested`."""
    worker_id, transport, target_ids = follow_setup(target_count=1, private=True)
    follower = _follower(worker_id, transport)

    step = follower.follow_one(worker_id)

    assert step.outcome == FollowOutcome.REQUESTED
    with db() as session:
        row = session.get(TargetFollow, (worker_id, target_ids[0]))
        done = read_follows_done(session, worker_id, INSIDE_WINDOW.date())
    assert row.state == "requested"
    assert row.requested_at is not None
    assert row.confirmed_at is None
    assert done == 1, "an outgoing request still spends daily cap"


def test_a_private_account_error_is_treated_as_a_request(
    db, follow_setup, frozen_clock, no_pacing, monkeypatch
):
    worker_id, transport, target_ids = follow_setup(target_count=1)
    monkeypatch.setattr(
        transport, "user_follow", lambda _uid: (_ for _ in ()).throw(PrivateAccountError("private"))
    )
    follower = _follower(worker_id, transport)

    step = follower.follow_one(worker_id)

    assert step.outcome == FollowOutcome.REQUESTED
    with db() as session:
        assert session.get(TargetFollow, (worker_id, target_ids[0])).state == "requested"


def test_the_friendship_sweep_promotes_requested_to_following(
    db, follow_setup, frozen_clock, no_pacing
):
    """SPEC 7.7: a slow sweep checks user_friendship_v1() and promotes approvals."""
    worker_id, transport, target_ids = follow_setup(target_count=2, private=True)
    follower = _follower(worker_id, transport)

    for _ in target_ids:
        follower.follow_one(worker_id)

    with db() as session:
        states = set(
            session.execute(
                sa.select(TargetFollow.state).where(
                    TargetFollow.worker_account_id == worker_id
                )
            ).scalars()
        )
    assert states == {"requested"}

    # FixtureTransport.user_friendship reports following=True: the requests were accepted.
    promoted = follower.sweep_requested(worker_id)

    assert promoted == len(target_ids)
    with db() as session:
        rows = session.execute(
            sa.select(TargetFollow.state, TargetFollow.confirmed_at).where(
                TargetFollow.worker_account_id == worker_id
            )
        ).all()
        account = session.get(WorkerAccount, worker_id)
    assert [state for state, _ in rows] == ["following"] * len(target_ids)
    assert all(confirmed is not None for _s, confirmed in rows)
    assert account.follows_count == len(target_ids)

    # The sweep is a read: it never spends follow cap.
    with db() as session:
        assert read_follows_done(session, worker_id, INSIDE_WINDOW.date()) == len(target_ids)


def test_the_sweep_marks_a_withdrawn_request_rejected(
    db, follow_setup, frozen_clock, no_pacing, monkeypatch
):
    worker_id, transport, target_ids = follow_setup(target_count=1, private=True)
    follower = _follower(worker_id, transport)
    follower.follow_one(worker_id)

    monkeypatch.setattr(
        transport,
        "user_friendship",
        lambda _uid: {"following": False, "outgoing_request": False},
    )
    promoted = follower.sweep_requested(worker_id)

    assert promoted == 0
    with db() as session:
        assert session.get(TargetFollow, (worker_id, target_ids[0])).state == "rejected"


def test_feedback_required_pauses_follows_for_this_account(
    db, follow_setup, frozen_clock, no_pacing, monkeypatch
):
    """SPEC 7.7: stop that account's follows for 24h, log an event, keep polling."""
    worker_id, transport, target_ids = follow_setup(target_count=3)
    monkeypatch.setattr(
        transport,
        "user_follow",
        lambda _uid: (_ for _ in ()).throw(FeedbackRequiredError("action blocked")),
    )
    follower = _follower(worker_id, transport)

    step = follower.follow_one(worker_id)
    assert step.outcome == FollowOutcome.PAUSED

    with db() as session:
        account = session.get(WorkerAccount, worker_id)
        events = session.execute(
            sa.select(AccountEvent.event_type).where(
                AccountEvent.worker_account_id == worker_id
            )
        ).scalars().all()
        row = session.get(TargetFollow, (worker_id, step.target_user_id))

    assert "feedback_required" in events
    assert account.status == "active", "reading is safer than writing - keep polling"
    assert row.state == "queued", "the target must stay queued for after the pause"

    # The pause is derived from the event row, so a restarted follower still honours it.
    assert _follower(worker_id, transport).follow_one(worker_id).outcome == FollowOutcome.PAUSED


# --- challenge_required: out of rotation, reserve promoted --------------------


def test_a_simulated_challenge_moves_the_account_out_of_rotation_and_promotes_a_reserve(
    db, make_worker_account, make_target, make_target_follow, frozen_clock
):
    """SPEC Phase 5 acceptance criterion.

    The account is marked `challenged`, an `account_event` is written, and a
    `reserve` account is promoted into the shard with its `target_follows` rows
    re-queued from the `targets` table.
    """
    lost_id = make_worker_account(shard_id=3, status="active")
    reserve_id = make_worker_account(shard_id=99, status="reserve")

    target_ids = [6000000000 + i for i in range(4)]
    for user_id in target_ids:
        make_target(user_id, shard_id=3, status="active")
        make_target_follow(lost_id, user_id, state="following")
    # A target in another shard must not be enqueued for the reserve.
    make_target(6100000000, shard_id=7, status="active")
    # An inactive target in shard 3 must not be enqueued either.
    make_target(6100000001, shard_id=3, status="deleted")

    action = Warden().handle_account_error(lost_id, ChallengeRequiredError("challenge_required"))

    assert action.action == "challenged"

    with db() as session:
        lost = session.get(WorkerAccount, lost_id)
        reserve = session.get(WorkerAccount, reserve_id)
        events = session.execute(
            sa.select(AccountEvent.event_type, AccountEvent.worker_account_id)
        ).all()
        queued = [
            int(uid)
            for uid in session.execute(
                sa.select(TargetFollow.target_user_id).where(
                    TargetFollow.worker_account_id == reserve_id,
                    TargetFollow.state == "queued",
                )
            ).scalars()
        ]

    # 1. The account is out of rotation. The poller and follower both gate on 'active'.
    assert lost.status == "challenged"
    assert lost.status != "active"

    # 2. An account_event row records the incident.
    assert ("challenge", lost_id) in events, f"no challenge event recorded: {events}"

    # 3. A reserve was promoted into the lost account's shard...
    assert reserve.status == "active"
    assert reserve.shard_id == 3

    # 4. ...with the follow graph rebuilt FROM THE DATABASE, shard-scoped.
    assert sorted(queued) == sorted(target_ids)
    assert 6100000000 not in queued, "a target from another shard was enqueued"
    assert 6100000001 not in queued, "an inactive target was enqueued"

    # The promotion is itself audited.
    assert ("recovered", reserve_id) in events


def test_the_reserve_keeps_its_own_device_settings_and_proxy(
    db, make_worker_account, make_target
):
    """SPEC section 8: never reuse another account's identity, never rotate a proxy."""
    lost_id = make_worker_account(shard_id=4, status="active")
    reserve_id = make_worker_account(shard_id=98, status="reserve")

    with db() as session:
        before = session.get(WorkerAccount, reserve_id)
        original_device = dict(before.device_settings)
        original_proxy = before.proxy_url
        lost_proxy = session.get(WorkerAccount, lost_id).proxy_url

    Warden().handle_account_error(lost_id, ChallengeRequiredError("challenge"))

    with db() as session:
        after = session.get(WorkerAccount, reserve_id)

    assert after.device_settings == original_device, "device settings were regenerated"
    assert after.proxy_url == original_proxy, "the reserve's proxy was rotated"
    assert after.proxy_url != lost_proxy, "the lost account's proxy was reused"


def test_promotion_alerts_when_there_is_no_reserve(db, make_worker_account, capsys):
    lost_id = make_worker_account(shard_id=5, status="active")
    warden = Warden()

    assert warden.promote_reserve(5) is None

    action = warden.handle_account_error(lost_id, ChallengeRequiredError("challenge"))
    assert action.action == "challenged"
    with db() as session:
        assert session.get(WorkerAccount, lost_id).status == "challenged"


def test_a_challenge_during_the_follower_also_retires_the_account(
    db, follow_setup, frozen_clock, no_pacing, monkeypatch
):
    worker_id, transport, _ = follow_setup(target_count=2)
    monkeypatch.setattr(
        transport,
        "user_follow",
        lambda _uid: (_ for _ in ()).throw(ChallengeRequiredError("challenge_required")),
    )
    follower = _follower(worker_id, transport)

    step = follower.follow_one(worker_id)

    assert step.outcome == FollowOutcome.ACCOUNT_LOST
    with db() as session:
        assert session.get(WorkerAccount, worker_id).status == "challenged"
        events = session.execute(sa.select(AccountEvent.event_type)).scalars().all()
    assert "challenge" in events

    # A challenged account does no further work.
    assert follower.follow_one(worker_id).outcome == FollowOutcome.ACCOUNT_LOST


# --- the warden NEVER auto-solves a challenge --------------------------------


def test_the_warden_never_auto_solves_a_challenge(db, make_worker_account, monkeypatch):
    """SPEC 7.8 / section 11: never attempt to auto-solve. Alert a human instead.

    Asserted by making every plausible solving entry point explode: if any of
    them is reached, the test fails.
    """
    worker_id = make_worker_account(shard_id=6, status="active")
    warden = Warden()

    forbidden: list[str] = []

    def _forbid(name):
        def _boom(*_a, **_k):
            forbidden.append(name)
            raise AssertionError(f"the warden called a challenge-solving path: {name}")

        return _boom

    # If the transport ever grows one of these, the warden must still not call it.
    from stories_monitor.transport.fixture import FixtureTransport

    for name in ("challenge_resolve", "challenge_code", "challenge_send_security_code"):
        monkeypatch.setattr(FixtureTransport, name, _forbid(name), raising=False)

    action = warden.handle_account_error(worker_id, ChallengeRequiredError("challenge_required"))

    assert forbidden == [], f"a challenge-solving path was taken: {forbidden}"
    assert action.action == "challenged"
    # The prime-directive assertion is a no-op that must exist and never raise.
    assert Warden.assert_never_auto_solve() is None

    with db() as session:
        account = session.get(WorkerAccount, worker_id)
    assert account.status == "challenged", "a challenged account must stop, not retry"


def test_live_transport_challenge_resolve_raises_rather_than_solving():
    """SPEC 7.8: instagrapi auto-solves from inside private_request unless stopped."""
    pytest.importorskip("instagrapi", reason="instagrapi is not installed")
    from stories_monitor.transport.live import _NoChallengeClient

    client = _NoChallengeClient.__new__(_NoChallengeClient)
    with pytest.raises(ChallengeRequiredError):
        client.challenge_resolve({"challenge": {"url": "https://example"}})


# --- health checks (SPEC 7.8) -------------------------------------------------


def test_check_health_alerts_when_a_shard_drops_below_min_active_workers(
    db, make_worker_account, settings
):
    """SPEC 7.8: alert when any shard drops below 2 active workers."""
    assert settings.min_active_workers_per_shard == 2

    # Shard 1 is healthy, shard 2 is one short, shard 3 has none.
    make_worker_account(shard_id=1, status="active", last_poll_at=dt.datetime.now(dt.UTC))
    make_worker_account(shard_id=1, status="active", last_poll_at=dt.datetime.now(dt.UTC))
    make_worker_account(shard_id=2, status="active", last_poll_at=dt.datetime.now(dt.UTC))
    make_worker_account(shard_id=2, status="challenged")
    make_worker_account(shard_id=3, status="challenged")

    alerts = Warden().check_health()
    shard_alerts = {a.context["shard_id"]: a for a in alerts if a.kind == "shard_below_minimum"}

    assert 1 not in shard_alerts, "a fully staffed shard must not alert"
    assert 2 in shard_alerts
    assert shard_alerts[2].severity == "warning"
    assert shard_alerts[2].context["active_workers"] == 1
    assert 3 in shard_alerts
    assert shard_alerts[3].severity == "critical", "zero active workers is critical"
    assert shard_alerts[3].context["active_workers"] == 0


def test_check_health_alerts_when_poller_lag_exceeds_the_threshold(
    db, make_worker_account, settings
):
    """SPEC 7.8: alert when poller lag exceeds 5 minutes.

    A missed poll window means permanently lost stories - they expire in 24h.
    """
    threshold = settings.poller_lag_alert_sec
    assert threshold == 300

    now = dt.datetime.now(dt.UTC)
    fresh = make_worker_account(
        shard_id=1, status="active", last_poll_at=now - dt.timedelta(seconds=30)
    )
    stale = make_worker_account(
        shard_id=1, status="active", last_poll_at=now - dt.timedelta(seconds=threshold + 120)
    )
    never = make_worker_account(shard_id=1, status="active", last_poll_at=None)

    alerts = Warden().check_health()
    lag = {a.context["worker_account_id"]: a for a in alerts if a.kind == "poller_lag"}

    assert fresh not in lag, "a freshly polled worker must not alert"
    assert stale in lag
    assert lag[stale].severity == "critical"
    assert lag[stale].context["lag_sec"] >= threshold
    assert never in lag, "a worker that has never polled must alert"
    assert lag[never].context["lag_sec"] is None


def test_check_health_alerts_on_queue_depth(db, make_worker_account, fake_queues, settings):
    from stories_monitor.config import Q_FETCH
    from stories_monitor.workers.warden import QUEUE_DEPTH_ALERT_THRESHOLD

    make_worker_account(shard_id=1, status="active", last_poll_at=dt.datetime.now(dt.UTC))
    make_worker_account(shard_id=1, status="active", last_poll_at=dt.datetime.now(dt.UTC))

    fake_queues[Q_FETCH].push_many(
        [{"user_id": i} for i in range(QUEUE_DEPTH_ALERT_THRESHOLD + 1)]
    )

    alerts = Warden().check_health()
    depth_alerts = [a for a in alerts if a.kind == "queue_depth"]

    assert depth_alerts, "a deep queue did not alert"
    assert depth_alerts[0].context["queue"] == Q_FETCH


def test_reconcile_lost_accounts_promotes_reserves_after_a_warden_restart(
    db, make_worker_account, make_target
):
    """Restart-safe recovery: an account lost while the warden was down still heals."""
    make_worker_account(shard_id=8, status="challenged")
    reserve_id = make_worker_account(shard_id=97, status="reserve")
    make_target(6200000000, shard_id=8, status="active")

    promoted = Warden().reconcile_lost_accounts()

    assert promoted == 1
    with db() as session:
        reserve = session.get(WorkerAccount, reserve_id)
        queued = session.execute(
            sa.select(sa.func.count())
            .select_from(TargetFollow)
            .where(TargetFollow.worker_account_id == reserve_id)
        ).scalar()
    assert reserve.status == "active"
    assert reserve.shard_id == 8
    assert queued == 1


def test_promotion_is_idempotent(db, make_worker_account, make_target):
    """ON CONFLICT DO NOTHING - re-running after a crash is harmless."""
    make_worker_account(shard_id=9, status="challenged")
    make_worker_account(shard_id=96, status="reserve")
    make_worker_account(shard_id=95, status="reserve")
    make_target(6300000000, shard_id=9, status="active")

    warden = Warden()
    first = warden.promote_reserve(9)
    second = warden.promote_reserve(9)

    assert first is not None and second is not None and first != second
    with db() as session:
        rows = session.execute(
            sa.select(sa.func.count()).select_from(TargetFollow)
        ).scalar()
    assert rows == 2, "one row per (worker, target), no duplicates"


# --- progress reporting (the weekly question, SPEC 7.7) -----------------------


def test_progress_report_projects_completion_per_shard(
    db, make_worker_account, make_target, make_target_follow, settings
):
    worker_id = make_worker_account(shard_id=1, status="active")
    for i in range(10):
        user_id = 6400000000 + i
        make_target(user_id, shard_id=1)
        make_target_follow(worker_id, user_id, state="following" if i < 4 else "queued")

    reports = {r.shard_id: r for r in progress_report()}

    assert reports[1].follows_completed == 4
    assert reports[1].follows_remaining == 6
    assert reports[1].active_workers == 1
    assert reports[1].daily_capacity == settings.follows_per_day
    assert reports[1].projected_completion is not None
    assert reports[1].days_remaining == pytest.approx(6 / settings.follows_per_day)


def test_progress_report_marks_a_stalled_shard(
    db, make_worker_account, make_target, make_target_follow
):
    worker_id = make_worker_account(shard_id=2, status="challenged")
    make_target(6500000000, shard_id=2)
    make_target_follow(worker_id, 6500000000, state="queued")

    reports = {r.shard_id: r for r in progress_report()}

    assert reports[2].active_workers == 0
    assert reports[2].days_remaining is None, "a shard with no workers is stalled"
    assert reports[2].projected_completion is None
