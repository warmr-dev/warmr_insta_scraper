import { NextResponse } from "next/server";
import { checkExtensionToken } from "@/lib/auth";
import { query } from "@/lib/db";

/**
 * Hand the extension its next batch of accounts to follow.
 *
 * The claim is the same one the Python follower uses, for the same reason:
 * `FOR UPDATE SKIP LOCKED` plus a single UPDATE means two Chrome profiles
 * running at once can never be given the same target. Without it, two profiles
 * would follow the same account from two different sessions - wasted budget on
 * a target that is already covered.
 *
 * Public accounts come first: a private one only yields a pending request whose
 * stories stay invisible until a human approves, and requesting is a louder
 * spam signal than following.
 */
export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  if (!checkExtensionToken(request)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  const body = await request.json().catch(() => ({}));
  const session = String(body.session ?? "").trim();
  const limit = Math.min(Math.max(Number(body.limit) || 10, 1), 50);

  if (!session) {
    return NextResponse.json({ error: "session is required" }, { status: 400 });
  }

  const rows = await query<{ target_user_id: string; username: string; is_private: boolean }>(
    `
    WITH picked AS (
      SELECT target_user_id
      FROM session_follows
      WHERE session_username IS NULL
        AND state = 'free'
        AND attempts < 3
      ORDER BY is_private, attempts, target_user_id
      LIMIT $2
      FOR UPDATE SKIP LOCKED
    )
    UPDATE session_follows f
       SET session_username = $1,
           state = 'claimed',
           claimed_at = now()
      FROM picked p
     WHERE f.target_user_id = p.target_user_id
    RETURNING f.target_user_id, f.username, f.is_private
    `,
    [session, limit],
  );

  return NextResponse.json({
    session,
    targets: rows.map((r) => ({
      id: String(r.target_user_id),
      username: r.username,
      is_private: r.is_private,
      url: `https://www.instagram.com/${r.username}/`,
    })),
  });
}
