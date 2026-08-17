/**
 * Database access. Server-only.
 *
 * Every query in this app runs here, on the server. The browser never holds a
 * database credential, because the `cookies` table contains live Instagram
 * sessions - a leaked key would hand over working sessions, not just analytics.
 *
 * The scraper's database has RLS enabled on every table with zero policies
 * (migration 0003), so `anon` reads nothing at all. We connect as `postgres`,
 * which has BYPASSRLS. That is why this file must never be imported from a
 * client component.
 */

import { Pool } from "pg";

if (typeof window !== "undefined") {
  throw new Error("lib/db.ts is server-only and must not reach the browser");
}

const connectionString = process.env.DATABASE_URL?.replace(
  // The scraper stores a SQLAlchemy URL; node-postgres wants a plain one.
  /^postgresql\+psycopg:\/\//,
  "postgresql://",
);

// Held on globalThis rather than in a module variable. Next.js isolates module
// state between routes, so a per-module pool meant each route opened its own
// connections and paid a fresh TLS handshake to Sydney - about a second per
// page, even when the query result was already cached.
const globalForPool = globalThis as unknown as { __warmrPool?: Pool };

function getPool(): Pool {
  if (!connectionString) {
    throw new Error("DATABASE_URL is not set");
  }

  if (!globalForPool.__warmrPool) {
    globalForPool.__warmrPool = new Pool({
      connectionString,
      // Supabase's pooler caps concurrent connections, and exceeding it gives
      // ECONNRESET rather than a queue - observed at 20 active connections.
      // Keep this small and let queries wait for a free client instead: the
      // wait is cheaper than a refused connection.
      max: 4,
      // Keep connections alive between page views. At 10s they expired between
      // navigations, so every tab switch paid for a fresh handshake.
      idleTimeoutMillis: 5 * 60_000,
      connectionTimeoutMillis: 15_000,
      keepAlive: true,
    });
  }

  return globalForPool.__warmrPool;
}

export async function query<T = Record<string, unknown>>(
  sql: string,
  params: unknown[] = [],
): Promise<T[]> {
  const result = await getPool().query(sql, params);
  return result.rows as T[];
}

/** A single row, or null. */
export async function queryOne<T = Record<string, unknown>>(
  sql: string,
  params: unknown[] = [],
): Promise<T | null> {
  const rows = await query<T>(sql, params);
  return rows[0] ?? null;
}
