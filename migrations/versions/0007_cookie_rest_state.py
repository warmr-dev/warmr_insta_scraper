"""Record when an account is resting, and why.

Rest lives in the collector's memory, which is the right place for it - but it
means the dashboard cannot say why an account produced nothing this cycle. An
operator seeing "0 stories" has no way to tell a resting account from a broken
one, which is exactly the confusion that made a healthy session look dead
earlier.

`rest_until` is the wall-clock moment the account may be polled again;
`rest_strikes` is how many consecutive rate-limits it has taken, so the
dashboard can distinguish one unlucky cycle from an account Instagram is
actively pushing back on.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cookies", sa.Column("rest_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "cookies",
        sa.Column("rest_strikes", sa.Integer(), nullable=False, server_default="0"),
    )
    # Requests in the current 24h window, for the daily budget. Kept here rather
    # than counted from activity_log on every cycle: that table is pruned, and a
    # budget that resets when logs are pruned is not a budget.
    op.add_column(
        "cookies",
        sa.Column("requests_today", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "cookies", sa.Column("requests_reset_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("cookies", "requests_reset_at")
    op.drop_column("cookies", "requests_today")
    op.drop_column("cookies", "rest_strikes")
    op.drop_column("cookies", "rest_until")
