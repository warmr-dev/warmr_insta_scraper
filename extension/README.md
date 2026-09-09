# Warmr Follower — Chrome extension

Follows assigned Instagram accounts from inside a logged-in Chrome profile, and
keeps that profile's session cookies fresh in Supabase.

## Why it exists

Following from outside the browser does not work, and this is not a bug that
better headers can fix. Instagram signs every web write with `fb_dtsg`, a token
the *page* mints and binds to the browser session that logged in. Cookies
replayed from another machine get the **logged-out** token — measured on a live
session: `USER_ID: 0` in the page bootstrap, and `fb_dtsg` bound to a different
id than our own `ds_user_id`. API *reads* still work (`timeline`, `friendships/
show` both 200), which is why the sessions look healthy while every follow is
rejected.

An extension sidesteps this entirely: it clicks the real Follow button in a
profile that is genuinely logged in, so the token, headers and fingerprint are
real by construction rather than by imitation.

## Setup

One Chrome profile per Instagram account. In each profile:

1. Log into Instagram normally.
2. `chrome://extensions` → enable **Developer mode** → **Load unpacked** → pick
   this `extension/` folder.
3. Open the popup → **Settings**:
   - **Supabase URL** — Supabase dashboard → Project Settings → API → Project URL.
   - **Supabase anon key** — the same page, `anon` `public` key.
   - **Instagram username** — leave blank to detect it automatically.
4. **Save settings**, then **Start**.

No server of your own is involved: the extension talks to Supabase directly.

### Why the anon key is safe to put here

Every table stays closed to `anon` (migration 0003), and migration 0011 grants
EXECUTE on exactly four `SECURITY DEFINER` functions:

| Function | What it does |
|---|---|
| `ext_claim_targets` | takes N free targets, atomically |
| `ext_report_follow` | records one outcome |
| `ext_begin_check` | flags "working on this now" |
| `ext_save_cookies` | stores refreshed cookies |

A stolen key can scramble the follow queue. It cannot read session cookies,
leads or stories — verified: `SET ROLE anon; SELECT * FROM cookies` is
`permission denied`.

The claim is a function rather than a REST call for a second reason: PostgREST
cannot express `FOR UPDATE SKIP LOCKED`, and without it two Chrome profiles
claiming at the same moment would be handed the same target.

## What it does

**Following.** Claims a batch of targets via `ext_claim_targets`, opens each
profile in a background tab, clicks Follow, closes the tab, and reports the
outcome via `ext_report_follow`. One follow at a time, driven by an alarm, so a
crash or a browser restart loses at most one and never double-follows.

The claim uses `FOR UPDATE SKIP LOCKED`, so several Chrome profiles running at
once can never be handed the same target — two profiles following one account
would waste budget on someone already covered.

**Token refresh.** Every 2–12 hours (configurable), and on demand via **Update
tokens now**, it reads this profile's Instagram cookies — including the httpOnly
`sessionid` and `datr`, which page scripts cannot see — and stores them via
`ext_save_cookies`. A refresh also re-activates a session that had been
disabled for stale cookies, since that is precisely what the refresh fixes.

## Pacing

Writes are policed far harder than reads, and an even drip of one follow every
N seconds is not a slower human — it is an obvious robot. So:

- **Bursts** of 2–5 follows, 45–150s apart, then a **25–90 minute rest**.
- **Sleep hours**: no follows outside the configured waking window.
- Every interval is **jittered**; nothing runs on a fixed cadence.
- On `feedback_required` the profile **stops following for 24–48h** and hands
  its whole queue back. Reads are untouched, so story collection continues —
  stopping those too would cost stories for a problem that only affects writes.

`Daily limit = 0` means no cap; the rhythm still paces it. Start low on accounts
that are already being rate-limited, and raise it once you see it holding.

## Outcomes

| Reported | Meaning | Effect on the target |
|---|---|---|
| `following` | followed | terminal success |
| `requested` | private account, request pending | terminal success |
| `blocked` | action block / checkpoint | released, **not** blamed |
| `throttled` | rate limited | released, **not** blamed |
| `unavailable` | deleted or suspended | terminal, no retry |
| `failed` | anything else | attempt counted, released below the retry ceiling |

Session faults never count against the target. Otherwise one bad profile would
burn every target's retries before anyone noticed.

## Notes

- Buttons are found by their **text**, never by class name — Instagram's classes
  are generated per build and would break on the next deploy.
- The extension only follows the button inside the profile `<header>`, so it
  cannot accidentally follow a sidebar suggestion.
- The anon key authorises the four functions only, never table access: a leaked
  key costs follow budget, not the pipeline.
