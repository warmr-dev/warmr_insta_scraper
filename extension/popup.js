/** Popup UI: status, logs, and settings. */

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

/**
 * Settings inputs are filled ONCE, when the popup opens.
 *
 * The periodic status refresh used to refill them too, skipping only the
 * focused field - so typing a URL and then clicking into the key field blanked
 * the URL behind you, and Save stored empty strings. Never refill a form the
 * user is in the middle of.
 */
let fieldsPrimed = false;

function message(el, text, kind) {
  el.className = `msg ${kind}`;
  el.textContent = text;
}

function ago(ts) {
  if (!ts) return "never";
  const mins = Math.round((Date.now() - ts) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  return `${Math.round(mins / 60)}h ago`;
}

// --- tabs -----------------------------------------------------------------

for (const button of document.querySelectorAll("nav button")) {
  button.addEventListener("click", () => {
    for (const b of document.querySelectorAll("nav button")) {
      b.classList.toggle("active", b === button);
    }
    for (const s of document.querySelectorAll("section")) {
      s.classList.toggle("active", s.id === `${button.dataset.tab}-tab`);
    }
  });
}

// --- render ---------------------------------------------------------------

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

  const lines = st.lastLog ?? [];
  $("log").innerHTML = lines.length
    ? lines
        .map((l) => {
          const time = new Date(l.at).toLocaleTimeString();
          const text = String(l.message).replace(/</g, "&lt;");
          return `<div class="${l.level}"><span class="time">${time}</span>${text}</div>`;
        })
        .join("")
    : '<div class="empty">Nothing yet. Press Start on the Status tab.</div>';
}

// --- actions --------------------------------------------------------------

$("start").addEventListener("click", async () => {
  const result = await send({ type: "START" });
  if (result && result.ok === false) {
    message($("statusMsg"), result.error, "bad");
  } else {
    message($("statusMsg"), "Running. Watch the Logs tab.", "good");
  }
  render();
});

$("stop").addEventListener("click", async () => {
  await send({ type: "STOP" });
  message($("statusMsg"), "Stopped.", "good");
  render();
});

$("refresh").addEventListener("click", async () => {
  const button = $("refresh");
  button.disabled = true;
  button.textContent = "Updating…";
  const result = await send({ type: "REFRESH_COOKIES" });
  message(
    $("statusMsg"),
    result?.ok
      ? `Tokens updated for ${result.username}.`
      : `Update failed: ${result?.error ?? "unknown"}`,
    result?.ok ? "good" : "bad",
  );
  button.disabled = false;
  button.textContent = "Update tokens now";
  render();
});

$("clearLog").addEventListener("click", async () => {
  await send({ type: "CLEAR_LOG" });
  render();
});

$("save").addEventListener("click", async () => {
  const config = {};
  for (const key of FIELDS) {
    const raw = $(key).value.trim();
    config[key] = NUMERIC.has(key) ? Number(raw) || 0 : raw;
  }

  // Refuse a half-filled config rather than storing blanks and letting the
  // background discover it a tick later.
  const problems = [];
  if (!config.supabaseUrl) problems.push("Supabase URL is empty");
  else if (!/^https:\/\/[a-z0-9-]+\.supabase\.co\/?$/i.test(config.supabaseUrl)) {
    problems.push("URL should look like https://xxxx.supabase.co");
  }
  if (!config.anonKey) problems.push("anon key is empty");
  else if (config.anonKey.length < 40) problems.push("anon key looks too short");

  if (problems.length) {
    message($("saveError"), problems.join(" · "), "bad");
    return;
  }

  const button = $("save");
  button.disabled = true;
  button.textContent = "Saving…";
  await send({ type: "SAVE_CONFIG", config });

  // Prove the credentials reach Supabase now, rather than letting the first
  // background tick discover a bad key much later.
  button.textContent = "Testing…";
  const test = await send({ type: "TEST_CONNECTION" });
  message(
    $("saveError"),
    test?.ok
      ? `Connected. ${test.detail ?? ""}`.trim()
      : `Saved, but the connection failed: ${test?.error ?? "unknown"}`,
    test?.ok ? "good" : "bad",
  );
  button.disabled = false;
  button.textContent = "Save & test";
  render();
});

render();
setInterval(render, 4000);
