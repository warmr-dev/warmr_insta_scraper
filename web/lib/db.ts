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

let pool: Pool | null = null;

function getPool(): Pool {
  if (!connectionString) {
    throw new Error("DATABASE_URL is not set");
  }
  if (!pool) {
    pool = new Pool({
      connectionString,
      // The database is in Sydney: ~2.5s per round-trip from here, and a new
      // connection costs a TLS handshake on top. Two things follow.
      //
      // The pool must fit the widest page: Overview issues 4 queries in
      // parallel, and a smaller pool would serialise the surplus, adding a
      // whole round-trip per queued query.
      max: 8,
      // Keep connections alive between page views. At 10s they expired between
      // navigations, so every tab switch paid for a fresh handshake.
      idleTimeoutMillis: 5 * 60_000,
      connectionTimeoutMillis: 15_000,
      keepAlive: true,
      ssl: { rejectUnauthorized: false },
    });
  }
  return pool;
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
