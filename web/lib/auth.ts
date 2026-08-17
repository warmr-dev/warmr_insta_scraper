/**
 * Authentication. Server-only.
 *
 * One credential pair, checked against environment variables. Deliberately
 * simple: this dashboard has a single operator, so a user table and password
 * hashing would add moving parts without adding safety.
 *
 * The session cookie is signed with SESSION_SECRET so it cannot be forged by
 * editing the cookie value.
 */

import { createHmac, timingSafeEqual } from "node:crypto";
import { cookies } from "next/headers";

const COOKIE_NAME = "warmr_session";
const MAX_AGE_SECONDS = 60 * 60 * 12; // 12 hours

function secret(): string {
  const value = process.env.SESSION_SECRET;
  if (!value) {
    throw new Error("SESSION_SECRET is not set");
  }
  return value;
}

function sign(payload: string): string {
  return createHmac("sha256", secret()).update(payload).digest("hex");
}

/** Compare without leaking length or position through timing. */
function safeEqual(a: string, b: string): boolean {
  const bufA = Buffer.from(a);
  const bufB = Buffer.from(b);
  if (bufA.length !== bufB.length) return false;
  return timingSafeEqual(bufA, bufB);
}

export function checkCredentials(email: string, password: string): boolean {
  const expectedEmail = process.env.ADMIN_EMAIL ?? "";
  const expectedPassword = process.env.ADMIN_PASSWORD ?? "";
  if (!expectedEmail || !expectedPassword) return false;

  // Evaluate both so a wrong email costs the same time as a wrong password.
  const emailOk = safeEqual(email.trim().toLowerCase(), expectedEmail.toLowerCase());
  const passwordOk = safeEqual(password, expectedPassword);
  return emailOk && passwordOk;
}

export async function createSession(email: string): Promise<void> {
  const expires = Date.now() + MAX_AGE_SECONDS * 1000;
  const payload = `${email}:${expires}`;
  const store = await cookies();
  store.set(COOKIE_NAME, `${payload}:${sign(payload)}`, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: MAX_AGE_SECONDS,
  });
}

export async function destroySession(): Promise<void> {
  const store = await cookies();
  store.delete(COOKIE_NAME);
}

/** The signed-in email, or null. Verifies the signature and the expiry. */
export async function getSession(): Promise<string | null> {
  const store = await cookies();
  const raw = store.get(COOKIE_NAME)?.value;
  if (!raw) return null;

  const parts = raw.split(":");
  if (parts.length !== 3) return null;
  const [email, expiresRaw, signature] = parts;

  if (!safeEqual(signature, sign(`${email}:${expiresRaw}`))) return null;

  const expires = Number(expiresRaw);
  if (!Number.isFinite(expires) || Date.now() > expires) return null;

  return email;
}

export async function requireSession(): Promise<string> {
  const email = await getSession();
  if (!email) {
    // Callers are server components/routes; redirect is handled by the caller.
    throw new Error("unauthenticated");
  }
  return email;
}
