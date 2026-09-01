"""Per-session activity log - what each account is doing, right now.

The dashboard could already show that a session was alive or dead, but not what
it was *doing*: which followings it polled, which stories it pulled, what went
to the AI and what came back. When collection looked wrong the only recourse was
reading container logs, which are per-process and gone on redeploy.

Rows are keyed by `username` to match the `cookies` table, NOT by
`worker_accounts.id` like `account_events` - the web path has no worker rows, so
the existing audit table cannot describe it.

`targets` holds the handles an event touched (the "fetches stories of 1..2..3"
detail). It is JSONB rather than a child table because it is only ever read back
whole, for display.

Retention is the caller's job (`prune_activity`): at ~11 accounts a minute this
table grows fast, and nobody looks at yesterday's poll.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "activity_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        # Matches cookies.username. Deliberately not a foreign key: an event
        # explaining why a session was removed must outlive the session row.
        sa.Column("username", sa.Text(), nullable=False),
        # poll | stories_found | ai_scoring | ai_scored | lead | error | cycle
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="ok"),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("targets", postgresql.JSONB(), nullable=True),
        sa.Column("item_count", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_activity_log"),
    )
    # The dashboard reads newest-first, either for one account or for all of
    # them; both are served by this index.
    op.create_index(
        "ix_activity_log_recent", "activity_log", ["occurred_at"], postgresql_using="btree"
    )
    op.create_index("ix_activity_log_account", "activity_log", ["username", "occurred_at"])

    # Same lockdown as migration 0003: every table denies anon by default. This
    # one carries target handles and account names, and an unlocked table would
    # be the single exception in the schema.
    op.execute("ALTER TABLE activity_log ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON activity_log FROM anon, authenticated")


def downgrade() -> None:
    op.drop_index("ix_activity_log_account", table_name="activity_log")
    op.drop_index("ix_activity_log_recent", table_name="activity_log")
    op.drop_table("activity_log")
