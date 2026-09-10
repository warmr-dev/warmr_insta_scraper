"""Clear `followed_by` when a target goes back in the pool.

`followed_by` is the dashboard's answer to "which session follows this account".
The release paths in `ext_report_follow` were not clearing it, so a target that
failed and returned to the pool kept naming the session that had failed on it -
290 rows in practice, of which 287 were not followed by anyone at all. The
dashboard read that as coverage the fleet did not have.

Releasing is exactly the moment that answer stops being true, so it is cleared
there. `last_attempt_by` still records who tried, which is what the reclaim
logic needs; `followed_by` is only ever set on success now.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


REPORT = """
CREATE OR REPLACE FUNCTION ext_report_follow(
    p_session text,
    p_target bigint,
    p_username text,
    p_outcome text,
    p_detail text DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    IF p_outcome IN ('following', 'requested') THEN
        UPDATE session_follows
           SET state = p_outcome,
               followed_by = p_session,
               followed_at = now(),
               last_checked_at = now(),
               is_checking = false,
               last_error = NULL
         WHERE target_user_id = p_target;

    ELSIF p_outcome IN ('blocked', 'throttled') THEN
        -- The SESSION failed, not the target. Release it without counting an
        -- attempt: one bad profile would otherwise burn every target's retries
        -- before anyone noticed. `followed_by` goes too - nobody follows this
        -- account now, and leaving the name there reads as coverage.
        UPDATE session_follows
           SET state = 'free',
               session_username = NULL,
               followed_by = NULL,
               claimed_at = NULL,
               is_checking = false,
               last_error = left(coalesce(p_detail, p_outcome), 1000)
         WHERE target_user_id = p_target;

    ELSIF p_outcome = 'unavailable' THEN
        UPDATE session_follows
           SET state = 'unavailable',
               session_username = NULL,
               followed_by = NULL,
               claimed_at = NULL,
               is_checking = false,
               attempts = attempts + 1,
               last_attempt_by = p_session,
               last_error = left(p_detail, 1000)
         WHERE target_user_id = p_target;

    ELSE
        UPDATE session_follows
           SET attempts = attempts + 1,
               last_attempt_by = p_session,
               last_error = left(p_detail, 1000),
               is_checking = false,
               state = CASE WHEN attempts + 1 < 3 THEN 'free' ELSE 'failed' END,
               session_username = CASE WHEN attempts + 1 < 3 THEN NULL ELSE session_username END,
               followed_by = NULL,
               claimed_at = CASE WHEN attempts + 1 < 3 THEN NULL ELSE claimed_at END
         WHERE target_user_id = p_target;
    END IF;

    INSERT INTO activity_log
        (username, phase, status, message, target_user_id, target_username)
    VALUES
        (p_session, 'follow', p_outcome, left(p_detail, 1000), p_target, left(p_username, 64));
END;
$$;
"""

# Repair the rows already carrying a stale name. Anything genuinely followed is
# reconciled separately against the live following list; this only removes an
# attribution that was never true.
BACKFILL = """
UPDATE session_follows
   SET followed_by = NULL
 WHERE followed_by IS NOT NULL
   AND state NOT IN ('following', 'requested');
"""


def upgrade() -> None:
    op.execute(REPORT)
    op.execute(
        "GRANT EXECUTE ON FUNCTION ext_report_follow(text, bigint, text, text, text) "
        "TO anon, authenticated"
    )
    op.execute(BACKFILL)


def downgrade() -> None:
    # The 0011 body is the previous definition; re-applying it here would mean
    # keeping two copies in sync, and the only difference is a NULL that should
    # never have been left set. Downgrading drops the function, and 0011's
    # upgrade recreates it.
    op.execute("DROP FUNCTION IF EXISTS ext_report_follow(text, bigint, text, text, text)")
