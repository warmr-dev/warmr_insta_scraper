import { NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { getActivity, getActivityAccounts } from "@/lib/queries";

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
    const [events, accounts] = await Promise.all([
      getActivity(username, limit),
      getActivityAccounts(),
    ]);
    return NextResponse.json({ events, accounts });
  } catch (error) {
    console.error("[activity] query failed:", error);
    return NextResponse.json({ error: "query failed" }, { status: 500 });
  }
}
