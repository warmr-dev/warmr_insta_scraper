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
    const pool = new Pool({
      connectionString,
      // Supabase's pooler caps concurrent connections, and exceeding it gives
      // ECONNRESET rather than a queue - observed at 20 active connections.
      // Keep this small and let queries wait for a free client instead: the
      // wait is cheaper than a refused connection.
      // The URL is Supabase's SESSION-mode pooler (port 5432), which holds one
      // server connection per client for the client's whole life and caps the
      // PROJECT at 15 - not 15 per instance. Every serverless instance keeps
      // its own pool, so this ceiling is shared with every other warm instance
      // and with the scraper. At max: 4 a mere four warm instances exhausted
      // it, and the server threw `EMAXCONNSESSION: max clients reached`, which
      // reached the browser only as a minified React error.
      //
      // 2 is deliberately mean. Page queries are short and the cache absorbs
      // repeats, so waiting for a free client costs far less than a refused
      // connection - and it leaves headroom for the scraper, which needs the
      // same pool to keep collecting.
      max: 2,
      // Serverless containers are frozen between requests, so a connection we
      // think is idle may already have been dropped by the pooler. Expiring our
      // own connections sooner than Supabase does means we hand out a fresh one
      // instead of a dead one. Longer than this and a reload after a pause got
      // a closed socket.
      //
      // Short here also returns connections to the shared 15 sooner: an idle
      // client in session mode still occupies one of them.
      idleTimeoutMillis: 10_000,
      connectionTimeoutMillis: 15_000,
      keepAlive: true,
    });

    // Without this, `pg` re-throws errors from *idle* clients as an uncaught
    // exception and kills the whole function - which is why the dashboard died
    // on reload rather than just failing one query. The pool discards the bad
    // client on its own; we only have to not crash.
    pool.on("error", (err) => {
      console.error("[db] idle client error, connection discarded:", err.message);
    });

    globalForPool.__warmrPool = pool;
  }

  return globalForPool.__warmrPool;
}

/** A dropped connection looks like this rather than a query error. */
function isDeadConnection(error: unknown): boolean {
  const code = (error as { code?: string } | null)?.code;
  const message = (error as { message?: string } | null)?.message ?? "";
  return (
    // The pooler is at capacity. Transient rather than fatal - a client frees
    // up in milliseconds - so it is worth the same single retry as a dropped
    // connection. Without this the page 500s while the fix is simply to wait.
    /EMAXCONNSESSION|max clients reached/i.test(message) ||
    code === "ECONNRESET" ||
    code === "EPIPE" ||
    code === "ETIMEDOUT" ||
    code === "57P01" || // admin_shutdown - the pooler closed it
    code === "08006" || // connection_failure
    code === "08003" || // connection_does_not_exist
    /Connection terminated|socket hang up|server closed the connection/i.test(message)
  );
}

export async function query<T = Record<string, unknown>>(
  sql: string,
  params: unknown[] = [],
): Promise<T[]> {
  try {
    const result = await getPool().query(sql, params);
    return result.rows as T[];
  } catch (error) {
    if (!isDeadConnection(error)) throw error;

    // The pool handed us a connection the pooler had already closed while this
    // container was frozen. The failure discards it, so a single retry gets a
    // live one. Retrying once is safe here because every query in this app is
    // a read.
    //
    // A capacity error is different: nothing is broken, the pooler is simply
    // full, so an immediate retry would hit the same wall. Pause briefly and
    // let an in-flight query finish first.
    const message = (error as { message?: string } | null)?.message ?? "";
    if (/EMAXCONNSESSION|max clients reached/i.test(message)) {
      console.warn("[db] pooler at capacity, retrying after a short wait");
      await new Promise((resolve) => setTimeout(resolve, 250));
    } else {
      console.warn("[db] stale connection, retrying once");
    }

    const result = await getPool().query(sql, params);
    return result.rows as T[];
  }
}

/** A single row, or null. */
export async function queryOne<T = Record<string, unknown>>(
  sql: string,
  params: unknown[] = [],
): Promise<T | null> {
  const rows = await query<T>(sql, params);
  return rows[0] ?? null;
}
