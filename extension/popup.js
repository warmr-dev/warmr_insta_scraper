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

async function render() {
  const { cfg, st, lastCookieSync } = await send({ type: "STATUS" });

  for (const key of FIELDS) {
    if ($(key) && document.activeElement !== $(key)) $(key).value = cfg[key] ?? "";
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
  await send({ type: "START" });
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
  // The refresh interval is the one value with a hard range: below 2h is
  // needless churn, above 12h and a session can expire before its next sync.
  config.cookieHours = Math.min(Math.max(config.cookieHours || 6, 2), 12);

  await send({ type: "SAVE_CONFIG", config });
  $("save").textContent = "Saved ✓";
  setTimeout(() => {
    $("save").textContent = "Save settings";
    render();
  }, 1200);
});

render();
setInterval(render, 4000);
