"""When a session is allowed to follow, and how long it waits between follows.

Following is the only write this system performs, and it is the action Instagram
polices hardest. The read path already got this treatment (one connection, UA-
matched client hints, irregular gaps); this module is the same idea applied to
the write, where the penalty for looking wrong is `feedback_required` rather
than a retry.

The model is a person with a phone, not a rate limiter:

- People follow in BURSTS. Someone opens the app, follows four accounts in two
  minutes, and puts the phone down for three hours. A perfectly even 1-per-6.5-
  minutes drip over 24h is not a slower human, it is an obvious robot - and it
  is what a naive `follows_per_day / 86400` scheduler produces.
- People SLEEP. Follows at 04:00 local time, every night, from an account whose
  owner supposedly lives in that timezone, is a contradiction.
- People get BORED. Each session gets its own daily budget, drawn randomly
  around the configured mean, so a fleet does not move in lockstep.
- People WARM UP. A session that has never followed anything should not open
  with 40 follows; the first days are deliberately small.

Everything here is pure and takes `now` explicitly, so the schedule can be
tested without waiting for real time to pass.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import random
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "BurstPlan",
    "RhythmDecision",
    "Verdict",
    "daily_budget",
    "next_gap_sec",
    "plan_burst",
    "warmup_allowance",
]


class Verdict(StrEnum):
    """Why a session may or may not follow right now."""

    GO = "go"
    ASLEEP = "asleep"          # outside the account's local waking window
    BUDGET_SPENT = "budget"    # today's self-imposed cap is used up
    RESTING = "resting"        # mid-burst pause, or a long between-burst rest
    BLOCKED = "blocked"        # feedback_required / checkpoint - not our choice


# A burst is "picked the phone up once". Real sessions cluster tightly.
_BURST_MIN = 2
_BURST_MAX = 5

# Inside a burst, follows are seconds apart - the time to tap, glance, tap again.
_IN_BURST_MIN_SEC = 25.0
_IN_BURST_MAX_SEC = 95.0

# Between bursts, the phone goes away. This is where nearly all the day goes.
_BETWEEN_BURST_MIN_SEC = 40 * 60.0
_BETWEEN_BURST_MAX_SEC = 200 * 60.0

# Day 1 of a session's follow life. Ramps toward the full budget over ~2 weeks;
# a brand-new session that immediately follows 30 accounts is the single
# loudest signal a fleet can send.
_WARMUP_DAYS = 14
_WARMUP_FLOOR = 5


@dataclass(frozen=True, slots=True)
class BurstPlan:
    """How many follows this pickup-of-the-phone should do, and the gaps inside it."""

    size: int
    gaps_sec: tuple[float, ...]

    @property
    def span_sec(self) -> float:
        return sum(self.gaps_sec)


@dataclass(frozen=True, slots=True)
class RhythmDecision:
    """Whether to follow now, and if not, when to look again."""

    verdict: Verdict
    wait_sec: float = 0.0
    reason: str = ""

    @property
    def go(self) -> bool:
        return self.verdict is Verdict.GO


def _session_rng(session_key: str, day: dt.date, salt: str = "") -> random.Random:
    """A stable per-session, per-day random stream.

    Seeded rather than global so a restart mid-day does not re-roll the day's
    budget and hand a session a second full allowance - the crash-resumability
    property the follower already relies on for its ledger.
    """
    digest = hashlib.sha256(f"{session_key}|{day.isoformat()}|{salt}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def warmup_allowance(follows_done_ever: int, target_budget: int) -> int:
    """Cap for a session by how much it has ever followed.

    Uses lifetime follows rather than account age because that is what we can
    actually observe: a session pasted today may belong to a 5-year-old account,
    and what Instagram reacts to is the change in behaviour, not the birthday.
    """
    if follows_done_ever >= _WARMUP_DAYS * _WARMUP_FLOOR:
        return target_budget
    # Roughly: 5 on the first day, widening until it meets the real budget.
    day_equivalent = follows_done_ever // _WARMUP_FLOOR
    allowance = _WARMUP_FLOOR + day_equivalent * max(1, target_budget // _WARMUP_DAYS)
    return max(_WARMUP_FLOOR, min(target_budget, allowance))


def daily_budget(
    session_key: str,
    now_local: dt.datetime,
    *,
    mean_per_day: int,
    follows_done_ever: int = 10_000,
    spread: float = 0.35,
) -> int:
    """Today's follow budget for one session.

    Drawn around `mean_per_day` with +-`spread`, so no two sessions share a
    number and no session repeats yesterday's. A fleet whose accounts all stop
    at exactly 30 follows describes itself.
    """
    if mean_per_day <= 0:
        return 0
    rng = _session_rng(session_key, now_local.date(), "budget")
    low = mean_per_day * (1.0 - spread)
    high = mean_per_day * (1.0 + spread)
    drawn = int(round(rng.uniform(low, high)))

    # One day in roughly nine, the owner simply does not use the app.
    if rng.random() < 0.11:
        drawn = int(drawn * rng.uniform(0.0, 0.25))

    drawn = max(0, drawn)
    return min(drawn, warmup_allowance(follows_done_ever, mean_per_day))


def plan_burst(session_key: str, now_local: dt.datetime, remaining_today: int) -> BurstPlan:
    """Size and internal gaps of the next burst."""
    if remaining_today <= 0:
        return BurstPlan(size=0, gaps_sec=())
    rng = _session_rng(session_key, now_local.date(), f"burst-{now_local.hour}-{remaining_today}")
    size = min(remaining_today, rng.randint(_BURST_MIN, _BURST_MAX))
    gaps = tuple(rng.uniform(_IN_BURST_MIN_SEC, _IN_BURST_MAX_SEC) for _ in range(max(0, size - 1)))
    return BurstPlan(size=size, gaps_sec=gaps)


def next_gap_sec(
    session_key: str,
    now_local: dt.datetime,
    *,
    position_in_burst: int,
    burst_size: int,
) -> float:
    """Seconds to wait before the next follow.

    Short inside a burst, long between bursts. Jittered on every call - a fixed
    gap, even a long one, is still a metronome.
    """
    rng = random.Random()  # deliberately unseeded: gaps need not survive a restart
    if position_in_burst < burst_size - 1:
        return rng.uniform(_IN_BURST_MIN_SEC, _IN_BURST_MAX_SEC)
    return rng.uniform(_BETWEEN_BURST_MIN_SEC, _BETWEEN_BURST_MAX_SEC)


def in_waking_hours(now_local: dt.datetime, start_hour: int, end_hour: int) -> bool:
    """True when the account's claimed local time is inside its waking window.

    Handles a window that wraps midnight (22 -> 6), which a naive
    `start <= hour < end` gets silently wrong.
    """
    hour = now_local.hour
    if start_hour == end_hour:
        return True
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def seconds_until_waking(now_local: dt.datetime, start_hour: int) -> float:
    """How long until the window opens again, so a sleeping session sleeps properly."""
    target = now_local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if target <= now_local:
        target = target + dt.timedelta(days=1)
    # Wake a little raggedly - nobody opens the app at 09:00:00 every morning.
    return (target - now_local).total_seconds() + random.uniform(0, 45 * 60)
