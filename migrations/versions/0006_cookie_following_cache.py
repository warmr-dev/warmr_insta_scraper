"""Cache each session's following list.

The web API has no story tray, so the accounts to ask about must be enumerated
via `friendships/<id>/following/`. That endpoint is throttled independently of
the feeds: measured across 11 sessions, four answered 401 there while serving
`reels_media` normally with the same cookies seconds apart.

Without a cache, a throttled graph call means no stories that cycle - and a
session that looks broken while being perfectly healthy. Following lists change
slowly (people are followed once and stay followed), so a cached copy is a
faithful substitute, and re-fetching it every cycle was wasted traffic anyway.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [[user_id, username], ...] - read back whole, never queried into.
    op.add_column("cookies", sa.Column("following", postgresql.JSONB(), nullable=True))
    op.add_column(
        "cookies",
        sa.Column("following_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cookies", "following_at")
    op.drop_column("cookies", "following")
