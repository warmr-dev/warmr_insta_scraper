/**
 * Runs on every Instagram page. Finds the Follow button and clicks it.
 *
 * Clicking the real button rather than calling the API is the whole point: the
 * page mints its own `fb_dtsg`, and a click carries the genuine token, headers
 * and fingerprint of a logged-in browser. Replayed cookies could not do this -
 * Instagram serves a logged-out write token to a session it does not recognise
 * as the browser that logged in, which is why every out-of-browser follow was
 * rejected.
 *
 * Buttons are found by their TEXT, never by class name: Instagram's classes are
 * generated per build and change without notice, so a class selector is a
 * guaranteed future outage.
 */

// Instagram renders the profile header asynchronously; the button may not exist
// for a second or two after load.
const BUTTON_TIMEOUT_MS = 12000;
const POLL_MS = 250;

// Exact button labels, lowercased. Order matters only for readability.
const FOLLOW_LABELS = ["follow", "follow back"];
const ALREADY_LABELS = ["following", "requested", "message"];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Every clickable element on the page, including Instagram's div-buttons. */
function clickables() {
  return Array.from(
    document.querySelectorAll('button, div[role="button"], a[role="button"]'),
  );
}

function labelOf(el) {
  return (el.innerText || el.textContent || "").trim().toLowerCase();
}

/**
 * The profile's own Follow button.
 *
 * A profile page also contains Follow buttons for suggested accounts in the
 * sidebar, so taking the first match on the page would follow a stranger. The
 * real one lives inside the <header>; fall back to the topmost match only when
 * there is no header, and never accept one that sits low on the page.
 */
function findFollowButton() {
  const candidates = clickables().filter((el) =>
    FOLLOW_LABELS.includes(labelOf(el)),
  );
  if (candidates.length === 0) return null;

  const header = document.querySelector("header");
  const inHeader = candidates.filter((el) => header && header.contains(el));
  if (inHeader.length > 0) return inHeader[0];

  // No header (rare layout): take the highest button on the page, and only if
  // it is plausibly in the profile header rather than a sidebar suggestion.
  const sorted = candidates
    .map((el) => ({ el, top: el.getBoundingClientRect().top + window.scrollY }))
    .sort((a, b) => a.top - b.top);
  return sorted[0].top < 600 ? sorted[0].el : null;
}

/** True when the page already shows a following/requested state. */
function alreadyFollowing() {
  const header = document.querySelector("header");
  const scope = header || document.body;
  return Array.from(
    scope.querySelectorAll('button, div[role="button"]'),
  ).some((el) => ALREADY_LABELS.includes(labelOf(el)) && labelOf(el) !== "message");
}

/** Page-level signals that the account cannot be followed at all. */
function pageProblem() {
  const text = document.body ? document.body.innerText : "";
  if (/Sorry, this page isn't available/i.test(text)) return "unavailable";
  if (/user not found/i.test(text)) return "unavailable";
  if (/Try Again Later|We restrict certain activity/i.test(text)) return "blocked";
  if (/Suspended|account has been disabled/i.test(text)) return "blocked";
  if (/challenge_required|Confirm it['’]s You/i.test(text)) return "blocked";
  return null;
}

async function waitForButton() {
  const deadline = Date.now() + BUTTON_TIMEOUT_MS;
  while (Date.now() < deadline) {
    const problem = pageProblem();
    if (problem) return { problem };
    if (alreadyFollowing()) return { already: true };
    const button = findFollowButton();
    if (button) return { button };
    await sleep(POLL_MS);
  }
  return {};
}

/**
 * Follow the account whose profile is currently open.
 *
 * Returns an outcome string the background script reports verbatim, so the
 * server-side attribution (session fault vs target fault) stays in one place.
 */
async function doFollow() {
  const found = await waitForButton();

  if (found.problem) return { outcome: found.problem, detail: "page state" };
  if (found.already) return { outcome: "following", detail: "already followed" };
  if (!found.button) return { outcome: "failed", detail: "no Follow button found" };

  // A person moves the pointer to the button before clicking it. These events
  // are what a real click emits, and dispatching only `click` is a cheap tell.
  found.button.scrollIntoView({ block: "center", behavior: "instant" });
  await sleep(180 + Math.random() * 420);
  for (const type of ["pointerover", "mouseover", "pointerdown", "mousedown", "mouseup"]) {
    found.button.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true }));
  }
  found.button.click();

  // Confirm the state actually changed rather than trusting the click.
  const deadline = Date.now() + 8000;
  while (Date.now() < deadline) {
    await sleep(POLL_MS);
    const problem = pageProblem();
    if (problem) return { outcome: problem, detail: "after click" };

    const header = document.querySelector("header") || document.body;
    const labels = Array.from(
      header.querySelectorAll('button, div[role="button"]'),
    ).map(labelOf);

    if (labels.includes("requested")) return { outcome: "requested" };
    if (labels.includes("following")) return { outcome: "following" };
  }

  return { outcome: "failed", detail: "state did not change after click" };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "FOLLOW_CURRENT") {
    doFollow().then(sendResponse);
    return true; // async response
  }
  if (message?.type === "WHOAMI") {
    // The logged-in username, read from the page's own bootstrap data. Used to
    // label this browser profile without asking the operator to type it.
    const match = document.documentElement.innerHTML.match(
      /"username":"([A-Za-z0-9._]{1,30})","is_verified"/,
    );
    sendResponse({ username: match ? match[1] : null });
    return false;
  }
  return false;
});
