/**
 * In-process cache for dashboard queries.
 *
 * The database is in Sydney: ~1.3s per page from here, paid again on every tab
 * click. But the scraper only writes once a minute, so almost every one of
 * those round-trips fetched data identical to what the previous page already
 * had.
 *
 * A short TTL is therefore nearly free in staleness and removes the wait
 * entirely on repeat views. 30 seconds is half the scraper's cycle, so numbers
 * are never more than one cycle behind.
 *
 * Deliberately a plain Map rather than Next's `unstable_cache`: this data is
 * per-deployment and tiny, and a Map is trivially inspectable when a number
 * looks wrong.
 */

type Entry<T> = { value: T; expires: number };

// globalThis, а не модульная переменная: Next.js изолирует модули между
// route handlers, поэтому обычный Map жил бы только внутри одного маршрута -
// именно поэтому кэшировался только Overview, а Leads/Targets/Analytics нет.
const globalStore = globalThis as unknown as {
  __warmrCache?: Map<string, Entry<unknown>>;
  __warmrInFlight?: Map<string, Promise<unknown>>;
};

const store =
  globalStore.__warmrCache ?? (globalStore.__warmrCache = new Map());

const DEFAULT_TTL_MS = 30_000;

/**
 * Run `loader` at most once per TTL per key.
 *
 * Concurrent callers share one in-flight promise, so four parallel page loads
 * cause one query rather than four - which matters because the connection pool
 * is deliberately small.
 */
const inFlight =
  globalStore.__warmrInFlight ?? (globalStore.__warmrInFlight = new Map());

export async function cached<T>(
  key: string,
  loader: () => Promise<T>,
  ttlMs: number = DEFAULT_TTL_MS,
): Promise<T> {
  const now = Date.now();
  const hit = store.get(key);
  if (hit && hit.expires > now) {
    return hit.value as T;
  }

  const pending = inFlight.get(key);
  if (pending) {
    return pending as Promise<T>;
  }

  const promise = loader()
    .then((value) => {
      store.set(key, { value, expires: Date.now() + ttlMs });
      return value;
    })
    .finally(() => {
      inFlight.delete(key);
    });

  inFlight.set(key, promise);
  return promise;
}

/** Drop cached entries so the next read is fresh. Used after a write. */
export function invalidate(prefix?: string): void {
  if (!prefix) {
    store.clear();
    return;
  }
  for (const key of store.keys()) {
    if (key.startsWith(prefix)) store.delete(key);
  }
}
