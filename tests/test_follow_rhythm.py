"""The follow rhythm: budgets, bursts, sleep.

These are pure functions taking `now` explicitly, so the whole daily and weekly
shape can be asserted without waiting for real time.
"""

from __future__ import annotations

import datetime as dt

from stories_monitor.follow_rhythm import (
    _BURST_MAX,
    _BURST_MIN,
    daily_budget,
    in_waking_hours,
    next_gap_sec,
    plan_burst,
    seconds_until_waking,
    warmup_allowance,
)


def _at(hour: int, day: int = 15) -> dt.datetime:
    return dt.datetime(2026, 6, day, hour, 0, tzinfo=dt.UTC)


class TestDailyBudget:
    def test_budget_is_stable_within_a_day(self) -> None:
        """A restart must not re-roll the budget and grant a second allowance."""
        first = daily_budget("acct", _at(9), mean_per_day=30)
        again = daily_budget("acct", _at(17), mean_per_day=30)
        assert first == again

    def test_budget_differs_between_days(self) -> None:
        monday = daily_budget("acct", _at(9, day=15), mean_per_day=30)
        tuesday = daily_budget("acct", _at(9, day=16), mean_per_day=30)
        wednesday = daily_budget("acct", _at(9, day=17), mean_per_day=30)
        assert len({monday, tuesday, wednesday}) > 1

    def test_sessions_do_not_share_a_budget(self) -> None:
        """A fleet that all stops at the same number describes itself.

        The bar is deliberately modest: around a mean of 30 the whole legal
        range is ~20-40, so a handful of distinct values across a dozen sessions
        is the most that range can express. What matters is that they are not
        all equal.
        """
        budgets = [daily_budget(f"acct{i}", _at(9), mean_per_day=30) for i in range(12)]
        assert len(set(budgets)) >= 3

    def test_budget_stays_near_the_mean(self) -> None:
        seen = [daily_budget(f"a{i}", _at(9), mean_per_day=30) for i in range(60)]
        # Off days pull the floor to 0, but nothing may exceed mean + spread.
        assert max(seen) <= int(30 * 1.35) + 1
        assert sum(seen) / len(seen) < 30 * 1.35

    def test_zero_mean_disables_following(self) -> None:
        assert daily_budget("acct", _at(9), mean_per_day=0) == 0

    def test_some_days_are_off_days(self) -> None:
        """People do not open the app every single day."""
        low = [
            d
            for i in range(120)
            if (d := daily_budget(f"acct{i}", _at(9), mean_per_day=30)) < 10
        ]
        assert low, "no off-days in 120 samples - the rhythm is too regular"


class TestWarmup:
    def test_a_new_session_starts_small(self) -> None:
        assert warmup_allowance(0, 30) <= 10

    def test_warmup_ramps_with_experience(self) -> None:
        early = warmup_allowance(10, 30)
        later = warmup_allowance(50, 30)
        assert early < later <= 30

    def test_an_experienced_session_gets_the_full_budget(self) -> None:
        assert warmup_allowance(5000, 30) == 30

    def test_warmup_caps_the_daily_budget(self) -> None:
        """A brand-new session must not draw a full-size day."""
        fresh = daily_budget("new", _at(9), mean_per_day=40, follows_done_ever=0)
        assert fresh <= 10


class TestBursts:
    def test_burst_is_a_handful_not_the_whole_day(self) -> None:
        plan = plan_burst("acct", _at(11), remaining_today=100)
        assert _BURST_MIN <= plan.size <= _BURST_MAX

    def test_burst_never_exceeds_what_is_left(self) -> None:
        plan = plan_burst("acct", _at(11), remaining_today=2)
        assert plan.size <= 2

    def test_no_burst_when_the_budget_is_gone(self) -> None:
        assert plan_burst("acct", _at(11), remaining_today=0).size == 0

    def test_gaps_inside_a_burst_are_seconds(self) -> None:
        gap = next_gap_sec("acct", _at(11), position_in_burst=0, burst_size=4)
        assert 10 <= gap <= 120

    def test_gap_after_a_burst_is_a_long_rest(self) -> None:
        """The phone goes away - this is where most of the day is spent."""
        gap = next_gap_sec("acct", _at(11), position_in_burst=3, burst_size=4)
        assert gap >= 30 * 60

    def test_gaps_are_never_identical(self) -> None:
        """A fixed gap, even a long one, is still a metronome."""
        gaps = {
            round(next_gap_sec("acct", _at(11), position_in_burst=0, burst_size=5), 3)
            for _ in range(20)
        }
        assert len(gaps) > 15


class TestSleep:
    def test_daytime_is_waking(self) -> None:
        assert in_waking_hours(_at(14), 9, 23)

    def test_small_hours_are_not(self) -> None:
        assert not in_waking_hours(_at(4), 9, 23)

    def test_window_may_wrap_midnight(self) -> None:
        """22->6 must not be read as an empty window."""
        assert in_waking_hours(_at(23), 22, 6)
        assert in_waking_hours(_at(2), 22, 6)
        assert not in_waking_hours(_at(12), 22, 6)

    def test_equal_bounds_mean_always_awake(self) -> None:
        assert in_waking_hours(_at(3), 0, 0)

    def test_sleeping_session_waits_until_morning(self) -> None:
        wait = seconds_until_waking(_at(3), 9)
        assert 6 * 3600 <= wait <= 6 * 3600 + 45 * 60 + 1

    def test_wake_time_is_ragged(self) -> None:
        """Nobody opens the app at exactly 09:00:00 every morning."""
        waits = {round(seconds_until_waking(_at(3), 9)) for _ in range(15)}
        assert len(waits) > 10
