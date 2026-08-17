"""Dedicated cookies table - one web-cookie set per account row.

Separate from worker_accounts JSONB so rows can be edited in the DB UI when
web cookies expire. Web cookies cannot be renewed from code, so manual updates
are routine operations, not emergencies.

The seven cookies are all required: without ig_did/mid/datr/rur the feed
endpoints answer 302 - measured, not assumed.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cookies",
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("sessionid", sa.Text(), nullable=False),
        sa.Column("csrftoken", sa.Text(), nullable=True),
        sa.Column("ds_user_id", sa.Text(), nullable=True),
        sa.Column("ig_did", sa.Text(), nullable=True),
        sa.Column("mid", sa.Text(), nullable=True),
        sa.Column("datr", sa.Text(), nullable=True),
        sa.Column("rur", sa.Text(), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("username", name="pk_cookies"),
    )
    op.create_index("ix_cookies_active", "cookies", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_cookies_active", table_name="cookies")
    op.drop_table("cookies")
