"""Initial schema (SPEC section 5).

Table and column names are normative - later phases reference them by name.

Revision ID: 0001
Revises:
Create Date: 2026-01-01 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- worker_accounts: our operational Instagram accounts -------------------
    op.create_table(
        "worker_accounts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_enc", sa.LargeBinary(), nullable=False),
        sa.Column("shard_id", sa.Integer(), nullable=False),
        # Bound permanently at creation. Never rotate (SPEC section 8).
        sa.Column("proxy_url", sa.Text(), nullable=False),
        # Generated once at creation, immutable (SPEC section 8).
        sa.Column("device_settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("session_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        # warming | active | challenged | banned | reserve
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("phase_offset_sec", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("follows_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_worker_accounts"),
        sa.UniqueConstraint("username", name="uq_worker_accounts_username"),
    )

    # --- targets: the ~57k accounts we monitor ---------------------------------
    op.create_table(
        "targets",
        # Instagram numeric pk, NOT username - usernames change, ids do not.
        sa.Column("user_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("instagram_url", sa.Text(), nullable=True),
        sa.Column("shard_id", sa.Integer(), nullable=True),
        # active | private | deleted | unreachable
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        # The change-detection watermark: max latest_reel_media seen in any tray.
        sa.Column("last_reel_media_ts", sa.BigInteger(), nullable=True),
        sa.Column("last_seen_in_tray", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_targets"),
    )
    op.create_index("ix_targets_shard_status", "targets", ["shard_id", "status"])

    # --- target_follows: worker -> target relationship state -------------------
    op.create_table(
        "target_follows",
        sa.Column("worker_account_id", sa.BigInteger(), nullable=False),
        sa.Column("target_user_id", sa.BigInteger(), nullable=False),
        # queued | requested | following | rejected | failed
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["worker_account_id"],
            ["worker_accounts.id"],
            name="fk_target_follows_worker_account_id",
        ),
        sa.ForeignKeyConstraint(
            ["target_user_id"],
            ["targets.user_id"],
            name="fk_target_follows_target_user_id",
        ),
        sa.PrimaryKeyConstraint("worker_account_id", "target_user_id", name="pk_target_follows"),
    )
    # The follower process polls this constantly.
    op.create_index(
        "ix_target_follows_worker_state", "target_follows", ["worker_account_id", "state"]
    )

    # --- stories: one row per discovered item; the dedup table -----------------
    op.create_table(
        "stories",
        sa.Column("story_id", sa.Text(), nullable=False),
        sa.Column("target_user_id", sa.BigInteger(), nullable=False),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expiring_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("media_type", sa.Integer(), nullable=False),  # 1 = photo, 2 = video
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # new | skipped_video | analyzing | analyzed | checked | sent | failed
        sa.Column(
            "pipeline_state", sa.Text(), nullable=False, server_default=sa.text("'new'")
        ),
        sa.ForeignKeyConstraint(
            ["target_user_id"], ["targets.user_id"], name="fk_stories_target_user_id"
        ),
        sa.PrimaryKeyConstraint("story_id", name="pk_stories"),
    )
    op.create_index("ix_stories_pipeline_state", "stories", ["pipeline_state"])
    op.create_index("ix_stories_target", "stories", ["target_user_id"])

    # --- story_analysis --------------------------------------------------------
    op.create_table(
        "story_analysis",
        sa.Column("story_id", sa.Text(), nullable=False),
        sa.Column("ocr_text", sa.Text(), nullable=True),
        sa.Column("cheap_score", sa.Integer(), nullable=True),
        sa.Column("cheap_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("smart_score", sa.Integer(), nullable=True),
        sa.Column("smart_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("final_score", sa.Integer(), nullable=True),
        sa.Column("service_category", sa.Text(), nullable=True),
        sa.Column("intent_type", sa.Text(), nullable=True),
        sa.Column("ai_explanation", sa.Text(), nullable=True),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["story_id"], ["stories.story_id"], name="fk_story_analysis_story_id"
        ),
        sa.PrimaryKeyConstraint("story_id", name="pk_story_analysis"),
    )

    # --- business_checks -------------------------------------------------------
    op.create_table(
        "business_checks",
        sa.Column("story_id", sa.Text(), nullable=False),
        sa.Column("vendor_id", sa.Text(), nullable=True),
        sa.Column("service_fit", sa.Numeric(), nullable=True),
        sa.Column("geo_ok", sa.Boolean(), nullable=True),
        sa.Column("community_conflict", sa.Boolean(), nullable=True),
        # approved | rejected | review
        sa.Column("final_status", sa.Text(), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["story_id"], ["stories.story_id"], name="fk_business_checks_story_id"
        ),
        sa.PrimaryKeyConstraint("story_id", name="pk_business_checks"),
    )

    # --- slack_deliveries: pk on story_id IS the idempotency guarantee ---------
    op.create_table(
        "slack_deliveries",
        sa.Column("story_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),  # pending | sent | failed
        sa.Column("slack_ts", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["story_id"], ["stories.story_id"], name="fk_slack_deliveries_story_id"
        ),
        sa.PrimaryKeyConstraint("story_id", name="pk_slack_deliveries"),
    )

    # --- account_events: audit trail for worker account incidents --------------
    op.create_table(
        "account_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("worker_account_id", sa.BigInteger(), nullable=False),
        # challenge | login_required | feedback_required | ban | recovered
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_account_events"),
    )
    op.create_index(
        "ix_account_events_worker", "account_events", ["worker_account_id", "occurred_at"]
    )

    # --- daily_action_counters: rate-limit ledger, survives restarts -----------
    op.create_table(
        "daily_action_counters",
        sa.Column("worker_account_id", sa.BigInteger(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("follows_done", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("requests_done", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.PrimaryKeyConstraint("worker_account_id", "day", name="pk_daily_action_counters"),
    )

    # --- metric_samples: simple stats table (SPEC section 10) ------------------
    op.create_table(
        "metric_samples",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("metric", sa.String(length=128), nullable=False),
        sa.Column("value", sa.Numeric(), nullable=False),
        sa.Column("labels", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_metric_samples"),
    )
    op.create_index(
        "ix_metric_samples_metric_time", "metric_samples", ["metric", "recorded_at"]
    )


def downgrade() -> None:
    # Reverse dependency order: children before parents.
    op.drop_index("ix_metric_samples_metric_time", table_name="metric_samples")
    op.drop_table("metric_samples")

    op.drop_table("daily_action_counters")

    op.drop_index("ix_account_events_worker", table_name="account_events")
    op.drop_table("account_events")

    op.drop_table("slack_deliveries")
    op.drop_table("business_checks")
    op.drop_table("story_analysis")

    op.drop_index("ix_stories_target", table_name="stories")
    op.drop_index("ix_stories_pipeline_state", table_name="stories")
    op.drop_table("stories")

    op.drop_index("ix_target_follows_worker_state", table_name="target_follows")
    op.drop_table("target_follows")

    op.drop_index("ix_targets_shard_status", table_name="targets")
    op.drop_table("targets")

    op.drop_table("worker_accounts")
