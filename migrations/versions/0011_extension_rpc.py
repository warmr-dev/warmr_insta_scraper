"""Four functions the Chrome extension may call, and nothing else.

The extension needs to claim targets, report outcomes and refresh its cookies,
but it runs on a laptop where its anon key is readable by anyone with access to
the machine. Migration 0003 locked every table against `anon` precisely because
`cookies` holds live Instagram sessions, and it says in as many words not to
add anon policies to work around that.

So this does not add table policies. It adds SECURITY DEFINER functions: the
tables stay unreadable to `anon`, and the only things the key can do are the
four operations below, with the shapes they enforce. A leaked key costs a
scrambled follow queue - not the session cookies, not the leads, not the
stories.

`ext_claim_targets` exists as a function for a second reason: PostgREST cannot
express `FOR UPDATE SKIP LOCKED`, and without it two Chrome profiles claiming at
the same moment would be handed the same target. That is the one guarantee this
whole assignment layer exists to provide, so the claim has to be one atomic
statement inside the database rather than a read followed by a write.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CLAIM = """
CREATE OR REPLACE FUNCTION ext_claim_targets(p_session text, p_limit int DEFAULT 10)
RETURNS TABLE (target_user_id bigint, username text, is_private boolean)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    IF p_session IS NULL OR length(trim(p_session)) = 0 THEN
        RAISE EXCEPTION 'session is required';
    END IF;

    -- Only a session we actually know about may claim. Otherwise a typo in the
    -- popup silently creates a phantom owner whose targets nothing will ever
    -- reclaim, because reclaim keys on rows in `cookies`.
    IF NOT EXISTS (SELECT 1 FROM cookies c WHERE c.username = p_session) THEN
        RAISE EXCEPTION 'unknown session %', p_session;
    END IF;

    RETURN QUERY
    WITH picked AS (
        SELECT f.target_user_id
        FROM session_follows f
        WHERE f.session_username IS NULL
          AND f.state = 'free'
          AND f.attempts < 3
        -- Public first: a private target yields only a pending request whose
        -- stories stay invisible until a human approves, and requesting is a
        -- louder spam signal than following.
        ORDER BY f.is_private, f.attempts, f.target_user_id
        LIMIT greatest(1, least(coalesce(p_limit, 10), 50))
        FOR UPDATE SKIP LOCKED
    )
    UPDATE session_follows f
       SET session_username = p_session,
           state = 'claimed',
           claimed_at = now()
      FROM picked p
     WHERE f.target_user_id = p.target_user_id
    RETURNING f.target_user_id, f.username, f.is_private;
END;
$$;
"""

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
        -- before anyone noticed.
        UPDATE session_follows
           SET state = 'free',
               session_username = NULL,
               claimed_at = NULL,
               is_checking = false,
               last_error = left(coalesce(p_detail, p_outcome), 1000)
         WHERE target_user_id = p_target;

    ELSIF p_outcome = 'unavailable' THEN
        -- Deleted, renamed or suspended. Retrying cannot help.
        UPDATE session_follows
           SET state = 'unavailable',
               session_username = NULL,
               claimed_at = NULL,
               is_checking = false,
               attempts = attempts + 1,
               last_attempt_by = p_session,
               last_error = left(p_detail, 1000)
         WHERE target_user_id = p_target;

    ELSE
        -- Anything else: count the attempt, and hand the row back while it is
        -- still under the retry ceiling.
        UPDATE session_follows
           SET attempts = attempts + 1,
               last_attempt_by = p_session,
               last_error = left(p_detail, 1000),
               is_checking = false,
               state = CASE WHEN attempts + 1 < 3 THEN 'free' ELSE 'failed' END,
               session_username = CASE WHEN attempts + 1 < 3 THEN NULL ELSE session_username END,
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

CHECKING = """
CREATE OR REPLACE FUNCTION ext_begin_check(p_session text, p_target bigint)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    UPDATE session_follows
       SET is_checking = true,
           followed_by = p_session,
           last_checked_at = now()
     WHERE target_user_id = p_target;
END;
$$;
"""

COOKIES = """
CREATE OR REPLACE FUNCTION ext_save_cookies(
    p_username text,
    p_sessionid text,
    p_csrftoken text,
    p_ds_user_id text,
    p_ig_did text,
    p_mid text,
    p_datr text,
    p_rur text,
    p_user_agent text DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    IF p_sessionid IS NULL OR length(p_sessionid) < 10 THEN
        RAISE EXCEPTION 'sessionid is required';
    END IF;

    INSERT INTO cookies AS c
        (username, sessionid, csrftoken, ds_user_id, ig_did, mid, datr, rur,
         user_agent, is_active, updated_at, last_error)
    VALUES
        (lower(trim(p_username)), p_sessionid, p_csrftoken, p_ds_user_id, p_ig_did,
         p_mid, p_datr, p_rur, p_user_agent, true, now(), NULL)
    ON CONFLICT (username) DO UPDATE SET
        sessionid   = EXCLUDED.sessionid,
        csrftoken   = EXCLUDED.csrftoken,
        ds_user_id  = EXCLUDED.ds_user_id,
        ig_did      = EXCLUDED.ig_did,
        mid         = EXCLUDED.mid,
        datr        = EXCLUDED.datr,
        rur         = EXCLUDED.rur,
        user_agent  = COALESCE(EXCLUDED.user_agent, c.user_agent),
        -- Re-activate: a session disabled for stale cookies is exactly the one
        -- a refresh is meant to fix, and leaving it off means a human still has
        -- to notice and flip it back.
        is_active   = true,
        updated_at  = now(),
        last_error  = NULL;

    INSERT INTO activity_log (username, phase, status, message)
    VALUES (lower(trim(p_username)), 'cookies', 'ok', 'refreshed by extension');
END;
$$;
"""

FUNCTIONS = (
    ("ext_claim_targets(text, int)", CLAIM),
    ("ext_report_follow(text, bigint, text, text, text)", REPORT),
    ("ext_begin_check(text, bigint)", CHECKING),
    (
        "ext_save_cookies(text, text, text, text, text, text, text, text, text)",
        COOKIES,
    ),
)


def upgrade() -> None:
    # `session_follows` was created by migration 0008, AFTER 0003 revoked table
    # access from anon, so it never got that REVOKE. RLS with no policies means
    # no rows leak either way, but leaving the GRANT in place makes the fleet's
    # security depend on RLS alone rather than on RLS *and* privileges - and the
    # extension is about to hand this key to every laptop running a profile.
    op.execute("REVOKE ALL ON session_follows FROM anon, authenticated")

    for signature, body in FUNCTIONS:
        op.execute(body)
        # SECURITY DEFINER means these run as the owner, so the tables stay
        # closed to anon while exactly these four calls are reachable.
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO anon, authenticated")


def downgrade() -> None:
    for signature, _ in FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM anon, authenticated")
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
