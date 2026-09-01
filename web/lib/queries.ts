/**
 * Analytics queries. Server-only.
 *
 * Every query is wrapped in a 30-second cache. The database is in Sydney, so a
 * round-trip costs over a second from here, and the scraper only writes once a
 * minute - without the cache, clicking between tabs re-fetched identical data
 * and paid the full latency each time.
 *
 * Deliberately raw SQL: these are read-only aggregates over the scraper's
 * schema, and an ORM would obscure what is being counted.
 *
 * Terminology matches the scraper so numbers can be reconciled:
 * - a story is one media item, deduplicated on story_id
 * - `skipped_video` means it never reached the AI, so it cost nothing
 * - a lead is final_score >= 7
 */

import { cached } from "./cache";
import { query, queryOne } from "./db";

export type Overview = {
  stories_total: number;
  photos_total: number;
  videos_skipped: number;
  analysed: number;
  leads: number;
  targets: number;
  accounts_live: number;
  accounts_dead: number;
  spend_usd: number;
  ai_calls: number;
};

export async function getOverview(): Promise<Overview> {
  return cached("overview", async () => {
    const row = await queryOne<Record<string, string>>(`
      SELECT
        (SELECT count(*) FROM stories) AS stories_total,
        (SELECT count(*) FROM stories WHERE media_type = 1) AS photos_total,
        (SELECT count(*) FROM stories WHERE pipeline_state = 'skipped_video') AS videos_skipped,
        (SELECT count(*) FROM story_analysis) AS analysed,
        (SELECT count(*) FROM story_analysis WHERE final_score >= 7) AS leads,
        (SELECT count(*) FROM targets) AS targets,
        (SELECT count(*) FROM cookies WHERE is_active) AS accounts_live,
        (SELECT count(*) FROM cookies WHERE NOT is_active) AS accounts_dead,
        (SELECT coalesce(sum(value), 0) FROM metric_samples
           WHERE metric = 'ai_estimated_spend_usd') AS spend_usd,
        (SELECT coalesce(sum(value), 0) FROM metric_samples
           WHERE metric = 'ai_calls') AS ai_calls
    `);

    const num = (key: string) => Number(row?.[key] ?? 0);
    return {
      stories_total: num("stories_total"),
      photos_total: num("photos_total"),
      videos_skipped: num("videos_skipped"),
      analysed: num("analysed"),
      leads: num("leads"),
      targets: num("targets"),
      accounts_live: num("accounts_live"),
      accounts_dead: num("accounts_dead"),
      spend_usd: num("spend_usd"),
      ai_calls: num("ai_calls"),
    };
  });
}

export type Lead = {
  story_id: string;
  username: string;
  final_score: number;
  service_category: string | null;
  intent_type: string | null;
  ai_explanation: string | null;
  taken_at: string;
  analyzed_at: string | null;
  instagram_url: string | null;
};

export async function getLeads(limit = 50): Promise<Lead[]> {
  return cached(`leads:${limit}`, () =>
    query<Lead>(
      `
      SELECT a.story_id, t.username, a.final_score, a.service_category,
             a.intent_type, a.ai_explanation, s.taken_at, a.analyzed_at,
             t.instagram_url
      FROM story_analysis a
      JOIN stories s ON s.story_id = a.story_id
      JOIN targets t ON t.user_id = s.target_user_id
      WHERE a.final_score >= 7
      ORDER BY a.analyzed_at DESC NULLS LAST
      LIMIT $1
    `,
      [limit],
    ),
  );
}

export type Account = {
  username: string;
  is_active: boolean;
  updated_at: string;
  last_error: string | null;
  cookie_count: number;
};

export async function getAccounts(): Promise<Account[]> {
  // Shorter TTL: this page has Re-check and Add buttons, so a stale row here is
  // more confusing than a stale count elsewhere.
  return cached(
    "accounts",
    () =>
      query<Account>(`
        SELECT username, is_active, updated_at, last_error,
               -- How many of the seven required cookies are present. Fewer than
               -- seven usually means the feed endpoints will answer 302.
               (CASE WHEN sessionid  IS NOT NULL AND sessionid  <> '' THEN 1 ELSE 0 END
              + CASE WHEN csrftoken  IS NOT NULL AND csrftoken  <> '' THEN 1 ELSE 0 END
              + CASE WHEN ds_user_id IS NOT NULL AND ds_user_id <> '' THEN 1 ELSE 0 END
              + CASE WHEN ig_did     IS NOT NULL AND ig_did     <> '' THEN 1 ELSE 0 END
              + CASE WHEN mid        IS NOT NULL AND mid        <> '' THEN 1 ELSE 0 END
              + CASE WHEN datr       IS NOT NULL AND datr       <> '' THEN 1 ELSE 0 END
              + CASE WHEN rur        IS NOT NULL AND rur        <> '' THEN 1 ELSE 0 END
               ) AS cookie_count
        FROM cookies
        ORDER BY is_active DESC, username
      `),
    5_000,
  );
}

export type TargetActivity = {
  username: string;
  instagram_url: string | null;
  stories: number;
  photos: number;
  analysed: number;
  leads: number;
  best_score: number;
  last_story_at: string | null;
  status: string;
};

/**
 * Per-target activity, which drives the "who posts regularly" view and mirrors
 * the scraper's prioritisation: a target with several analysed photos and no
 * signal gets skipped until a periodic recheck.
 */
export async function getTargetActivity(limit = 100): Promise<TargetActivity[]> {
  return cached(`targets:${limit}`, () =>
    query<TargetActivity>(
      `
      SELECT t.username, t.instagram_url,
             count(s.story_id) AS stories,
             count(*) FILTER (WHERE s.media_type = 1) AS photos,
             count(a.story_id) AS analysed,
             count(*) FILTER (WHERE a.final_score >= 7) AS leads,
             coalesce(max(a.final_score), 0) AS best_score,
             max(s.taken_at) AS last_story_at,
             CASE
               WHEN count(*) FILTER (WHERE a.final_score >= 7) > 0 THEN 'proven'
               WHEN coalesce(max(a.final_score), 0) >= 4 THEN 'promising'
               WHEN count(a.story_id) >= 8 THEN 'exhausted'
               ELSE 'unproven'
             END AS status
      FROM targets t
      LEFT JOIN stories s ON s.target_user_id = t.user_id
      LEFT JOIN story_analysis a ON a.story_id = s.story_id
      GROUP BY t.username, t.instagram_url
      HAVING count(s.story_id) > 0
      ORDER BY count(s.story_id) DESC
      LIMIT $1
    `,
      [limit],
    ),
  );
}

export type ScoreBucket = { final_score: number; count: number };

export async function getScoreDistribution(): Promise<ScoreBucket[]> {
  return cached("scores", () =>
    query<ScoreBucket>(`
      SELECT final_score, count(*) AS count
      FROM story_analysis
      WHERE final_score IS NOT NULL
      GROUP BY final_score
      ORDER BY final_score DESC
    `),
  );
}

export type CategoryCount = { service_category: string; count: number };

export async function getCategories(limit = 12): Promise<CategoryCount[]> {
  return cached(`categories:${limit}`, () =>
    query<CategoryCount>(
      `
      SELECT coalesce(service_category, '(none)') AS service_category,
             count(*) AS count
      FROM story_analysis
      GROUP BY 1
      ORDER BY 2 DESC
      LIMIT $1
    `,
      [limit],
    ),
  );
}

export type DailyPoint = {
  day: string;
  stories: number;
  photos: number;
  analysed: number;
  leads: number;
};

export async function getDailyActivity(days = 14): Promise<DailyPoint[]> {
  return cached(`daily:${days}`, () =>
    query<DailyPoint>(
      `
      SELECT to_char(date_trunc('day', s.discovered_at), 'YYYY-MM-DD') AS day,
             count(*) AS stories,
             count(*) FILTER (WHERE s.media_type = 1) AS photos,
             count(a.story_id) AS analysed,
             count(*) FILTER (WHERE a.final_score >= 7) AS leads
      FROM stories s
      LEFT JOIN story_analysis a ON a.story_id = s.story_id
      WHERE s.discovered_at > now() - ($1 || ' days')::interval
      GROUP BY 1
      ORDER BY 1
    `,
      [days],
    ),
  );
}

export type PipelineState = { pipeline_state: string; count: number };

export async function getPipelineStates(): Promise<PipelineState[]> {
  return cached("states", () =>
    query<PipelineState>(`
      SELECT pipeline_state, count(*) AS count
      FROM stories
      GROUP BY 1
      ORDER BY 2 DESC
    `),
  );
}

export type ActivityEvent = {
  id: string;
  username: string;
  phase: string;
  status: string;
  message: string | null;
  targets: string[] | null;
  item_count: number | null;
  duration_ms: number | null;
  occurred_at: string;
};

/**
 * The live activity trail (migration 0004).
 *
 * Cached for 5s rather than the usual 30: this page exists to answer "what is
 * happening right now", and a half-minute-stale feed would defeat it.
 */
export async function getActivity(
  username?: string,
  limit = 200,
): Promise<ActivityEvent[]> {
  return cached(
    `activity:${username ?? "all"}:${limit}`,
    () =>
      query<ActivityEvent>(
        `
        SELECT id::text, username, phase, status, message, targets,
               item_count, duration_ms,
               to_char(occurred_at AT TIME ZONE 'UTC',
                       'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS occurred_at
        FROM activity_log
        ${username ? "WHERE username = $2" : ""}
        ORDER BY occurred_at DESC, id DESC
        LIMIT $1
      `,
        username ? [limit, username] : [limit],
      ),
    5_000,
  );
}

export type ActivityAccount = {
  username: string;
  events: number;
  last_seen: string | null;
  last_message: string | null;
};

/** One row per session that has done anything recently - drives the filter. */
export async function getActivityAccounts(): Promise<ActivityAccount[]> {
  return cached(
    "activity:accounts",
    () =>
      query<ActivityAccount>(`
        SELECT username,
               count(*) AS events,
               to_char(max(occurred_at) AT TIME ZONE 'UTC',
                       'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS last_seen,
               (SELECT message FROM activity_log b
                 WHERE b.username = a.username
                 ORDER BY occurred_at DESC, id DESC LIMIT 1) AS last_message
        FROM activity_log a
        WHERE occurred_at > now() - interval '24 hours'
        GROUP BY username
        ORDER BY max(occurred_at) DESC
      `),
    5_000,
  );
}

export type SkippedTarget = {
  handle: string;
  times_skipped: number;
  photos_skipped: number;
  last_reason: string | null;
  last_skipped: string | null;
  analysed: number;
  best_score: number;
  avg_score: number;
  leads: number;
};

/**
 * Accounts the pipeline stopped paying for, and the evidence behind each call.
 *
 * Two sources, deliberately: `activity_log` says how often we skipped a handle
 * recently, while `stories`/`story_analysis` carry the scoring history the
 * decision was actually made on. The log alone would show the verdict without
 * the reasoning; the analysis tables alone would not show that a skip is
 * currently in force.
 *
 * Prefer `getLogsPageData` when rendering the page: it fetches this and the
 * other three panels over ONE pooled connection. See that function for why.
 */
export async function getSkippedTargets(limit = 100): Promise<SkippedTarget[]> {
  return cached(
    `skipped:${limit}`,
    () =>
      query<SkippedTarget>(
        `
        WITH skips AS (
          SELECT jsonb_array_elements_text(targets) AS handle,
                 count(*) AS times_skipped,
                 sum(coalesce(item_count, 0)) AS photos_skipped,
                 max(occurred_at) AS last_skipped
          FROM activity_log
          WHERE phase = 'skipped' AND status = 'irrelevant' AND targets IS NOT NULL
          GROUP BY 1
        )
        SELECT s.handle,
               s.times_skipped,
               s.photos_skipped,
               (SELECT message FROM activity_log a
                 WHERE a.phase = 'skipped' AND a.targets ? s.handle
                 ORDER BY a.occurred_at DESC LIMIT 1) AS last_reason,
               to_char(s.last_skipped AT TIME ZONE 'UTC',
                       'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS last_skipped,
               coalesce(h.analysed, 0) AS analysed,
               coalesce(h.best_score, 0) AS best_score,
               coalesce(h.avg_score, 0) AS avg_score,
               coalesce(h.leads, 0) AS leads
        FROM skips s
        LEFT JOIN (
          SELECT t.username,
                 count(a.story_id) AS analysed,
                 coalesce(max(a.final_score), 0) AS best_score,
                 round(coalesce(avg(a.final_score), 0)::numeric, 1) AS avg_score,
                 count(*) FILTER (WHERE a.final_score >= 7) AS leads
          FROM targets t
          JOIN stories st ON st.target_user_id = t.user_id
          JOIN story_analysis a ON a.story_id = st.story_id
          GROUP BY t.username
        ) h ON h.username = s.handle
        ORDER BY s.photos_skipped DESC, s.times_skipped DESC
        LIMIT $1
      `,
        [limit],
      ),
    5_000,
  );
}

export type SkipSummary = {
  status: string;
  events: number;
  items: number;
};

/** How much each skip reason saved, this being the point of skipping. */
export async function getSkipSummary(): Promise<SkipSummary[]> {
  return cached(
    "skip:summary",
    () =>
      query<SkipSummary>(`
        SELECT status,
               count(*) AS events,
               sum(coalesce(item_count, 0)) AS items
        FROM activity_log
        WHERE phase = 'skipped'
          AND occurred_at > now() - interval '24 hours'
        GROUP BY status
        ORDER BY sum(coalesce(item_count, 0)) DESC
      `),
    5_000,
  );
}


export type LogsPageData = {
  events: ActivityEvent[];
  accounts: ActivityAccount[];
  skipSummary: SkipSummary[];
  skipped: SkippedTarget[];
};

/**
 * Everything the Logs page needs, over a single pooled connection.
 *
 * The page originally ran its four queries through `Promise.all`, which asks
 * the pool for four clients at once. That is fine against a normal Postgres,
 * but this deployment talks to Supabase's SESSION-mode pooler on port 5432,
 * which holds one server connection per client for the client's whole life and
 * caps the project at 15. Each serverless instance keeps its own pool, so a
 * handful of warm instances rendering this page exhausted the cap and the
 * server threw `EMAXCONNSESSION: max clients reached`. In the browser that
 * surfaced only as a minified React error, because the failure happened while
 * streaming the RSC payload - the page had already returned 200.
 *
 * One connection, four statements, one round-trip to Sydney. Sequential
 * `await`s would also have fixed the exhaustion but would pay the latency four
 * times over; this pays it once.
 *
 * Each result is aggregated to a single JSON column so the shapes come back
 * intact rather than as a cartesian join.
 */
export async function getLogsPageData(limit = 200): Promise<LogsPageData> {
  return cached(
    `logs:page:${limit}`,
    async () => {
      const row = await queryOne<{
        events: ActivityEvent[] | null;
        accounts: ActivityAccount[] | null;
        skip_summary: SkipSummary[] | null;
        skipped: SkippedTarget[] | null;
      }>(
        `
        WITH recent_events AS (
          SELECT id::text, username, phase, status, message, targets,
                 item_count, duration_ms,
                 to_char(occurred_at AT TIME ZONE 'UTC',
                         'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS occurred_at
          FROM activity_log
          ORDER BY occurred_at DESC, id DESC
          LIMIT $1
        ),
        active_accounts AS (
          SELECT username,
                 count(*) AS events,
                 to_char(max(occurred_at) AT TIME ZONE 'UTC',
                         'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS last_seen,
                 (SELECT message FROM activity_log b
                   WHERE b.username = a.username
                   ORDER BY occurred_at DESC, id DESC LIMIT 1) AS last_message
          FROM activity_log a
          WHERE occurred_at > now() - interval '24 hours'
          GROUP BY username
          ORDER BY max(occurred_at) DESC
        ),
        skip_summary AS (
          SELECT status,
                 count(*) AS events,
                 sum(coalesce(item_count, 0)) AS items
          FROM activity_log
          WHERE phase = 'skipped'
            AND occurred_at > now() - interval '24 hours'
          GROUP BY status
          ORDER BY sum(coalesce(item_count, 0)) DESC
        ),
        skips AS (
          SELECT jsonb_array_elements_text(targets) AS handle,
                 count(*) AS times_skipped,
                 sum(coalesce(item_count, 0)) AS photos_skipped,
                 max(occurred_at) AS last_skipped
          FROM activity_log
          WHERE phase = 'skipped' AND status = 'irrelevant' AND targets IS NOT NULL
          GROUP BY 1
        ),
        history AS (
          SELECT t.username,
                 count(a.story_id) AS analysed,
                 coalesce(max(a.final_score), 0) AS best_score,
                 round(coalesce(avg(a.final_score), 0)::numeric, 1) AS avg_score,
                 count(*) FILTER (WHERE a.final_score >= 7) AS leads
          FROM targets t
          JOIN stories st ON st.target_user_id = t.user_id
          JOIN story_analysis a ON a.story_id = st.story_id
          GROUP BY t.username
        ),
        skipped_targets AS (
          SELECT s.handle,
                 s.times_skipped,
                 s.photos_skipped,
                 (SELECT message FROM activity_log al
                   WHERE al.phase = 'skipped' AND al.targets ? s.handle
                   ORDER BY al.occurred_at DESC LIMIT 1) AS last_reason,
                 to_char(s.last_skipped AT TIME ZONE 'UTC',
                         'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS last_skipped,
                 coalesce(h.analysed, 0) AS analysed,
                 coalesce(h.best_score, 0) AS best_score,
                 coalesce(h.avg_score, 0) AS avg_score,
                 coalesce(h.leads, 0) AS leads
          FROM skips s
          LEFT JOIN history h ON h.username = s.handle
          ORDER BY s.photos_skipped DESC, s.times_skipped DESC
          LIMIT 100
        )
        SELECT
          (SELECT coalesce(jsonb_agg(to_jsonb(e)), '[]'::jsonb) FROM recent_events e) AS events,
          (SELECT coalesce(jsonb_agg(to_jsonb(a)), '[]'::jsonb) FROM active_accounts a) AS accounts,
          (SELECT coalesce(jsonb_agg(to_jsonb(s)), '[]'::jsonb) FROM skip_summary s) AS skip_summary,
          (SELECT coalesce(jsonb_agg(to_jsonb(k)), '[]'::jsonb) FROM skipped_targets k) AS skipped
      `,
        [limit],
      );

      return {
        events: row?.events ?? [],
        accounts: row?.accounts ?? [],
        skipSummary: row?.skip_summary ?? [],
        skipped: row?.skipped ?? [],
      };
    },
    5_000,
  );
}

export type TargetStory = {
  story_id: string;
  media_type: number;
  pipeline_state: string;
  taken_at: string;
  expiring_at: string | null;
  discovered_at: string;
  is_live: boolean;
  final_score: number | null;
  cheap_score: number | null;
  smart_score: number | null;
  service_category: string | null;
  intent_type: string | null;
  ai_explanation: string | null;
  ocr_text: string | null;
  analyzed_at: string | null;
};

export type TargetDetail = {
  username: string;
  user_id: string;
  instagram_url: string | null;
  stories: number;
  photos: number;
  videos: number;
  analysed: number;
  leads: number;
  best_score: number;
  avg_score: number;
  live_stories: number;
  last_story_at: string | null;
  status: string;
};

/**
 * One target's header counts and its full story list, over ONE connection.
 *
 * Combined for the same reason as `getLogsPageData`: Supabase's session-mode
 * pooler caps the whole project at 15 clients, so a page that asks for two at
 * once is two instances away from exhausting it.
 *
 * `is_live` is the column that makes this page useful. Instagram stories vanish
 * after 24 hours and we deliberately keep no copy of the media (spec 7.4/11 -
 * it is deleted after analysis), so an expired story cannot be opened by anyone
 * and a link to it would be a dead end. Computed in SQL against now() rather
 * than in the browser, whose clock may be wrong.
 */
export async function getTargetDetail(
  username: string,
): Promise<{ target: TargetDetail | null; stories: TargetStory[] }> {
  return cached(`target:${username}`, async () => {
    const row = await queryOne<{
      target: TargetDetail | null;
      stories: TargetStory[] | null;
    }>(
      `
      WITH tgt AS (
        SELECT user_id, username, instagram_url FROM targets WHERE username = $1
      ),
      story_rows AS (
        SELECT s.story_id,
               s.media_type,
               s.pipeline_state,
               to_char(s.taken_at AT TIME ZONE 'UTC',
                       'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS taken_at,
               to_char(s.expiring_at AT TIME ZONE 'UTC',
                       'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS expiring_at,
               to_char(s.discovered_at AT TIME ZONE 'UTC',
                       'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS discovered_at,
               -- Openable on Instagram right now? Stories last 24h and we keep
               -- no media copy, so anything older is gone for good.
               (coalesce(s.expiring_at, s.taken_at + interval '24 hours') > now())
                 AS is_live,
               a.final_score, a.cheap_score, a.smart_score,
               a.service_category, a.intent_type, a.ai_explanation, a.ocr_text,
               to_char(a.analyzed_at AT TIME ZONE 'UTC',
                       'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS analyzed_at
        FROM stories s
        JOIN tgt ON tgt.user_id = s.target_user_id
        LEFT JOIN story_analysis a ON a.story_id = s.story_id
        ORDER BY s.taken_at DESC
        LIMIT 500
      ),
      header AS (
        SELECT tgt.username,
               tgt.user_id::text AS user_id,
               tgt.instagram_url,
               count(s.story_id) AS stories,
               count(*) FILTER (WHERE s.media_type = 1) AS photos,
               count(*) FILTER (WHERE s.media_type <> 1) AS videos,
               count(a.story_id) AS analysed,
               count(*) FILTER (WHERE a.final_score >= 7) AS leads,
               coalesce(max(a.final_score), 0) AS best_score,
               round(coalesce(avg(a.final_score), 0)::numeric, 1) AS avg_score,
               count(*) FILTER (
                 WHERE coalesce(s.expiring_at, s.taken_at + interval '24 hours') > now()
               ) AS live_stories,
               to_char(max(s.taken_at) AT TIME ZONE 'UTC',
                       'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS last_story_at,
               CASE
                 WHEN count(*) FILTER (WHERE a.final_score >= 7) > 0 THEN 'proven'
                 WHEN coalesce(max(a.final_score), 0) >= 4 THEN 'promising'
                 WHEN count(a.story_id) >= 8 THEN 'exhausted'
                 ELSE 'unproven'
               END AS status
        FROM tgt
        LEFT JOIN stories s ON s.target_user_id = tgt.user_id
        LEFT JOIN story_analysis a ON a.story_id = s.story_id
        GROUP BY tgt.username, tgt.user_id, tgt.instagram_url
      )
      SELECT (SELECT to_jsonb(h) FROM header h) AS target,
             (SELECT coalesce(jsonb_agg(to_jsonb(r)), '[]'::jsonb)
                FROM story_rows r) AS stories
    `,
      [username],
    );

    return { target: row?.target ?? null, stories: row?.stories ?? [] };
  });
}
