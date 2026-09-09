/** Popup UI: status, the two buttons that matter, and the settings form. */

const FIELDS = [
  "supabaseUrl",
  "anonKey",
  "session",
  "cookieHours",
  "dailyLimit",
  "gapMinSec",
  "gapMaxSec",
  "restMinMin",
  "restMaxMin",
  "wakeHour",
  "sleepHour",
];

const NUMERIC = new Set([
  "cookieHours",
  "dailyLimit",
  "gapMinSec",
  "gapMaxSec",
  "restMinMin",
  "restMaxMin",
  "wakeHour",
  "sleepHour",
]);

const $ = (id) => document.getElementById(id);
const send = (message) => chrome.runtime.sendMessage(message);

function ago(ts) {
  if (!ts) return "never";
  const mins = Math.round((Date.now() - ts) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  return `${Math.round(mins / 60)}h ago`;
}

// Settings inputs are filled ONCE, on open. The old code refilled them on every
// 4-second refresh, skipping only the focused field - so typing a URL and then
// clicking into the key field blanked the URL, and Save stored empty strings.
// The symptom was "not configured" immediately after saving, with no clue why.
let fieldsPrimed = false;

async function render() {
  const { cfg, st, lastCookieSync } = await send({ type: "STATUS" });

  if (!fieldsPrimed) {
    for (const key of FIELDS) {
      if ($(key)) $(key).value = cfg[key] ?? "";
    }
    fieldsPrimed = true;
  }

  const blocked = Date.now() < (st.blockedUntil ?? 0);
  const status = $("status");
  if (blocked) {
    const hours = Math.round((st.blockedUntil - Date.now()) / 3600000);
    status.textContent = `blocked (~${hours}h)`;
    status.className = "warn";
  } else if (cfg.enabled) {
    status.textContent = "running";
    status.className = "on";
  } else {
    status.textContent = "stopped";
    status.className = "off";
  }

  $("account").textContent = cfg.session || "—";
  $("today").textContent = cfg.dailyLimit
    ? `${st.doneToday} / ${cfg.dailyLimit}`
    : String(st.doneToday);
  $("queued").textContent = String(st.queue.length);
  $("synced").textContent = ago(lastCookieSync);

  $("log").innerHTML = (st.lastLog ?? [])
    .map((l) => {
      const time = new Date(l.at).toLocaleTimeString();
      const text = `${time} ${l.message}`.replace(/</g, "&lt;");
      return `<div class="${l.level}">${text}</div>`;
    })
    .join("");
}

$("start").addEventListener("click", async () => {
  const result = await send({ type: "START" });
  if (result && result.ok === false) {
    const box = $("saveError");
    box.className = "err";
    box.style.display = "block";
    box.textContent = result.error;
    // Open Settings so the empty fields are actually visible.
    document.querySelector("details").open = true;
  }
  render();
});

$("stop").addEventListener("click", async () => {
  await send({ type: "STOP" });
  render();
});

$("refresh").addEventListener("click", async () => {
  $("refresh").textContent = "Updating…";
  const result = await send({ type: "REFRESH_COOKIES" });
  $("refresh").textContent = result?.ok ? "Updated ✓" : "Failed — see log";
  setTimeout(() => {
    $("refresh").textContent = "Update tokens now";
    render();
  }, 1800);
});

$("save").addEventListener("click", async () => {
  const config = {};
  for (const key of FIELDS) {
    const raw = $(key).value.trim();
    config[key] = NUMERIC.has(key) ? Number(raw) || 0 : raw;
  }

  // Refuse to store a half-filled config. Saving blanks and only finding out
  // from a background log line is the failure this whole screen should prevent.
  const problems = [];
  if (!config.supabaseUrl) problems.push("Supabase URL is empty");
  else if (!/^https:\/\/[a-z0-9-]+\.supabase\.co\/?$/i.test(config.supabaseUrl)) {
    problems.push("Supabase URL should look like https://xxxx.supabase.co");
  }
  if (!config.anonKey) problems.push("anon key is empty");
  else if (config.anonKey.length < 40) problems.push("anon key looks too short");

  if (problems.length) {
    $("saveError").textContent = problems.join(" · ");
    $("saveError").style.display = "block";
    return;
  }
  $("saveError").style.display = "none";
  // The refresh interval is the one value with a hard range: below 2h is
  // needless churn, above 12h and a session can expire before its next sync.
  config.cookieHours = Math.min(Math.max(config.cookieHours || 6, 2), 12);

  await send({ type: "SAVE_CONFIG", config });

  // Prove the credentials actually reach Supabase, rather than reporting
  // "saved" and letting the first background tick discover the truth an hour
  // later. A wrong key is the single most likely setup mistake.
  $("save").textContent = "Testing…";
  const test = await send({ type: "TEST_CONNECTION" });
  const box = $("saveError");
  box.style.display = "block";
  if (test?.ok) {
    box.className = "err ok";
    box.textContent = `Connected. ${test.detail ?? ""}`.trim();
  } else {
    box.className = "err";
    box.textContent = `Saved, but the connection failed: ${test?.error ?? "unknown"}`;
  }
  $("save").textContent = "Save & test";
  render();
});

render();
setInterval(render, 4000);
