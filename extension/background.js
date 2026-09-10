/**
 * The orchestrator: claims targets, opens each profile, follows, reports back,
 * and keeps this profile's Instagram cookies fresh in Supabase.
 *
 * One Chrome profile is one Instagram account. The extension never needs to
 * know a password and never replays cookies anywhere - it acts inside the
 * session that is already logged in, which is exactly why the follow works
 * here when an out-of-browser request does not.
 *
 * Pacing is deliberately human. Instagram polices writes far harder than reads,
 * and an even drip of one follow every N seconds is not a slower human, it is
 * an obvious robot. So follows come in small bursts with long rests between
 * them, the account sleeps at night, and every interval is jittered.
 */

const DEFAULTS = {
  // Pre-filled for this project so a fresh profile works without typing.
  // Overridable in the popup if the project ever moves.
  supabaseUrl: "https://fdfwockxvtjtbermusfq.supabase.co",
  // The project's anon key. Safe to ship here: RLS keeps every table closed to
  // it (verified - `SELECT * FROM cookies` is permission denied), and the only
  // things it can do are the four ext_* functions. A leaked key costs a
  // scrambled follow queue, not the sessions or the leads.
  anonKey:
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." +
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZkZndvY2t4dnRqdGJlcm11c2ZxIiwicm9sZSI6" +
    "ImFub24iLCJpYXQiOjE3ODY0NTcyODgsImV4cCI6MjEwMjAzMzI4OH0." +
    "QHBui55xfioxuxI-x0eu1-HEvdazmHNE3yeNWidGXG0",
  session: "",
  enabled: false,
  // How many follows per day. 0 means unlimited - the rhythm still paces it.
  dailyLimit: 120,
  // A daily cap alone still permits 120 follows in two hours, which is the
  // shape that got throttled. An hourly ceiling is what actually spreads them.
  hourlyLimit: 25,
  // Minutes between cookie refreshes. The popup constrains this to 2-12 hours.
  cookieHours: 6,
  // Seconds between follows inside one burst.
  gapMinSec: 45,
  gapMaxSec: 150,
  // Follows per burst, then a long rest.
  burstMin: 2,
  burstMax: 5,
  restMinMin: 25,
  restMaxMin: 90,
  // Instagram's informal ceiling is around 150-200 follows a day and roughly 60
  // an hour, and this account was rate-limited after sustaining ~55/hour for
  // nine hours (426 in a day). A daily cap is therefore on by default rather
  // than opt-in: unlimited is a setting to choose deliberately, not to inherit.
  // Local hours the account is "awake", in 24-hour form; the popup shows them
  // as AM/PM. Following at 4am every night is a tell.
  //
  // 0 rather than 24 for midnight: both behave identically in `awake()` (the
  // wrap branch handles start > end), but 24 cannot survive a round trip
  // through a 12-hour picker, so storing it would make the form change the
  // value just by being opened and saved.
  wakeHour: 8,
  sleepHour: 0,
};

const COOKIE_NAMES = [
  "sessionid",
  "csrftoken",
  "ds_user_id",
  "ig_did",
  "mid",
  "datr",
  "rur",
];

const ALARM_FOLLOW = "warmr-follow";
const ALARM_COOKIES = "warmr-cookies";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Set when a short gap is waiting on a timer rather than an alarm.
let pendingTimer = null;
const rand = (min, max) => min + Math.random() * (max - min);

async function config() {
  const stored = await chrome.storage.local.get(Object.keys(DEFAULTS));
  return { ...DEFAULTS, ...stored };
}

async function state() {
  const s = await chrome.storage.local.get([
    "queue",
    "doneToday",
    "day",
    "burstLeft",
    "blockedUntil",
    "lastLog",
    "hourStamp",
    "doneThisHour",
  ]);
  return {
    queue: s.queue ?? [],
    doneToday: s.doneToday ?? 0,
    day: s.day ?? "",
    burstLeft: s.burstLeft ?? 0,
    blockedUntil: s.blockedUntil ?? 0,
    lastLog: s.lastLog ?? [],
    hourStamp: s.hourStamp ?? "",
    doneThisHour: s.doneThisHour ?? 0,
  };
}

async function log(message, level = "info") {
  const { lastLog } = await state();
  const line = { at: new Date().toISOString(), level, message };
  const next = [line, ...lastLog].slice(0, 60);
  await chrome.storage.local.set({ lastLog: next });
  console.log(`[warmr] ${message}`);
}

function today() {
  return new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD, local
}

/** True when the account is inside its waking window. Handles wrap past midnight. */
function awake(cfg) {
  const hour = new Date().getHours();
  const { wakeHour: start, sleepHour: end } = cfg;
  if (start === end) return true;
  if (start < end) return hour >= start && hour < end;
  return hour >= start || hour < end;
}

/** Minutes until the waking window opens again. */
function minutesUntilWaking(cfg) {
  const now = new Date();
  const target = new Date(now);
  target.setHours(cfg.wakeHour, 0, 0, 0);
  if (target <= now) target.setDate(target.getDate() + 1);
  return (target - now) / 60000;
}

// --- server ---------------------------------------------------------------

/**
 * Call one of the database functions the extension is allowed to use.
 *
 * PostgREST exposes them under /rest/v1/rpc/<name>. Tables are NOT reachable
 * with this key - migration 0003 keeps them closed to `anon`, and migration
 * 0011 grants EXECUTE on exactly these four functions. So the worst a stolen
 * key can do is scramble the follow queue; it cannot read session cookies,
 * leads or stories.
 */
async function rpc(cfg, fn, args) {
  const base = cfg.supabaseUrl.replace(/\/$/, "");
  const response = await fetch(`${base}/rest/v1/rpc/${fn}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      apikey: cfg.anonKey,
      Authorization: `Bearer ${cfg.anonKey}`,
    },
    body: JSON.stringify(args),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(`${fn} -> ${response.status} ${text.slice(0, 160)}`);
  }
  const text = await response.text();
  return text ? JSON.parse(text) : null;
}

// --- cookies --------------------------------------------------------------

/**
 * Read this profile's Instagram cookies and push them to the server.
 *
 * `chrome.cookies` sees httpOnly cookies, which page JavaScript cannot - that
 * includes `sessionid` and `datr`, the two that matter most.
 */
async function refreshCookies(reason = "scheduled") {
  const cfg = await config();
  if (!cfg.supabaseUrl || !cfg.anonKey) {
    await log("cookie refresh skipped: extension not configured yet", "warn");
    return { ok: false, error: "not configured" };
  }

  const all = await chrome.cookies.getAll({ domain: ".instagram.com" });
  const jar = {};
  for (const cookie of all) {
    if (COOKIE_NAMES.includes(cookie.name)) jar[cookie.name] = cookie.value;
  }

  if (!jar.sessionid) {
    await log("no sessionid in this profile - is it logged into Instagram?", "error");
    return { ok: false, error: "not logged in" };
  }

  const username = (await currentSession(cfg)) || "";
  if (!username) {
    await log("cannot determine the Instagram username for this profile", "error");
    return { ok: false, error: "unknown username" };
  }

  try {
    await rpc(cfg, "ext_save_cookies", {
      p_username: username,
      p_sessionid: jar.sessionid,
      p_csrftoken: jar.csrftoken ?? null,
      p_ds_user_id: jar.ds_user_id ?? null,
      p_ig_did: jar.ig_did ?? null,
      p_mid: jar.mid ?? null,
      p_datr: jar.datr ?? null,
      p_rur: jar.rur ?? null,
      p_user_agent: navigator.userAgent,
    });
    const missing = COOKIE_NAMES.filter((n) => !jar[n]);
    await chrome.storage.local.set({ session: username, lastCookieSync: Date.now() });
    await log(
      `cookies refreshed for ${username} (${reason})${
        missing.length ? `, missing: ${missing.join(", ")}` : ""
      }`,
    );
    return { ok: true, username };
  } catch (error) {
    await log(`cookie refresh failed: ${error.message}`, "error");
    return { ok: false, error: error.message };
  }
}

/**
 * The account this profile is logged into RIGHT NOW, checked against cookies.
 *
 * `cfg.session` is a cache, not the truth. A profile that is logged out and
 * into a different account keeps the old name otherwise, and then claims
 * targets as account A while following them from account B - so the follows
 * land on the wrong account and reclaim can never free them, because the
 * session it names is not the one doing the work.
 *
 * The cookie carries `ds_user_id`, so the check is free: compare it to the id
 * behind the cached name only when the cached name is missing or the id moved.
 */
async function currentSession(cfg) {
  const all = await chrome.cookies.getAll({ domain: ".instagram.com" });
  const dsUserId = all.find((c) => c.name === "ds_user_id")?.value;
  const { sessionUserId } = await chrome.storage.local.get("sessionUserId");

  if (cfg.session && dsUserId && sessionUserId === dsUserId) {
    return cfg.session; // cache still matches this profile
  }

  const username = await detectUsername();
  if (username) {
    if (cfg.session && username !== cfg.session) {
      await log(
        `this profile is now ${username}, was ${cfg.session} - switching`,
        "warn",
      );
    }
    await chrome.storage.local.set({ session: username, sessionUserId: dsUserId ?? null });
    return username;
  }

  // Detection failed - usually a 429 while resolving the id, not a logged-out
  // profile. A name typed under Settings is a deliberate answer to exactly this
  // question, so use it rather than refusing to start. Binding it to the
  // current ds_user_id means the profile-switch check still works afterwards.
  if (cfg.session) {
    await log(`could not verify the account; using ${cfg.session} from Settings`, "warn");
    await chrome.storage.local.set({ sessionUserId: dsUserId ?? null });
    return cfg.session;
  }

  return null;
}

/**
 * Which Instagram account this Chrome profile is logged into.
 *
 * Tried cheapest first, because opening a tab to answer a question the cookies
 * already answer is both slow and visible:
 *
 * 1. `ds_user_id` from the cookie jar, resolved to a username through the
 *    public user-info endpoint. No tab, works even with no Instagram open.
 * 2. An Instagram tab that happens to be open already.
 * 3. Only then, a background tab.
 *
 * The username is not cosmetic: it is what `ext_claim_targets` records as the
 * owner of each target, and therefore what lets a dead profile's targets be
 * reclaimed by the others.
 */
/**
 * Runs INSIDE an Instagram tab and returns the viewer's username.
 *
 * Anchored on the account id wherever possible, because the first bare
 * "username" in a profile page belongs to the profile being viewed, not the
 * viewer - the mistake that made this report a stranger's handle. The second
 * pattern is the shape the viewer's own block takes, verified against a saved
 * logged-in page.
 */
function extractViewerUsername(ownId) {
  const html = document.documentElement.innerHTML;
  const patterns = [];
  if (ownId) {
    patterns.push(
      new RegExp(
        '"id"\\s*:\\s*"' + ownId + '"[^{}]{0,4000}?"username"\\s*:\\s*"([A-Za-z0-9._]{1,30})"',
      ),
    );
  }
  patterns.push(/"username"\s*:\s*"([A-Za-z0-9._]{1,30})"\s*,\s*"is_supervised_user"/);
  for (const pattern of patterns) {
    const match = html.match(pattern);
    if (match) return match[1];
  }
  return null;
}

async function detectUsername() {
  const all = await chrome.cookies.getAll({ domain: ".instagram.com" });
  const dsUserId = all.find((c) => c.name === "ds_user_id")?.value;

  if (dsUserId) {
    // Two endpoints, because either can be throttled on its own and a 429 here
    // used to look like "not logged in". The second is the one the website
    // itself calls, so it answers whenever the session works at all.
    const lookups = [
      {
        url: `https://i.instagram.com/api/v1/users/${dsUserId}/info/`,
        headers: { "User-Agent": "Instagram 302.0.0.23.114 Android" },
        pick: (d) => d?.user?.username,
      },
      {
        url: "https://www.instagram.com/api/v1/accounts/current_user/",
        headers: { "X-IG-App-ID": "936619743392459" },
        pick: (d) => d?.user?.username,
      },
    ];

    for (const lookup of lookups) {
      try {
        const response = await fetch(lookup.url, {
          headers: lookup.headers,
          credentials: "include",
        });
        if (!response.ok) continue;
        const data = await response.json();
        const username = lookup.pick(data);
        if (username) return username;
      } catch {
        // Offline, throttled, or declined - try the next.
      }
    }
  }

  // Ask a tab that is already open - still no new window for the operator.
  //
  // `scripting.executeScript` rather than sendMessage: a tab loaded before the
  // extension was installed has no content script in it, and messaging such a
  // tab throws. Injecting works regardless, which matters most on a fresh
  // install - exactly when there is an Instagram tab open and no listener in
  // it.
  const existing = await chrome.tabs.query({ url: "https://www.instagram.com/*" });
  for (const tab of existing) {
    try {
      const [result] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        args: [dsUserId ?? ""],
        func: extractViewerUsername,
      });
      if (result?.result) return result.result;
    } catch {
      // Tab closed mid-query, or not scriptable. Try the next.
    }
  }

  // Last resort. Only reached when the cookies carry no ds_user_id AND nothing
  // is open, which in practice means the profile is not logged in at all.
  const tab = await chrome.tabs.create({
    url: "https://www.instagram.com/",
    active: false,
  });
  try {
    await waitForLoad(tab.id);
    await sleep(1500);
    const [result] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      args: [dsUserId ?? ""],
      func: extractViewerUsername,
    });
    return result?.result ?? null;
  } catch {
    return null;
  } finally {
    await chrome.tabs.remove(tab.id).catch(() => {});
  }
}

function waitForLoad(tabId, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error("tab load timed out"));
    }, timeoutMs);
    function listener(id, info) {
      if (id === tabId && info.status === "complete") {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

// --- following ------------------------------------------------------------

/** Open one profile, click Follow, close the tab, report the outcome. */
async function followOne(cfg, target) {
  // `ext_begin_check` used to run here to light up "checking now" on the
  // dashboard. Removed: it is a round trip per follow for a flag that is stale
  // within seconds, and the outcome report a moment later carries the same
  // information. Fewer moving parts per follow is the point of this pass.

  const tab = await chrome.tabs.create({ url: target.url, active: false });
  let result = { outcome: "failed", detail: "tab never loaded" };

  try {
    await waitForLoad(tab.id);
    // Let the profile header render. Scaled to the configured pace: when the
    // operator has asked for short gaps, a fixed 1.2-3s settle is a large part
    // of each follow, and the content script polls for the button anyway.
    const settleMax = Math.min(3000, Math.max(400, cfg.gapMinSec * 25));
    await sleep(rand(Math.min(400, settleMax), settleMax));
    try {
      result = await chrome.tabs.sendMessage(tab.id, { type: "FOLLOW_CURRENT" });
    } catch (messageError) {
      // "Receiving end does not exist": the content script is not in this tab.
      // It happens on the first tab after an install or reload, before Chrome
      // has injected declared scripts. Inject it and ask once more, rather than
      // reporting a failure for a follow that was never attempted.
      if (!/Receiving end does not exist|Could not establish connection/i.test(
        String(messageError.message ?? messageError),
      )) {
        throw messageError;
      }
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        files: ["content.js"],
      });
      await sleep(400);
      result = await chrome.tabs.sendMessage(tab.id, { type: "FOLLOW_CURRENT" });
    }
  } catch (error) {
    result = { outcome: "failed", detail: String(error.message ?? error).slice(0, 200) };
  } finally {
    await chrome.tabs.remove(tab.id).catch(() => {});
  }

  try {
    await rpc(cfg, "ext_report_follow", {
      p_session: cfg.session,
      p_target: Number(target.id),
      p_username: target.username,
      p_outcome: result.outcome,
      p_detail: result.detail ?? null,
    });
  } catch (error) {
    // The follow may well have landed; only the report failed. Leaving it here
    // means the row stays `claimed` forever - never retried, never reclaimed,
    // and invisible. Queue it so a later tick can report it once the network is
    // back, and let the stale-claim sweep catch it if this profile never runs
    // again.
    await log(`could not report ${target.username}: ${error.message}`, "error");
    const { pendingReports = [] } = await chrome.storage.local.get("pendingReports");
    pendingReports.push({
      target_id: target.id,
      username: target.username,
      outcome: result.outcome,
      detail: result.detail ?? null,
    });
    await chrome.storage.local.set({ pendingReports: pendingReports.slice(-200) });
  }

  return result;
}

/** Flush outcomes whose report failed earlier, oldest first. */
async function flushPendingReports(cfg) {
  const { pendingReports = [] } = await chrome.storage.local.get("pendingReports");
  if (pendingReports.length === 0) return;

  const remaining = [];
  for (const item of pendingReports) {
    try {
      await rpc(cfg, "ext_report_follow", {
        p_session: cfg.session,
        p_target: Number(item.target_id),
        p_username: item.username,
        p_outcome: item.outcome,
        p_detail: item.detail,
      });
    } catch {
      remaining.push(item); // still unreachable; keep it for next time
    }
  }
  await chrome.storage.local.set({ pendingReports: remaining });
  const sent = pendingReports.length - remaining.length;
  if (sent > 0) await log(`reported ${sent} outcome(s) held from earlier`);
}

/**
 * One tick: follow at most one account, then schedule the next tick.
 *
 * Deliberately one-at-a-time. The alarm carries the rhythm, so a crash or a
 * browser restart loses at most one follow and never double-follows.
 */
async function tick() {
  const cfg = await config();
  if (!cfg.enabled) return;
  if (!cfg.supabaseUrl || !cfg.anonKey) {
    await log("not configured - open the popup and set the Supabase URL and key", "warn");
    return;
  }

  // Re-check which account this profile is on before doing anything with its
  // name: claiming as the wrong session is worse than not claiming at all.
  const session = await currentSession(cfg);
  if (!session) {
    await log("no Instagram account detected in this profile - is it logged in?", "error");
    return scheduleNext(rand(10, 20));
  }
  cfg.session = session;

  await flushPendingReports(cfg);

  const st = await state();

  // An action block stops writes for a day or two. Reads are unaffected, and
  // the collector keeps working - stopping those too would cost stories for a
  // problem that only concerns writes.
  if (Date.now() < st.blockedUntil) {
    const hours = Math.round((st.blockedUntil - Date.now()) / 3600000);
    await log(`resting after a block, ~${hours}h left`);
    return scheduleNext(rand(30, 60));
  }

  if (!awake(cfg)) {
    // Sleep until the window opens rather than waking every half hour to say
    // the same thing. The extra 0-40 minutes means a fleet of profiles does not
    // all start following at 08:00:00 exactly.
    const minutes = minutesUntilWaking(cfg) + rand(0, 40);
    const at = new Date(Date.now() + minutes * 60000);
    const hour12 = (h) => {
      const n = ((Number(h) % 24) + 24) % 24;
      return `${n % 12 === 0 ? 12 : n % 12} ${n < 12 ? "AM" : "PM"}`;
    };
    await log(
      `asleep until ${at.toLocaleTimeString([], {
        hour: "numeric",
        minute: "2-digit",
      })} (active hours are ${hour12(cfg.wakeHour)} to ${hour12(cfg.sleepHour)}) - ` +
        "change them under Settings to follow now",
    );
    return scheduleNext(minutes);
  }

  // Roll the day over.
  let doneToday = st.doneToday;
  if (st.day !== today()) {
    doneToday = 0;
    await chrome.storage.local.set({ day: today(), doneToday: 0 });
  }

  if (cfg.dailyLimit > 0 && doneToday >= cfg.dailyLimit) {
    await log(`daily limit reached (${doneToday}/${cfg.dailyLimit})`);
    return scheduleNext(rand(30, 60));
  }

  // Hourly ceiling. Sustained ~55/hour is what got this account throttled, so
  // the cap is on the rate rather than only on the daily total.
  const hourNow = new Date().toISOString().slice(0, 13);
  let doneThisHour = st.hourStamp === hourNow ? st.doneThisHour : 0;
  if (st.hourStamp !== hourNow) {
    await chrome.storage.local.set({ hourStamp: hourNow, doneThisHour: 0 });
  }
  if (cfg.hourlyLimit > 0 && doneThisHour >= cfg.hourlyLimit) {
    const minutesLeft = 60 - new Date().getMinutes();
    await log(`hourly limit reached (${doneThisHour}/${cfg.hourlyLimit}) - waiting ${minutesLeft}m`);
    return scheduleNext(minutesLeft + rand(1, 5));
  }

  // Refill the queue from the server when it runs dry.
  let queue = st.queue;
  if (queue.length === 0) {
    try {
      const rows = await rpc(cfg, "ext_claim_targets", {
        p_session: cfg.session,
        p_limit: 10,
      });
      queue = (rows ?? []).map((r) => ({
        id: String(r.target_user_id),
        username: r.username,
        is_private: r.is_private,
        url: `https://www.instagram.com/${r.username}/`,
      }));
      await chrome.storage.local.set({ queue, queueOwner: cfg.session });
      await log(`claimed ${queue.length} targets`);
    } catch (error) {
      await log(`could not claim targets: ${error.message}`, "error");
      return scheduleNext(rand(5, 15));
    }
    if (queue.length === 0) {
      await log("nothing free to claim right now");
      return scheduleNext(rand(15, 40));
    }
  }

  const target = queue[0];
  // Remove it from the queue BEFORE acting. A previous version of this line was
  // lost in a later edit, and the result was the same target followed fourteen
  // times: every failure path returns early without ever shortening the queue,
  // so `queue[0]` stayed the same account forever.
  queue = queue.slice(1);
  await chrome.storage.local.set({ queue });

  // The queue belongs to the session that claimed it. If this profile is now a
  // different account - relogin, or settings copied from another profile - the
  // queued targets are owned by someone else in the database, and acting on
  // them would report follows under the wrong session. Drop the queue instead.
  const { queueOwner } = await chrome.storage.local.get("queueOwner");
  if (queueOwner && queueOwner !== cfg.session) {
    await log(
      `queue belonged to ${queueOwner}, this profile is ${cfg.session} - discarding it`,
      "warn",
    );
    await chrome.storage.local.set({ queue: [], queueOwner: cfg.session, doneIds: [] });
    return scheduleNext(rand(0.05, 0.2));
  }

  // Never act on a queued target without confirming the claim still stands.
  //
  // The queue lives in chrome.storage and survives reloads and upgrades, so it
  // can outlive the claim behind it: the row may have been released, reclaimed
  // by another session, or already followed. Local guards cannot see any of
  // that, and a queue written before those guards existed carries no owner at
  // all - which is how one account was followed twenty-one times from a batch
  // the database had long since freed. The database is the only thing that
  // knows, so ask it.
  const { doneIds = [] } = await chrome.storage.local.get("doneIds");
  if (doneIds.includes(String(target.id))) {
    await log(`skipping ${target.username} - already followed by this profile`, "warn");
    return scheduleNext(rand(0.05, 0.2));
  }

  let stillOurs = false;
  try {
    stillOurs = Boolean(
      await rpc(cfg, "ext_owns_target", {
        p_session: cfg.session,
        p_target: Number(target.id),
      }),
    );
  } catch (error) {
    // Cannot verify: skip rather than risk a repeat. The target stays claimed
    // and the stale-claim sweep will free it if this profile never returns.
    await log(`could not verify ${target.username}: ${error.message}`, "warn");
    return scheduleNext(rand(0.2, 0.5));
  }
  if (!stillOurs) {
    await log(`skipping ${target.username} - no longer claimed by this profile`, "warn");
    return scheduleNext(rand(0.05, 0.2));
  }

  const result = await followOne(cfg, target);

  if (result.outcome === "following" || result.outcome === "requested") {
    const { doneIds: seen = [] } = await chrome.storage.local.get("doneIds");
    seen.push(String(target.id));
    await chrome.storage.local.set({ doneIds: seen.slice(-500) });
    doneToday += 1;
    await chrome.storage.local.set({
      doneToday,
      day: today(),
      hourStamp: hourNow,
      doneThisHour: doneThisHour + 1,
    });
    await log(`followed ${target.username} (${doneToday} today)`);
  } else if (result.outcome === "throttled") {
    // Instagram rate-limited the confirmation, which means this session is
    // going too fast. Back off for a while rather than pressing on into a
    // block, and give the target back untouched.
    await rpc(cfg, "ext_report_follow", {
      p_session: cfg.session,
      p_target: Number(target.id),
      p_username: target.username,
      p_outcome: "throttled",
      p_detail: result.detail ?? "rate limited",
    }).catch(() => {});
    await log(`rate limited on ${target.username} - backing off`, "warn");
    return scheduleNext(rand(20, 45));
  } else if (result.outcome === "blocked") {
    // Give the whole queue back and stand down for a day or two.
    const until = Date.now() + rand(24, 48) * 3600 * 1000;
    await chrome.storage.local.set({ blockedUntil: until, queue: [] });
    for (const item of queue.slice(1)) {
      await rpc(cfg, "ext_report_follow", {
        p_session: cfg.session,
        p_target: Number(item.id),
        p_username: item.username,
        p_outcome: "throttled",
        p_detail: "released after action block",
      }).catch(() => {});
    }
    await log(`ACTION BLOCK on ${target.username} - pausing follows`, "error");
    return scheduleNext(rand(60, 120));
  } else {
    await log(`${target.username}: ${result.outcome} (${result.detail ?? ""})`, "warn");
  }

  // Burst rhythm: a few follows close together, then the phone goes away.
  let burstLeft = st.burstLeft;
  if (burstLeft <= 0) {
    burstLeft = Math.floor(rand(cfg.burstMin, cfg.burstMax + 1));
  }
  burstLeft -= 1;
  await chrome.storage.local.set({ burstLeft });

  const delayMin =
    burstLeft > 0
      ? rand(cfg.gapMinSec, cfg.gapMaxSec) / 60
      : rand(cfg.restMinMin, cfg.restMaxMin);
  return scheduleNext(delayMin);
}

/**
 * Wait `minutes`, then tick again.
 *
 * Chrome clamps `chrome.alarms` to a 30-second floor, so a 10-second gap set in
 * the popup used to be silently stretched to 36s - the setting appeared to do
 * nothing. Short waits therefore use a timer, which is exact; long ones keep
 * the alarm, which survives the service worker being suspended (a timer does
 * not, and a 40-minute rest would simply never fire).
 */
/**
 * Run one tick, and guarantee the next one is scheduled.
 *
 * `tick` schedules the next run at the end of each of its paths, so anything
 * that threw before reaching one killed the loop silently and the operator had
 * to press Start again - which is exactly what a failed follow did, because the
 * reporting call is the most likely thing in there to throw.
 *
 * `running` makes the loop single-flight. Pressing Start while a tick or a
 * pending timer was in flight used to run two chains at once, which is how the
 * same target got followed twice a second apart.
 */
let running = false;

async function safeTick(reason) {
  if (running) {
    await log(`tick skipped - one already in flight (${reason})`, "warn");
    return;
  }
  running = true;
  try {
    await tick();
  } catch (error) {
    await log(`tick failed: ${String(error.message ?? error).slice(0, 160)}`, "error");
    // The loop must outlive any single failure, so re-arm before giving up on
    // this one.
    const cfg = await config();
    if (cfg.enabled) scheduleNext(rand(1, 3));
  } finally {
    running = false;
  }
}

function scheduleNext(minutes) {
  chrome.alarms.clear(ALARM_FOLLOW);
  if (pendingTimer) {
    clearTimeout(pendingTimer);
    pendingTimer = null;
  }

  if (minutes < 0.75) {
    pendingTimer = setTimeout(() => {
      pendingTimer = null;
      safeTick("timer");
    }, Math.max(1000, minutes * 60000));
    return;
  }
  chrome.alarms.create(ALARM_FOLLOW, { delayInMinutes: minutes });
}

// --- wiring ---------------------------------------------------------------

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === ALARM_FOLLOW) await safeTick("alarm");
  if (alarm.name === ALARM_COOKIES) await refreshCookies("scheduled");
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  (async () => {
    if (message?.type === "REFRESH_COOKIES") {
      sendResponse(await refreshCookies("manual"));
    } else if (message?.type === "START") {
      // Refuse to "start" into a state that can only log an error every tick.
      const cfg = await config();
      if (!cfg.supabaseUrl || !cfg.anonKey) {
        sendResponse({ ok: false, error: "Set the Supabase URL and key first" });
        return;
      }
      await chrome.storage.local.set({ enabled: true, blockedUntil: 0 });
      await applySchedules();
      await log("started");
      // Say up front if nothing will happen tonight, rather than letting the
      // Status tab read "running" while the log quietly says otherwise.
      const asleep = !awake(cfg);
      await safeTick("start");
      sendResponse({
        ok: true,
        asleep,
        detail: asleep
          ? `Started, but it is outside the active hours you set. Nothing will ` +
            `run until then - change them under Settings to follow now.`
          : null,
      });
    } else if (message?.type === "STOP") {
      await chrome.storage.local.set({ enabled: false });
      await chrome.alarms.clear(ALARM_FOLLOW);
      // A short gap is waiting on a timer, not an alarm; clearing only the
      // alarm would let one more follow fire after Stop.
      if (pendingTimer) {
        clearTimeout(pendingTimer);
        pendingTimer = null;
      }
      await log("stopped");
      sendResponse({ ok: true });
    } else if (message?.type === "CLEAR_LOG") {
      await chrome.storage.local.set({ lastLog: [] });
      sendResponse({ ok: true });
    } else if (message?.type === "STATUS") {
      const cfg = await config();
      const st = await state();
      const { lastCookieSync } = await chrome.storage.local.get("lastCookieSync");
      sendResponse({ cfg, st, lastCookieSync: lastCookieSync ?? null });
    } else if (message?.type === "TEST_CONNECTION") {
      sendResponse(await testConnection());
    } else if (message?.type === "SAVE_CONFIG") {
      await chrome.storage.local.set(message.config);
      await applySchedules();
      sendResponse({ ok: true });
    } else {
      sendResponse({ ok: false, error: "unknown message" });
    }
  })();
  return true;
});

/**
 * Check the credentials against Supabase and report a usable message.
 *
 * Claims zero targets on purpose - `p_limit: 0` is clamped to 1 by the function,
 * so this instead asks for the session's own name, which exercises the same
 * auth path without consuming work from the queue.
 */
async function testConnection() {
  const cfg = await config();
  if (!cfg.supabaseUrl || !cfg.anonKey) {
    return { ok: false, error: "URL or key is empty" };
  }

  const username = (await currentSession(cfg)) || "";
  if (!username) {
    // By this point the cookie lookup, an open tab and a fresh tab have all
    // failed, which almost always means the profile is not logged in.
    const all = await chrome.cookies.getAll({ domain: ".instagram.com" });
    const hasSession = all.some((c) => c.name === "sessionid" && c.value);
    return {
      ok: false,
      error: hasSession
        ? "logged in, but Instagram would not confirm which account (likely rate " +
          "limited) - type the username under Settings and save again"
        : "this Chrome profile is not logged into Instagram - log in, then try again",
    };
  }
  await chrome.storage.local.set({ session: username });

  try {
    // A refresh both proves the key works and does something useful: it stores
    // this profile's current cookies, which is half the point of the extension.
    const all = await chrome.cookies.getAll({ domain: ".instagram.com" });
    const jar = {};
    for (const cookie of all) {
      if (COOKIE_NAMES.includes(cookie.name)) jar[cookie.name] = cookie.value;
    }
    if (!jar.sessionid) {
      return { ok: false, error: "no sessionid - log into Instagram in this profile" };
    }
    await rpc(cfg, "ext_save_cookies", {
      p_username: username,
      p_sessionid: jar.sessionid,
      p_csrftoken: jar.csrftoken ?? null,
      p_ds_user_id: jar.ds_user_id ?? null,
      p_ig_did: jar.ig_did ?? null,
      p_mid: jar.mid ?? null,
      p_datr: jar.datr ?? null,
      p_rur: jar.rur ?? null,
      p_user_agent: navigator.userAgent,
    });
    await chrome.storage.local.set({ lastCookieSync: Date.now() });
    await log(`connection OK, cookies stored for ${username}`);
    return { ok: true, detail: `Account: ${username}` };
  } catch (error) {
    const message = String(error.message ?? error);
    await log(`connection test failed: ${message}`, "error");
    if (message.includes("401") || message.includes("JWT")) {
      return { ok: false, error: "key rejected (401) - check the anon key" };
    }
    if (message.includes("404")) {
      return { ok: false, error: "function not found (404) - is migration 0011 applied?" };
    }
    if (message.includes("Failed to fetch")) {
      return { ok: false, error: "cannot reach that URL - check the Supabase URL" };
    }
    return { ok: false, error: message.slice(0, 140) };
  }
}

async function applySchedules() {
  const cfg = await config();
  const hours = Math.min(Math.max(Number(cfg.cookieHours) || 6, 2), 12);
  await chrome.alarms.clear(ALARM_COOKIES);
  chrome.alarms.create(ALARM_COOKIES, {
    delayInMinutes: 1,
    periodInMinutes: hours * 60,
  });
}

chrome.runtime.onInstalled.addListener(async (details) => {
  await applySchedules();
  // A queue carried across an update may name targets this profile no longer
  // owns, and the version that produced it had no ownership check. Cheaper to
  // re-claim than to reason about what is still valid in it.
  if (details.reason === "update" || details.reason === "install") {
    await chrome.storage.local.set({ queue: [], queueOwner: null, doneIds: [] });
  }
  await log(`extension ${details.reason ?? "loaded"} - queue reset`);
});

chrome.runtime.onStartup.addListener(async () => {
  await applySchedules();
  const cfg = await config();
  if (cfg.enabled) await safeTick("startup");
});
