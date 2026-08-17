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
