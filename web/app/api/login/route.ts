import { NextResponse } from "next/server";
import { checkCredentials, createSession } from "@/lib/auth";

export async function POST(request: Request) {
  const { email, password } = await request.json().catch(() => ({}));

  if (typeof email !== "string" || typeof password !== "string") {
    return NextResponse.json({ error: "invalid request" }, { status: 400 });
  }

  if (!checkCredentials(email, password)) {
    // Same response whichever field is wrong - do not help enumeration.
    return NextResponse.json({ error: "invalid credentials" }, { status: 401 });
  }

  await createSession(email.trim().toLowerCase());
  return NextResponse.json({ ok: true });
}
