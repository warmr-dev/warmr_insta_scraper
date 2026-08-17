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
      // Supabase's pooler terminates idle connections; keep the pool small so a
      // serverless function does not hold more than it needs.
      max: 3,
      idleTimeoutMillis: 10_000,
      connectionTimeoutMillis: 15_000,
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
