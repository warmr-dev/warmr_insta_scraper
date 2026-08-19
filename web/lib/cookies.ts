/**
 * Cookie parsing and session checks. Server-only.
 *
 * The seven cookies below are all required: without ig_did/mid/datr/rur the
 * Instagram feed endpoints answer 302 - measured, not assumed.
 */

export const COOKIE_NAMES = [
  "sessionid",
  "csrftoken",
  "ds_user_id",
  "ig_did",
  "mid",
  "datr",
  "rur",
] as const;

export type CookieJar = Partial<Record<string, string>>;

/**
 * Accepts every shape a browser hands out: the JSON array a cookie-export
 * extension produces, a plain object, and an `a=1; b=2` header string. Nobody
 * should have to reformat cookies by hand to get back online.
 */
export function parseCookies(raw: string): CookieJar {
  const text = raw.trim();
  if (!text) return {};

  if (text.startsWith("[") || text.startsWith("{")) {
    try {
      const parsed = JSON.parse(text);
      if (Array.isArray(parsed)) {
        const jar: CookieJar = {};
        for (const entry of parsed) {
          if (entry && typeof entry.name === "string") {
            jar[entry.name] = String(entry.value ?? "");
          }
        }
        return jar;
      }
      if (parsed && typeof parsed === "object") {
        const jar: CookieJar = {};
        for (const [k, v] of Object.entries(parsed)) {
          if (v != null) jar[k] = String(v);
        }
        return jar;
      }
    } catch {
      // Fall through to the header-string parser.
    }
  }

  const jar: CookieJar = {};
  for (const chunk of text.split(text.includes(";") ? ";" : "\n")) {
    const piece = chunk.trim();
    if (!piece) continue;
    const sep = piece.includes("=") ? "=" : "\t";
    const idx = piece.indexOf(sep);
    if (idx <= 0) continue;
    jar[piece.slice(0, idx).trim()] = piece.slice(idx + 1).trim();
  }
  return jar;
}

export function missingCookies(jar: CookieJar): string[] {
  return COOKIE_NAMES.filter((name) => !jar[name]);
}

export type SessionCheck = {
  alive: boolean;
  detail: string;
  trayEntries?: number;
};

/**
 * Ask Instagram whether the session still works.
 *
 * Uses `feed/reels_media/`, the batch endpoint the collector actually depends
 * on. Two endpoints that look like better checks are not:
 *
 * - `feed/reels_tray/` exists on the mobile API but NOT on the web one. The web
 *   host answers 200 with the 600KB SPA shell, so it reports every session as
 *   dead no matter how fresh the cookies are. Measured, not assumed.
 * - `accounts/current_user/` answers 400 to browser cookies even when the feeds
 *   work.
 *
 * An empty `reels` object is a valid, healthy answer - it means the probe id has
 * no active story, not that the session is broken.
 */
export async function checkSession(jar: CookieJar): Promise<SessionCheck> {
  const cookieHeader = Object.entries(jar)
    .map(([k, v]) => `${k}=${v}`)
    .join("; ");

  try {
    // Instagram's own account (id 25025320) is the probe: it always exists, so
    // a valid session gets a well-formed answer whether or not it has a story.
    const response = await fetch(
      "https://www.instagram.com/api/v1/feed/reels_media/?reel_ids=25025320",
      {
        headers: {
          "User-Agent":
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
          "X-IG-App-ID": "936619743392459",
          "X-CSRFToken": jar.csrftoken ?? "",
          "X-Requested-With": "XMLHttpRequest",
          Referer: "https://www.instagram.com/",
          Cookie: cookieHeader,
        },
        redirect: "manual",
        cache: "no-store",
        // Instagram can hang. Without a deadline the whole function times out
        // and returns an empty body, which the browser cannot parse as JSON.
        signal: AbortSignal.timeout(12_000),
      },
    );

    if (response.status === 200) {
      // A 200 does not guarantee JSON: when the session is not valid for the
      // feeds Instagram serves the HTML login page with a 200. Calling .json()
      // on that throws "Unexpected token '<'", which surfaced to the user as a
      // broken save rather than as "this session is dead".
      const text = await response.text();
      let body: { status?: string; reels?: Record<string, unknown> } | null = null;
      try {
        body = JSON.parse(text);
      } catch {
        return {
          alive: false,
          detail: "Instagram returned the login page — session not valid for feeds",
        };
      }

      // `{"reels":{},"status":"ok"}` is the healthy shape. Anything else means
      // Instagram answered but not as a signed-in user.
      if (body?.status !== "ok") {
        return {
          alive: false,
          detail: `Instagram answered without a session (status: ${body?.status ?? "unknown"})`,
        };
      }

      return {
        alive: true,
        detail: "Session accepted by the stories feed",
      };
    }

    // 302 and 400 both mean the session is no longer valid for feeds. Instagram
    // has no distinct "expired" status on the web API.
    if ([301, 302, 303, 307, 308].includes(response.status)) {
      return { alive: false, detail: "Redirected to login — session expired" };
    }
    if (response.status === 400) {
      return { alive: false, detail: "400 on feed — session no longer valid" };
    }
    if (response.status === 429) {
      return { alive: false, detail: "Rate limited (429) — try again later" };
    }
    return { alive: false, detail: `HTTP ${response.status}` };
  } catch (error) {
    return {
      alive: false,
      detail: error instanceof Error ? error.message.slice(0, 120) : "request failed",
    };
  }
}
