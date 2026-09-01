import { NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { query } from "@/lib/db";
import { COOKIE_NAMES, checkSession, missingCookies, parseCookies } from "@/lib/cookies";
import { invalidate } from "@/lib/cache";

// Saving hits Instagram and then a database in Sydney. The platform default of
// 10s was not enough for both, and an aborted function returns an empty body -
// which is why the browser reported "Unexpected end of JSON input" rather than
// a readable error.
export const maxDuration = 60;

/** POST: add or replace a session's cookies. */
export async function POST(request: Request) {
  if (!(await getSession())) {
    return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  }

  const body = await request.json().catch(() => ({}));
  const username = String(body.username ?? "").trim();
  const raw = String(body.cookies ?? "");
  // Sent by the paste form; absent for API callers, in which case the scraper
  // falls back to its shared default and warns.
  const userAgent = String(body.user_agent ?? "").slice(0, 500) || null;

  if (!username) {
    return NextResponse.json({ error: "username is required" }, { status: 400 });
  }

  const jar = parseCookies(raw);
  if (!jar.sessionid) {
    return NextResponse.json(
      { error: "cookies must include sessionid" },
      { status: 400 },
    );
  }

  // Verify against Instagram before storing, but never let the check cost us
  // the save: cookies expire, and a user pasting fresh ones must not lose them
  // because Instagram was slow or unreachable. A failed check is recorded as
  // last_error instead, and PATCH can re-run it.
  const check = await checkSession(jar).catch((error: unknown) => ({
    alive: false,
    detail:
      error instanceof Error
        ? `check failed: ${error.message.slice(0, 120)}`
        : "check failed",
  }));

  try {
    await query(
      `
      INSERT INTO cookies (username, sessionid, csrftoken, ds_user_id, ig_did, mid,
                           datr, rur, is_active, updated_at, last_error, user_agent)
      VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9, now(), $10, $11)
      ON CONFLICT (username) DO UPDATE SET
        sessionid = EXCLUDED.sessionid, csrftoken = EXCLUDED.csrftoken,
        ds_user_id = EXCLUDED.ds_user_id, ig_did = EXCLUDED.ig_did,
        mid = EXCLUDED.mid, datr = EXCLUDED.datr, rur = EXCLUDED.rur,
        is_active = EXCLUDED.is_active, updated_at = now(),
        last_error = EXCLUDED.last_error,
        -- Keep the stored UA when a caller sends none, rather than clearing a
        -- good value on re-save.
        user_agent = coalesce(EXCLUDED.user_agent, cookies.user_agent)
    `,
      [
        username,
        jar.sessionid,
        jar.csrftoken ?? null,
        jar.ds_user_id ?? null,
        jar.ig_did ?? null,
        jar.mid ?? null,
        jar.datr ?? null,
        jar.rur ?? null,
        check.alive,
        check.alive ? null : check.detail,
        userAgent,
      ],
    );
  } catch (error) {
    // Always answer with JSON. An unhandled throw here returns an empty body,
    // and the browser reports "Unexpected end of JSON input" instead of the
    // actual reason.
    console.error("[api/sessions] save failed:", error);
    return NextResponse.json(
      {
        error: "could not save the session",
        detail: error instanceof Error ? error.message.slice(0, 200) : "database error",
      },
      { status: 500 },
    );
  }

  // Иначе страница показала бы прежнее состояние из кэша.
  invalidate("accounts");

  return NextResponse.json({
    ok: true,
    username,
    alive: check.alive,
    detail: check.detail,
    cookiesFound: COOKIE_NAMES.filter((n) => jar[n]).length,
    missing: missingCookies(jar),
  });
}

/** PATCH: re-check a stored session against Instagram. */
export async function PATCH(request: Request) {
  if (!(await getSession())) {
    return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  }

  const body = await request.json().catch(() => ({}));
  const username = String(body.username ?? "").trim();
  if (!username) {
    return NextResponse.json({ error: "username is required" }, { status: 400 });
  }

  const rows = await query<Record<string, string | null>>(
    `SELECT sessionid, csrftoken, ds_user_id, ig_did, mid, datr, rur
     FROM cookies WHERE username = $1`,
    [username],
  );
  if (rows.length === 0) {
    return NextResponse.json({ error: "not found" }, { status: 404 });
  }

  const jar = Object.fromEntries(
    Object.entries(rows[0]).filter(([, v]) => v),
  ) as Record<string, string>;

  const check = await checkSession(jar);
  await query(
    `UPDATE cookies SET is_active = $2, last_error = $3 WHERE username = $1`,
    [username, check.alive, check.alive ? null : check.detail],
  );

  invalidate("accounts");
  return NextResponse.json({ ok: true, alive: check.alive, detail: check.detail });
}

/** DELETE: remove a session. */
export async function DELETE(request: Request) {
  if (!(await getSession())) {
    return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  }
  const body = await request.json().catch(() => ({}));
  const username = String(body.username ?? "").trim();
  if (!username) {
    return NextResponse.json({ error: "username is required" }, { status: 400 });
  }
  await query(`DELETE FROM cookies WHERE username = $1`, [username]);
  invalidate("accounts");
  return NextResponse.json({ ok: true });
}
