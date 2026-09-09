"""Say which target an activity row is about, and clear the log weekly.

The activity log could say "this session polled" but not "this session is
looking at THIS account", because `targets` is a JSONB array meant for the
several accounts one tray call covers. A single-target action - a follow, a
friendship check - had nowhere precise to go, so the dashboard could not answer
the question that actually gets asked: which session token is working on which
account.

`target_user_id` + `target_username` answer it, indexed so the per-target view
("who has been looking at this account, and when") is cheap.

The cron job replaces nothing: `activity.prune()` already exists but only runs
when a worker calls it, so a stopped fleet means a log that grows forever. A
database-side schedule keeps the table bounded whether or not anything is
running - which is exactly when an unbounded log hurts, since a full disk on
Supabase takes down collection too.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept for a week. Long enough to investigate "what happened on Tuesday",
# short enough that the table stays small at fleet scale.
RETENTION_DAYS = 7


def upgrade() -> None:
    op.add_column("activity_log", sa.Column("target_user_id", sa.BigInteger(), nullable=True))
    op.add_column("activity_log", sa.Column("target_username", sa.Text(), nullable=True))

    # "Everything that has happened to this account", newest first.
    op.create_index(
        "ix_activity_log_target",
        "activity_log",
        ["target_username", "occurred_at"],
        postgresql_where=sa.text("target_username IS NOT NULL"),
    )

    # The deletion itself, as a function so the schedule stays a one-liner and
    # the retention window lives in one place.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION prune_activity_log()
        RETURNS integer
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public
        AS $$
        DECLARE
            removed integer;
        BEGIN
            DELETE FROM activity_log
            WHERE occurred_at < now() - interval '{RETENTION_DAYS} days';
            GET DIAGNOSTICS removed = ROW_COUNT;
            RAISE NOTICE 'prune_activity_log removed % rows', removed;
            RETURN removed;
        END;
        $$;
        """
    )

    # pg_cron is available on Supabase but is NOT enabled by default, and it
    # only exists in the `postgres` database. Guarded so this migration still
    # applies on a plain Postgres (local dev, CI) that has no pg_cron at all -
    # there the function is created and simply never scheduled.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_cron') THEN
                CREATE EXTENSION IF NOT EXISTS pg_cron;

                -- Unschedule first so re-running the migration does not stack
                -- duplicate jobs under the same name.
                PERFORM cron.unschedule('prune-activity-log')
                WHERE EXISTS (
                    SELECT 1 FROM cron.job WHERE jobname = 'prune-activity-log'
                );

                -- 03:17 every Sunday. Deliberately not on the hour: scheduled
                -- work that lands exactly at 03:00 collides with everything
                -- else scheduled at 03:00.
                PERFORM cron.schedule(
                    'prune-activity-log',
                    '17 3 * * 0',
                    'SELECT prune_activity_log()'
                );
            ELSE
                RAISE NOTICE 'pg_cron unavailable - prune_activity_log() created but not scheduled';
            END IF;
        END;
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
                PERFORM cron.unschedule('prune-activity-log')
                WHERE EXISTS (
                    SELECT 1 FROM cron.job WHERE jobname = 'prune-activity-log'
                );
            END IF;
        END;
        $$;
        """
    )
    op.execute("DROP FUNCTION IF EXISTS prune_activity_log()")
    op.drop_index("ix_activity_log_target", table_name="activity_log")
    op.drop_column("activity_log", "target_username")
    op.drop_column("activity_log", "target_user_id")
