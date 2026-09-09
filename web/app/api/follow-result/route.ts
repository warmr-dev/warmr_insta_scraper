import { NextResponse } from "next/server";
import { checkExtensionToken } from "@/lib/auth";
import { query } from "@/lib/db";

/**
 * Record what happened to one follow attempt, and log it against the target.
 *
 * Outcomes are deliberately attributed rather than lumped together, because
 * getting this wrong is expensive:
 *
 * - `following` / `requested` are terminal successes.
 * - `blocked` / `throttled` mean the SESSION failed, so the target goes back to
 *   the pool WITHOUT an attempt counted against it - otherwise one bad session
 *   would burn every target's retries before anyone noticed.
 * - `unavailable` (deleted, suspended) is terminal for the target.
 * - `failed` counts an attempt and releases the row for another session to try.
 */
export const dynamic = "force-dynamic";

const TERMINAL_OK = new Set(["following", "requested"]);
const SESSION_FAULT = new Set(["blocked", "throttled"]);

export async function POST(request: Request) {
  if (!checkExtensionToken(request)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  const body = await request.json().catch(() => ({}));
  const session = String(body.session ?? "").trim();
  const targetId = String(body.target_id ?? "").trim();
  const username = String(body.username ?? "").trim();
  const outcome = String(body.outcome ?? "").trim();
  const detail = String(body.detail ?? "").slice(0, 500) || null;

  if (!session || !targetId || !outcome) {
    return NextResponse.json(
      { error: "session, target_id and outcome are required" },
      { status: 400 },
    );
  }

  if (TERMINAL_OK.has(outcome)) {
    await query(
      `UPDATE session_follows
          SET state = $3, followed_by = $1, followed_at = now(),
              last_checked_at = now(), is_checking = false, last_error = NULL
        WHERE target_user_id = $2`,
      [session, targetId, outcome],
    );
  } else if (SESSION_FAULT.has(outcome)) {
    // The target is innocent: release it without blaming it.
    await query(
      `UPDATE session_follows
          SET state = 'free', session_username = NULL, claimed_at = NULL,
              is_checking = false, last_error = $2
        WHERE target_user_id = $1`,
      [targetId, detail ?? outcome],
    );
  } else if (outcome === "unavailable") {
    await query(
      `UPDATE session_follows
          SET state = 'unavailable', session_username = NULL, claimed_at = NULL,
              is_checking = false, attempts = attempts + 1, last_error = $2,
              last_attempt_by = $3
        WHERE target_user_id = $1`,
      [targetId, detail, session],
    );
  } else {
    // A generic failure: count it, and hand the row back below the retry ceiling.
    await query(
      `UPDATE session_follows
          SET attempts = attempts + 1,
              last_error = $2,
              last_attempt_by = $3,
              is_checking = false,
              state = CASE WHEN attempts + 1 < 3 THEN 'free' ELSE 'failed' END,
              session_username = CASE WHEN attempts + 1 < 3 THEN NULL ELSE session_username END,
              claimed_at = CASE WHEN attempts + 1 < 3 THEN NULL ELSE claimed_at END
        WHERE target_user_id = $1`,
      [targetId, detail, session],
    );
  }

  // The activity trail: which session token acted on which account, and how.
  await query(
    `INSERT INTO activity_log
       (username, phase, status, message, target_user_id, target_username)
     VALUES ($1, 'follow', $2, $3, $4, $5)`,
    [session, outcome, detail, targetId, username || null],
  );

  return NextResponse.json({ ok: true });
}
