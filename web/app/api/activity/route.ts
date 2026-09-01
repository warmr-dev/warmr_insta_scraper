import { NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { getActivity, getActivityAccounts, getLogsPageData } from "@/lib/queries";

/**
 * Feed for the live logs page.
 *
 * Authenticated like every other route: the trail names monitored accounts and
 * the handles they follow, which is exactly the business data the dashboard
 * login exists to protect.
 */
export async function GET(request: Request) {
  if (!(await getSession())) {
    return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  }

  const url = new URL(request.url);
  const username = url.searchParams.get("username") || undefined;
  const limitRaw = Number(url.searchParams.get("limit") ?? 200);
  // Clamped: this endpoint is polled every 5s, and an unbounded limit would let
  // one tab pull the whole table on every tick.
  const limit = Math.min(Math.max(Number.isFinite(limitRaw) ? limitRaw : 200, 1), 500);

  try {
    // Unfiltered is the common case (the page polls it every 5s per open tab),
    // and it is served from the same one-connection query the page uses, so a
    // tab left open does not hold two pooled clients per tick. Supabase's
    // session-mode pooler caps the whole project at 15.
    if (!username) {
      const { events, accounts } = await getLogsPageData(limit);
      return NextResponse.json({ events, accounts });
    }

    // Filtered: two statements, but run in sequence rather than concurrently -
    // one pooled client at a time.
    const events = await getActivity(username, limit);
    const accounts = await getActivityAccounts();
    return NextResponse.json({ events, accounts });
  } catch (error) {
    console.error("[activity] query failed:", error);
    return NextResponse.json({ error: "query failed" }, { status: 500 });
  }
}
