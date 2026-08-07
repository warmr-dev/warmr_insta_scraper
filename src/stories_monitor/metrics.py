"""Observability (SPEC section 10).

Every sample lands in `metric_samples` as (metric, value, labels, recorded_at).
That table is the simple stats table the spec asks for; a /metrics endpoint can be
layered on top of these same helpers later.

Detection latency (`discovered_at - taken_at`, p50 and p95) is the number that
proves or disproves the whole design, so it gets a first-class helper.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import Float, cast, func, select
from sqlalchemy.orm import Session

from .db.models import MetricSample, Story
from .db.session import session_scope
from .logging_setup import get_logger

log = get_logger(__name__)

__all__ = [
    "M_CHEAP_MODEL_CALLS",
    "M_DETECTION_LATENCY",
    "M_ESTIMATED_SPEND",
    "M_LEADS_APPROVED",
    "M_PHOTOS_ANALYSED",
    "M_POLL_LATENCY",
    "M_POLL_SUCCESS",
    "M_QUEUE_DEPTH",
    "M_SLACK_DELIVERIES",
    "M_SMART_MODEL_CALLS",
    "M_STORIES_DISCOVERED",
    "M_TRAY_ENTRY_COUNT",
    "M_VIDEOS_SKIPPED",
    "daily_spend",
    "detection_latency_percentiles",
    "metric_summary",
    "record_cheap_model_call",
    "record_detection_latency",
    "record_leads_approved",
    "record_metric",
    "record_photo_analysed",
    "record_poll",
    "record_queue_depth",
    "record_slack_delivery",
    "record_smart_model_call",
    "record_stories_discovered",
    "record_tray_entry_count",
    "record_video_skipped",
]

# --- metric names (stable strings; dashboards key on these) -------------------

M_POLL_LATENCY = "poll.latency_sec"
M_POLL_SUCCESS = "poll.success"
M_TRAY_ENTRY_COUNT = "poll.tray_entry_count"
M_QUEUE_DEPTH = "queue.depth"
M_STORIES_DISCOVERED = "stories.discovered"
M_PHOTOS_ANALYSED = "stories.photos_analysed"
M_VIDEOS_SKIPPED = "stories.videos_skipped"
M_CHEAP_MODEL_CALLS = "ai.cheap_model_calls"
M_SMART_MODEL_CALLS = "ai.smart_model_calls"
M_ESTIMATED_SPEND = "ai.estimated_spend_usd"
M_LEADS_APPROVED = "leads.approved"
M_SLACK_DELIVERIES = "slack.deliveries"
M_DETECTION_LATENCY = "detection.latency_sec"


# --- core writer ---------------------------------------------------------------


def record_metric(
    metric: str,
    value: float,
    labels: dict[str, Any] | None = None,
    *,
    session: Session | None = None,
) -> None:
    """Append one sample. Never raises - metrics must not break the pipeline."""
    sample = MetricSample(metric=metric, value=value, labels=labels or None)
    try:
        if session is not None:
            session.add(sample)
            session.flush()
        else:
            with session_scope() as own_session:
                own_session.add(sample)
    except Exception as exc:  # noqa: BLE001
        log.warning("metrics.write_failed", metric=metric, error=str(exc))


# --- SPEC section 10 helpers ---------------------------------------------------


def record_poll(
    worker_account_id: int,
    *,
    latency_sec: float,
    success: bool,
    tray_entry_count: int | None = None,
    session: Session | None = None,
) -> None:
    """Poll latency and success rate per worker account, plus the truncation canary."""
    labels = {"worker_account_id": worker_account_id}
    record_metric(M_POLL_LATENCY, latency_sec, labels, session=session)
    record_metric(M_POLL_SUCCESS, 1.0 if success else 0.0, labels, session=session)
    if tray_entry_count is not None:
        record_tray_entry_count(worker_account_id, tray_entry_count, session=session)


def record_tray_entry_count(
    worker_account_id: int, count: int, *, session: Session | None = None
) -> None:
    """Tray entry count per poll - a sudden drop means truncation trouble."""
    record_metric(
        M_TRAY_ENTRY_COUNT, float(count), {"worker_account_id": worker_account_id},
        session=session,
    )


def record_queue_depth(
    queue_name: str, depth: int, *, session: Session | None = None
) -> None:
    record_metric(M_QUEUE_DEPTH, float(depth), {"queue": queue_name}, session=session)


def record_stories_discovered(
    count: int = 1, *, worker_account_id: int | None = None, session: Session | None = None
) -> None:
    labels = {"worker_account_id": worker_account_id} if worker_account_id else None
    record_metric(M_STORIES_DISCOVERED, float(count), labels, session=session)


def record_photo_analysed(count: int = 1, *, session: Session | None = None) -> None:
    record_metric(M_PHOTOS_ANALYSED, float(count), session=session)


def record_video_skipped(count: int = 1, *, session: Session | None = None) -> None:
    record_metric(M_VIDEOS_SKIPPED, float(count), session=session)


def record_cheap_model_call(
    *,
    model: str,
    estimated_cost_usd: float = 0.0,
    calls: int = 1,
    session: Session | None = None,
) -> None:
    record_metric(M_CHEAP_MODEL_CALLS, float(calls), {"model": model}, session=session)
    if estimated_cost_usd:
        record_metric(
            M_ESTIMATED_SPEND,
            estimated_cost_usd,
            {"model": model, "tier": "cheap"},
            session=session,
        )


def record_smart_model_call(
    *,
    model: str,
    estimated_cost_usd: float = 0.0,
    calls: int = 1,
    session: Session | None = None,
) -> None:
    record_metric(M_SMART_MODEL_CALLS, float(calls), {"model": model}, session=session)
    if estimated_cost_usd:
        record_metric(
            M_ESTIMATED_SPEND,
            estimated_cost_usd,
            {"model": model, "tier": "smart"},
            session=session,
        )


def record_leads_approved(count: int = 1, *, session: Session | None = None) -> None:
    record_metric(M_LEADS_APPROVED, float(count), session=session)


def record_slack_delivery(
    *, status: str, count: int = 1, session: Session | None = None
) -> None:
    record_metric(M_SLACK_DELIVERIES, float(count), {"status": status}, session=session)


def record_detection_latency(
    story_id: str,
    taken_at: dt.datetime,
    discovered_at: dt.datetime | None = None,
    *,
    session: Session | None = None,
) -> float:
    """discovered_at - taken_at, in seconds. Returns the value it recorded.

    This is the number that proves or disproves the whole design (SPEC section 10).
    """
    discovered = discovered_at or dt.datetime.now(dt.UTC)
    if taken_at.tzinfo is None:
        taken_at = taken_at.replace(tzinfo=dt.UTC)
    if discovered.tzinfo is None:
        discovered = discovered.replace(tzinfo=dt.UTC)

    latency = (discovered - taken_at).total_seconds()
    record_metric(M_DETECTION_LATENCY, latency, {"story_id": story_id}, session=session)
    return latency


# --- read side -----------------------------------------------------------------


def detection_latency_percentiles(
    *,
    since: dt.datetime | None = None,
    session: Session | None = None,
) -> dict[str, float | int | None]:
    """p50 / p95 of `discovered_at - taken_at` computed straight from `stories`.

    Reading from `stories` rather than `metric_samples` means the number is correct
    even for rows written before instrumentation, and it cannot drift from the data.
    Returns {"count", "p50", "p95"} with None percentiles when there are no rows.
    """
    latency = cast(
        func.extract("epoch", Story.discovered_at - Story.taken_at), Float
    )
    stmt = select(
        func.count(),
        func.percentile_cont(0.5).within_group(latency.asc()),
        func.percentile_cont(0.95).within_group(latency.asc()),
    )
    if since is not None:
        stmt = stmt.where(Story.discovered_at >= since)

    def _run(s: Session) -> dict[str, float | int | None]:
        count, p50, p95 = s.execute(stmt).one()
        return {
            "count": int(count or 0),
            "p50": float(p50) if p50 is not None else None,
            "p95": float(p95) if p95 is not None else None,
        }

    if session is not None:
        return _run(session)
    with session_scope() as own_session:
        return _run(own_session)


def metric_summary(
    metric: str,
    *,
    since: dt.datetime | None = None,
    session: Session | None = None,
) -> dict[str, float | int | None]:
    """count / sum / avg / min / max for one metric over an optional window."""
    value = cast(MetricSample.value, Float)
    stmt = select(
        func.count(),
        func.sum(value),
        func.avg(value),
        func.min(value),
        func.max(value),
    ).where(MetricSample.metric == metric)
    if since is not None:
        stmt = stmt.where(MetricSample.recorded_at >= since)

    def _run(s: Session) -> dict[str, float | int | None]:
        count, total, avg, lo, hi = s.execute(stmt).one()
        return {
            "count": int(count or 0),
            "sum": float(total) if total is not None else None,
            "avg": float(avg) if avg is not None else None,
            "min": float(lo) if lo is not None else None,
            "max": float(hi) if hi is not None else None,
        }

    if session is not None:
        return _run(session)
    with session_scope() as own_session:
        return _run(own_session)


def daily_spend(
    *, day: dt.date | None = None, session: Session | None = None
) -> float:
    """Estimated model spend for one UTC day - the budget alert input (SPEC 7.8)."""
    target_day = day or dt.datetime.now(dt.UTC).date()
    start = dt.datetime.combine(target_day, dt.time.min, tzinfo=dt.UTC)
    end = start + dt.timedelta(days=1)

    stmt = select(func.sum(cast(MetricSample.value, Float))).where(
        MetricSample.metric == M_ESTIMATED_SPEND,
        MetricSample.recorded_at >= start,
        MetricSample.recorded_at < end,
    )

    def _run(s: Session) -> float:
        return float(s.execute(stmt).scalar() or 0.0)

    if session is not None:
        return _run(session)
    with session_scope() as own_session:
        return _run(own_session)
