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
];

// Stored as 24-hour numbers because the scheduling logic works in them, but
// shown as 12-hour + AM/PM: "Sleep hour 24" was a genuinely confusing way to
// say midnight.
const HOUR_FIELDS = ["wakeHour", "sleepHour"];

const NUMERIC = new Set([
  "cookieHours",
  "dailyLimit",
  "gapMinSec",
  "gapMaxSec",
  "restMinMin",
  "restMaxMin",
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

/** 24-hour value -> {hour12, meridiem}. Midnight is 12 AM, noon is 12 PM. */
function to12(hour24) {
  const h = ((Number(hour24) % 24) + 24) % 24;
  const meridiem = h < 12 ? "AM" : "PM";
  const hour12 = h % 12 === 0 ? 12 : h % 12;
  return { hour12, meridiem };
}

/** {hour12, meridiem} -> 24-hour value. */
function to24(hour12, meridiem) {
  const h = Number(hour12) % 12;
  return meridiem === "PM" ? h + 12 : h;
}

/** Fill the 1-12 options once. */
function primeHourSelects() {
  for (const id of ["wakeHour12", "sleepHour12"]) {
    const select = $(id);
    if (select.options.length) continue;
    for (let h = 1; h <= 12; h += 1) {
      const option = document.createElement("option");
      option.value = String(h);
      option.textContent = String(h);
      select.appendChild(option);
    }
  }
}

/** Say the window back in plain words, so a wrong setting is obvious. */
function describeHours() {
  const wake = to24($("wakeHour12").value, $("wakeMeridiem").value);
  const sleep = to24($("sleepHour12").value, $("sleepMeridiem").value);
  const label = (h) => {
    const { hour12, meridiem } = to12(h);
    return `${hour12}:00 ${meridiem}`;
  };
  const summary = $("hoursSummary");
  if (wake === sleep) {
    summary.textContent = "Follows around the clock - no quiet hours.";
    return;
  }
  const overnight = wake > sleep;
  summary.textContent =
    `Follows between ${label(wake)} and ${label(sleep)}` +
    (overnight ? " (overnight, across midnight)." : ", and rests outside that.");
}

/** Mirrors `awake()` in background.js, including the wrap past midnight. */
function withinHours(cfg) {
  const hour = new Date().getHours();
  const start = Number(cfg.wakeHour);
  const end = Number(cfg.sleepHour);
  if (start === end) return true;
  if (start < end) return hour >= start && hour < end;
  return hour >= start || hour < end;
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

for (const id of ["wakeHour12", "wakeMeridiem", "sleepHour12", "sleepMeridiem"]) {
  document.getElementById(id).addEventListener("change", describeHours);
}

/**
 * Pace presets.
 *
 * Throughput is dominated by the REST between bursts, not the gap inside one,
 * which is the opposite of what the log suggests when two follows are two
 * minutes apart. Estimates assume ~6s of unavoidable work per follow (open the
 * tab, load, click, report).
 */
const PRESETS = {
  safe: { gapMinSec: 45, gapMaxSec: 150, restMinMin: 25, restMaxMin: 90 },
  moderate: { gapMinSec: 20, gapMaxSec: 45, restMinMin: 5, restMaxMin: 15 },
  fast: { gapMinSec: 8, gapMaxSec: 20, restMinMin: 1, restMaxMin: 4 },
};

function estimateRate() {
  const gap = (Number($("gapMinSec").value) + Number($("gapMaxSec").value)) / 2;
  const rest = ((Number($("restMinMin").value) + Number($("restMaxMin").value)) / 2) * 60;
  const burst = 3.5; // midpoint of the 2-5 burst
  const perCycle = burst * (gap + 6) + rest;
  if (!Number.isFinite(perCycle) || perCycle <= 0) return;
  const perHour = (burst / perCycle) * 3600;
  $("paceHint").textContent =
    `About ${perHour.toFixed(0)} follows/hour at these settings. ` +
    "Throughput is set mostly by the rest between bursts, not the gap inside one.";
}

for (const button of document.querySelectorAll(".preset")) {
  button.addEventListener("click", () => {
    const preset = PRESETS[button.dataset.preset];
    for (const [key, value] of Object.entries(preset)) $(key).value = String(value);
    estimateRate();
  });
}

for (const id of ["gapMinSec", "gapMaxSec", "restMinMin", "restMaxMin"]) {
  $(id).addEventListener("input", estimateRate);
}

// --- render ---------------------------------------------------------------

async function render() {
  const { cfg, st, lastCookieSync } = await send({ type: "STATUS" });

  if (!fieldsPrimed) {
    primeHourSelects();
    for (const key of FIELDS) {
      if ($(key)) $(key).value = cfg[key] ?? "";
    }
    const wake = to12(cfg.wakeHour);
    $("wakeHour12").value = String(wake.hour12);
    $("wakeMeridiem").value = wake.meridiem;
    const sleep = to12(cfg.sleepHour);
    $("sleepHour12").value = String(sleep.hour12);
    $("sleepMeridiem").value = sleep.meridiem;
    describeHours();
    estimateRate();
    fieldsPrimed = true;
  }

  const blocked = Date.now() < (st.blockedUntil ?? 0);
  const status = $("status");
  if (blocked) {
    const hours = Math.round((st.blockedUntil - Date.now()) / 3600000);
    status.textContent = `blocked (~${hours}h)`;
    status.className = "warn";
  } else if (cfg.enabled && !withinHours(cfg)) {
    // "running" while nothing can run is the reading that sent someone to the
    // logs to find out why. Say it on the status line instead.
    const wake = to12(cfg.wakeHour);
    status.textContent = `asleep until ${wake.hour12} ${wake.meridiem}`;
    status.className = "warn";
  } else if (cfg.enabled) {
    status.textContent = "running";
    status.className = "on";
  } else {
    status.textContent = "stopped";
    status.className = "off";
  }

  const toggle = $("toggle");
  toggle.textContent = cfg.enabled ? "Stop" : "Start";
  toggle.className = `act ${cfg.enabled ? "stop" : "primary"}`;

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

/**
 * One button, because Start and Stop are one decision.
 *
 * Two buttons meant the running state had to be read off the status line to
 * know which one to press, and Start while already running was a legal click
 * that started a second chain.
 */
$("toggle").addEventListener("click", async () => {
  const button = $("toggle");
  button.disabled = true;

  const { cfg } = await send({ type: "STATUS" });
  if (cfg.enabled) {
    await send({ type: "STOP" });
    message($("statusMsg"), "Stopped.", "good");
  } else {
    const result = await send({ type: "START" });
    if (result && result.ok === false) {
      message($("statusMsg"), result.error, "bad");
    } else if (result?.asleep) {
      message($("statusMsg"), result.detail, "bad");
    } else {
      message($("statusMsg"), "Running. Watch the Logs tab.", "good");
    }
  }

  button.disabled = false;
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
  config.wakeHour = to24($("wakeHour12").value, $("wakeMeridiem").value);
  config.sleepHour = to24($("sleepHour12").value, $("sleepMeridiem").value);

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
