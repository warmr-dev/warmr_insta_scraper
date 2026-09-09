import { NextResponse } from "next/server";
import { checkExtensionToken } from "@/lib/auth";
import { query } from "@/lib/db";
import { invalidate } from "@/lib/cache";

/**
 * Store cookies harvested by the extension from a live browser profile.
 *
 * This is the whole reason the extension exists. Cookies pasted by hand go
 * stale within days and, replayed from another machine, authenticate reads but
 * not writes - Instagram serves a logged-out `fb_dtsg` to a session it does not
 * recognise as the browser that logged in. Cookies read from inside the profile
 * that IS logged in have no such problem, and refreshing them on a timer keeps
 * collection alive without anyone re-pasting anything.
 *
 * Unlike /api/sessions this does not verify against Instagram first: the
 * extension only ever sends cookies it just read from a working, logged-in
 * profile, and a verification round-trip here would cost the save whenever
 * Instagram is slow.
 */
export const dynamic = "force-dynamic";
export const maxDuration = 30;

const COOKIE_NAMES = [
  "sessionid",
  "csrftoken",
  "ds_user_id",
  "ig_did",
  "mid",
  "datr",
  "rur",
] as const;

export async function POST(request: Request) {
  if (!checkExtensionToken(request)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  const body = await request.json().catch(() => ({}));
  const username = String(body.username ?? "").trim().toLowerCase();
  const cookies = (body.cookies ?? {}) as Record<string, string>;
  const userAgent = String(body.user_agent ?? "").slice(0, 500) || null;

  if (!username) {
    return NextResponse.json({ error: "username is required" }, { status: 400 });
  }
  if (!cookies.sessionid) {
    return NextResponse.json(
      { error: "cookies must include sessionid" },
      { status: 400 },
    );
  }

  const values = COOKIE_NAMES.map((name) => cookies[name] ?? null);
  const missing = COOKIE_NAMES.filter((n) => !cookies[n]);

  await query(
    `
    INSERT INTO cookies
      (username, sessionid, csrftoken, ds_user_id, ig_did, mid, datr, rur,
       user_agent, is_active, updated_at, last_error)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, true, now(), NULL)
    ON CONFLICT (username) DO UPDATE SET
      sessionid = EXCLUDED.sessionid,
      csrftoken = EXCLUDED.csrftoken,
      ds_user_id = EXCLUDED.ds_user_id,
      ig_did = EXCLUDED.ig_did,
      mid = EXCLUDED.mid,
      datr = EXCLUDED.datr,
      rur = EXCLUDED.rur,
      user_agent = COALESCE(EXCLUDED.user_agent, cookies.user_agent),
      -- A refresh revives a session that was disabled when its cookies expired:
      -- that is the point of refreshing, and leaving it inactive would mean a
      -- human still has to notice and flip it back.
      is_active = true,
      updated_at = now(),
      last_error = NULL
    `,
    [username, ...values, userAgent],
  );

  await query(
    `INSERT INTO activity_log (username, phase, status, message)
     VALUES ($1, 'cookies', 'ok', $2)`,
    [username, `refreshed by extension${missing.length ? `, missing: ${missing.join(", ")}` : ""}`],
  );

  invalidate();

  return NextResponse.json({ ok: true, username, missing });
}
