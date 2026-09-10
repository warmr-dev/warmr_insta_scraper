"""A function to confirm this session still owns a target.

The extension keeps its queue in `chrome.storage.local`, which survives
reloads, upgrades and restarts. That is deliberate - a crash should not lose the
batch - but it means the queue can outlive the claim behind it: a target that
was released, reclaimed by another session, or already followed still sits in
the local list, and the extension has no way to notice.

That is not hypothetical. One profile followed the same account repeatedly from
a queue whose rows the database had long since freed, because nothing between
the queue and the click ever asked the database whether the claim still stood.

`ext_owns_target` is that question, in one round trip: true only when this
session still holds the row AND it is not already followed.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OWNS = """
CREATE OR REPLACE FUNCTION ext_owns_target(p_session text, p_target bigint)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT EXISTS (
        SELECT 1 FROM session_follows
         WHERE target_user_id = p_target
           AND session_username = p_session
           AND state = 'claimed'
    );
$$;
"""


def upgrade() -> None:
    op.execute(OWNS)
    op.execute(
        "GRANT EXECUTE ON FUNCTION ext_owns_target(text, bigint) TO anon, authenticated"
    )


def downgrade() -> None:
    op.execute("REVOKE ALL ON FUNCTION ext_owns_target(text, bigint) FROM anon, authenticated")
    op.execute("DROP FUNCTION IF EXISTS ext_owns_target(text, bigint)")
