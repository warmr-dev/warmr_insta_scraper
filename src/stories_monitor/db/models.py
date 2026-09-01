"""SQLAlchemy models. Table/column names are normative per SPEC section 5."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class WorkerAccount(Base):
    """Our operational Instagram accounts."""

    __tablename__ = "worker_accounts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    password_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    shard_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # Bound permanently at creation. Never rotate (SPEC section 8).
    proxy_url: Mapped[str] = mapped_column(Text, nullable=False)
    # Generated once at creation, immutable (SPEC section 8).
    device_settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    session_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # warming | active | challenged | banned | reserve
    status: Mapped[str] = mapped_column(Text, nullable=False)
    phase_offset_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    follows_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_poll_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Target(Base):
    """The ~57k accounts we monitor."""

    __tablename__ = "targets"

    # Instagram numeric pk, NOT username - usernames change, ids do not.
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    instagram_url: Mapped[str | None] = mapped_column(Text)
    shard_id: Mapped[int | None] = mapped_column(Integer)
    # active | private | deleted | unreachable
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    # The change-detection watermark: max latest_reel_media seen in any tray.
    last_reel_media_ts: Mapped[int | None] = mapped_column(BigInteger)
    last_seen_in_tray: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    imported_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_targets_shard_status", "shard_id", "status"),)


class TargetFollow(Base):
    """Which worker follows which target, and the state of that relationship."""

    __tablename__ = "target_follows"

    worker_account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("worker_accounts.id"), primary_key=True
    )
    target_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("targets.user_id"), primary_key=True
    )
    # queued | requested | following | rejected | failed
    state: Mapped[str] = mapped_column(Text, nullable=False)
    requested_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)

    # The follower process polls this constantly.
    __table_args__ = (Index("ix_target_follows_worker_state", "worker_account_id", "state"),)


class Story(Base):
    """One row per discovered story item. This is the dedup table."""

    __tablename__ = "stories"

    story_id: Mapped[str] = mapped_column(Text, primary_key=True)
    target_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("targets.user_id"), nullable=False
    )
    taken_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expiring_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    media_type: Mapped[int] = mapped_column(Integer, nullable=False)  # 1 = photo, 2 = video
    discovered_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # new | skipped_video | analyzing | analyzed | checked | sent | failed
    pipeline_state: Mapped[str] = mapped_column(Text, nullable=False, default="new")

    __table_args__ = (
        Index("ix_stories_pipeline_state", "pipeline_state"),
        Index("ix_stories_target", "target_user_id"),
    )


class StoryAnalysis(Base):
    __tablename__ = "story_analysis"

    story_id: Mapped[str] = mapped_column(
        Text, ForeignKey("stories.story_id"), primary_key=True
    )
    ocr_text: Mapped[str | None] = mapped_column(Text)
    cheap_score: Mapped[int | None] = mapped_column(Integer)
    cheap_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    smart_score: Mapped[int | None] = mapped_column(Integer)
    smart_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    final_score: Mapped[int | None] = mapped_column(Integer)
    service_category: Mapped[str | None] = mapped_column(Text)
    intent_type: Mapped[str | None] = mapped_column(Text)
    ai_explanation: Mapped[str | None] = mapped_column(Text)
    analyzed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class BusinessCheck(Base):
    __tablename__ = "business_checks"

    story_id: Mapped[str] = mapped_column(
        Text, ForeignKey("stories.story_id"), primary_key=True
    )
    vendor_id: Mapped[str | None] = mapped_column(Text)
    service_fit: Mapped[float | None] = mapped_column(Numeric)
    geo_ok: Mapped[bool | None] = mapped_column(Boolean)
    community_conflict: Mapped[bool | None] = mapped_column(Boolean)
    # approved | rejected | review
    final_status: Mapped[str | None] = mapped_column(Text)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class SlackDelivery(Base):
    """Primary key on story_id is the idempotency guarantee - never posted twice."""

    __tablename__ = "slack_deliveries"

    story_id: Mapped[str] = mapped_column(
        Text, ForeignKey("stories.story_id"), primary_key=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)  # pending | sent | failed
    slack_ts: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class AccountEvent(Base):
    """Audit trail for worker account incidents."""

    __tablename__ = "account_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    worker_account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # challenge | login_required | feedback_required | ban | recovered
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_account_events_worker", "worker_account_id", "occurred_at"),)


class DailyActionCounter(Base):
    """Rate-limit ledger. Survives restarts, unlike an in-memory counter."""

    __tablename__ = "daily_action_counters"

    worker_account_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    day: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    follows_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requests_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Cookie(Base):
    """Веб-куки одного аккаунта - одна строка на аккаунт.

    Отдельной таблицей, а не JSONB, чтобы строку можно было поправить руками в
    интерфейсе Supabase: веб-куки нельзя продлить из кода, поэтому ручное
    обновление - штатная операция, а не авария.
    """

    __tablename__ = "cookies"

    username: Mapped[str] = mapped_column(Text, primary_key=True)

    # Семь куки веб-API. Без ig_did/mid/datr/rur ленты отвечают 302.
    sessionid: Mapped[str] = mapped_column(Text, nullable=False)
    csrftoken: Mapped[str | None] = mapped_column(Text)
    ds_user_id: Mapped[str | None] = mapped_column(Text)
    ig_did: Mapped[str | None] = mapped_column(Text)
    mid: Mapped[str | None] = mapped_column(Text)
    datr: Mapped[str | None] = mapped_column(Text)
    rur: Mapped[str | None] = mapped_column(Text)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_cookies_active", "is_active"),)

    def as_jar(self) -> dict[str, str]:
        """Куки в виде словаря для WebTransport, пустые значения отброшены."""
        names = ("sessionid", "csrftoken", "ds_user_id", "ig_did", "mid", "datr", "rur")
        return {n: v for n in names if (v := getattr(self, n))}


class MetricSample(Base):
    """Simple stats table backing the /metrics endpoint (SPEC section 10)."""

    __tablename__ = "metric_samples"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    metric: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[float] = mapped_column(Numeric, nullable=False)
    labels: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    recorded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_metric_samples_metric_time", "metric", "recorded_at"),)


class ActivityLog(Base):
    """What each web session is doing, step by step (migration 0004).

    Keyed by `username` to match `cookies`, not by `worker_accounts.id` like
    `AccountEvent` - the web path has no worker rows.
    """

    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    # poll | stories_found | ai_scoring | ai_scored | lead | error | cycle
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="ok")
    message: Mapped[str | None] = mapped_column(Text)
    targets: Mapped[list[str] | None] = mapped_column(JSONB)
    item_count: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_activity_log_recent", "occurred_at"),
        Index("ix_activity_log_account", "username", "occurred_at"),
    )
