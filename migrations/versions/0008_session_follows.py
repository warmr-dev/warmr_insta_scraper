"""Follow assignments owned by a cookie session, and reclaimable when it dies.

`target_follows` already tracks who follows whom, but it is keyed by
`worker_accounts.id` - the password-login accounts. The fleet that actually runs
is the cookie sessions in `cookies`, keyed by username, and those two systems
never met. This table is the missing join: it lets a *session* own a follow.

The design point is reclaim. When a session dies holding 256 follows, those 256
targets must become available to the surviving sessions without a human picking
through them - so ownership is a nullable column, not a composite primary key.
Releasing is then `UPDATE ... SET session_username = NULL`, and any live session
can claim the row next. A composite PK of (session, target) would have forced a
delete-and-reinsert of every row instead, which is both slower and loses the
history of who tried what.

`claimed_at` exists so a row a session took but never acted on - because the
process was killed between claim and follow - can be swept back rather than
sitting reserved forever.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "session_follows",
        # autoincrement=False: this is Instagram's own pk. Left to Alembic's
        # default a BigInteger primary key becomes BIGSERIAL, and a row inserted
        # without an explicit id would silently get a made-up target id.
        sa.Column("target_user_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        # NULL = unassigned and free for any session to claim. This is the whole
        # reclaim mechanism.
        sa.Column("session_username", sa.Text(), nullable=True),
        # free | claimed | following | requested | failed | unavailable
        sa.Column("state", sa.Text(), nullable=False, server_default="free"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("followed_at", sa.DateTime(timezone=True), nullable=True),
        # Who made the last attempt - survives the row being released, so a
        # dying session's failures can be forgiven without forgiving failures
        # recorded by healthy sessions.
        sa.Column("last_attempt_by", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("target_user_id"),
        sa.ForeignKeyConstraint(
            ["session_username"],
            ["cookies.username"],
            # A session row being deleted must free its follows, never delete
            # the targets themselves - the target list is the expensive asset.
            ondelete="SET NULL",
        ),
    )

    # The claim query: "give me free rows, cheapest first". Partial, because
    # only unassigned rows are ever scanned by it and the table is ~57k wide.
    op.create_index(
        "ix_session_follows_free",
        "session_follows",
        ["state", "attempts"],
        postgresql_where=sa.text("session_username IS NULL"),
    )
    # The per-session view the dashboard and the reclaim sweep both need.
    op.create_index(
        "ix_session_follows_owner",
        "session_follows",
        ["session_username", "state"],
    )

    # Deny by default, exactly as migration 0003 does for every other table.
    # This one names live session usernames, so leaving it readable by `anon`
    # would leak the fleet's account list to anyone holding the public key.
    # The scraper connects as `postgres` (BYPASSRLS) and is unaffected.
    op.execute("ALTER TABLE session_follows ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index("ix_session_follows_owner", table_name="session_follows")
    op.drop_index("ix_session_follows_free", table_name="session_follows")
    op.drop_table("session_follows")
