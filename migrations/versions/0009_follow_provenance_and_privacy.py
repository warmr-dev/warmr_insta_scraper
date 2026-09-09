"""Scrape provenance, privacy, and the one-session-per-target guarantee.

Three things the real import file forced:

1. `source_account` - the spreadsheet's own `followed_by` column names the
   account each target was SCRAPED FROM (kenmcelroyofficial, eddie, ...), which
   is not the session that follows it. Two different facts; conflating them
   would have made "who follows this target" unanswerable the moment a session
   died. So the file's value lands here and `followed_by` stays ours.

2. `followed_by` / `is_checking` - denormalised onto the row so the dashboard
   can answer "which session is watching this account, and is it working on it
   right now" without joining. `followed_by` is the session that actually holds
   the follow; `is_checking` is true only while a session is mid-action on it.

3. `is_private` + the ordering index - private targets yield a pending request
   rather than a follow, so the public ones are claimed first.

The partial unique index is the important line in this file. Ownership was
already single-row, but nothing at the schema level SAID a target may have only
one owner. Now the database refuses a second one.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("session_follows", sa.Column("source_account", sa.Text(), nullable=True))
    op.add_column("session_follows", sa.Column("followed_by", sa.Text(), nullable=True))
    op.add_column(
        "session_follows",
        sa.Column("is_checking", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "session_follows",
        sa.Column("is_private", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("session_follows", sa.Column("full_name", sa.Text(), nullable=True))
    op.add_column("session_follows", sa.Column("last_checked_at", sa.DateTime(timezone=True)))

    # Claim order: public accounts before private ones, then least-attempted.
    # Replaces the 0008 index, which did not know about privacy.
    op.drop_index("ix_session_follows_free", table_name="session_follows")
    op.create_index(
        "ix_session_follows_free",
        "session_follows",
        ["is_private", "attempts", "target_user_id"],
        postgresql_where=sa.text("session_username IS NULL AND state = 'free'"),
    )

    # One session per target, enforced by the database rather than by the
    # correctness of the claim query. A second owner is now impossible even if
    # a future code path forgets to check.
    op.create_index(
        "uq_session_follows_one_owner",
        "session_follows",
        ["target_user_id"],
        unique=True,
        postgresql_where=sa.text("session_username IS NOT NULL"),
    )

    # "What is this session working on right now" - the dashboard's live view.
    op.create_index(
        "ix_session_follows_checking",
        "session_follows",
        ["followed_by", "is_checking"],
        postgresql_where=sa.text("is_checking"),
    )


def downgrade() -> None:
    op.drop_index("ix_session_follows_checking", table_name="session_follows")
    op.drop_index("uq_session_follows_one_owner", table_name="session_follows")
    op.drop_index("ix_session_follows_free", table_name="session_follows")
    op.create_index(
        "ix_session_follows_free",
        "session_follows",
        ["state", "attempts"],
        postgresql_where=sa.text("session_username IS NULL"),
    )
    for column in (
        "last_checked_at",
        "full_name",
        "is_private",
        "is_checking",
        "followed_by",
        "source_account",
    ):
        op.drop_column("session_follows", column)
