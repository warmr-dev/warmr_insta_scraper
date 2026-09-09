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

// Button labels, lowercased.
//
// Matching is by WORD, not by whole string: Instagram nests an icon inside the
// button, and its accessibility text is part of innerText. The followed-state
// button reads "Following Down chevron icon", so an exact === "following" test
// reported a successful follow as a failure - which is exactly what happened,
// and why this is now `labelHas` rather than an equality check.
const FOLLOW_LABELS = ["follow", "follow back"];
const FOLLOWED_LABELS = ["following"];
const REQUESTED_LABELS = ["requested"];

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
 * True when the button's text starts with `word`.
 *
 * Anchored to the start so "Following" matches but "Follow" inside some longer
 * sentence elsewhere on the page does not, and so the trailing icon text an
 * exact match chokes on is simply ignored.
 */
function labelHas(el, word) {
  const label = labelOf(el);
  return label === word || label.startsWith(`${word} `);
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
    // "Following ..." must never count as "Follow": startsWith on the bare word
    // would match it, so the followed state is excluded first.
    FOLLOW_LABELS.some((w) => labelHas(el, w)) &&
    !FOLLOWED_LABELS.some((w) => labelHas(el, w)),
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

/** True when the header already shows a following/requested state. */
function alreadyFollowing() {
  const header = document.querySelector("header");
  const scope = header || document.body;
  return Array.from(scope.querySelectorAll('button, div[role="button"]')).some(
    (el) =>
      FOLLOWED_LABELS.some((w) => labelHas(el, w)) ||
      REQUESTED_LABELS.some((w) => labelHas(el, w)),
  );
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
  //
  // Scoped to the whole document, not the <header>. Instagram re-renders the
  // header after a follow, and React can replace the subtree entirely - so a
  // query rooted at the old <header> element, or run in the instant between
  // removal and reinsertion, sees nothing and reports a successful follow as a
  // failure. The header is still preferred when it exists; the document is the
  // fallback rather than the only source.
  //
  // 15s rather than 8s because the state can lag on a slow connection, and the
  // cost of waiting is one slow follow while the cost of giving up early is a
  // wrong record that sends another session to follow the same account again.
  const deadline = Date.now() + 15000;
  let sawTransient = false;

  while (Date.now() < deadline) {
    await sleep(POLL_MS);
    const problem = pageProblem();
    if (problem) return { outcome: problem, detail: "after click" };

    const scope = document.querySelector("header") || document;
    const buttons = [
      ...scope.querySelectorAll('button, div[role="button"]'),
      // Belt and braces: if the header was replaced, look document-wide too.
      ...document.querySelectorAll('header button, header div[role="button"]'),
    ];

    if (buttons.some((el) => REQUESTED_LABELS.some((w) => labelHas(el, w)))) {
      return { outcome: "requested" };
    }
    if (buttons.some((el) => FOLLOWED_LABELS.some((w) => labelHas(el, w)))) {
      return { outcome: "following" };
    }
    // A spinner or a still-"Follow" button means the request is in flight;
    // remember it so the failure message can say which case this was.
    if (buttons.some((el) => FOLLOW_LABELS.some((w) => labelHas(el, w)))) {
      sawTransient = true;
    }
  }

  // Last resort: ask Instagram directly rather than trusting the DOM. The click
  // may well have worked - reporting `failed` releases the target and sends
  // another session to follow an account we already follow.
  const confirmed = await confirmViaApi();
  if (confirmed) return confirmed;

  return {
    outcome: "failed",
    detail: sawTransient
      ? "button still read Follow after 15s"
      : "state did not change after click",
  };
}

/**
 * Ask Instagram whether we now follow this profile.
 *
 * The DOM is the primary signal because it needs no request, but it is also the
 * part most likely to change shape. `friendships/show` is authoritative and
 * costs one cheap authenticated GET from inside the page, where the session is
 * real. Returns null when it cannot answer, so the caller keeps its own verdict.
 */
async function confirmViaApi() {
  try {
    const id = (document.documentElement.innerHTML.match(
      /"profile_id"\s*:\s*"(\d+)"/,
    ) || document.documentElement.innerHTML.match(
      /"user_id"\s*:\s*"(\d+)"/,
    ))?.[1];
    if (!id) return null;

    const response = await fetch(`/api/v1/friendships/show/${id}/`, {
      headers: { "X-IG-App-ID": "936619743392459" },
      credentials: "include",
    });
    if (!response.ok) return null;

    const data = await response.json();
    if (data.following) return { outcome: "following", detail: "confirmed via API" };
    if (data.outgoing_request) {
      return { outcome: "requested", detail: "confirmed via API" };
    }
    return null;
  } catch {
    return null;
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "FOLLOW_CURRENT") {
    doFollow().then(sendResponse);
    return true; // async response
  }
  if (message?.type === "WHOAMI") {
    // The VIEWER's username, from the page bootstrap.
    //
    // `"username":"x","is_verified"` was wrong: on a profile page the first
    // such match is the profile being LOOKED AT, so this reported whichever
    // account the operator happened to be browsing. These keys belong to the
    // viewer specifically, and are tried in order of how tightly they are
    // bound to the logged-in session.
    const html = document.documentElement.innerHTML;
    const patterns = [
      /"viewer"\s*:\s*\{[^}]*?"username"\s*:\s*"([A-Za-z0-9._]{1,30})"/,
      /"viewerId"\s*:\s*"\d+"[^}]*?"username"\s*:\s*"([A-Za-z0-9._]{1,30})"/,
      /"CURRENT_USER_ID"[^}]*?"username"\s*:\s*"([A-Za-z0-9._]{1,30})"/,
    ];
    let username = null;
    for (const pattern of patterns) {
      const match = html.match(pattern);
      if (match) {
        username = match[1];
        break;
      }
    }
    sendResponse({ username });
    return false;
  }
  return false;
});
