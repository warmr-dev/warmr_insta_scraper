# Instagram Stories Monitor — Build Spec

Instructions for Claude Code. Read this whole file before writing any code.

---

## 1. What we are building

A system that watches ~57,000 Instagram accounts for newly posted stories, detects them
within ~1 minute, runs photo stories through a two-stage AI classifier, applies business
rules against an internal vendor database, and pushes qualified leads to Slack.

**The core architectural trick:** we do not poll 57,000 accounts individually. We operate
~20 worker Instagram accounts that collectively follow all 57,000 targets. Each worker
polls `feed/reels_tray/` — a single request that returns the story tray for *all* of that
worker's followings. 57,000 monitored objects collapse into 20 polled objects.

**Videos are ignored.** Only photo stories reach the AI pipeline. Media is never stored
permanently.

### Non-goals

- No Android emulator, no device farm, no Appium. This is plain Python speaking HTTP.
- No permanent media storage. Temp files only, deleted after analysis.
- No email discovery from external sources. OCR of text visible in the image only.
- The Harvard-followers harvesting task is a separate project. Do not build it here.

---

## 2. Constraints that shape every decision

| Constraint | Value | Consequence |
|---|---|---|
| Budget | $650/month total | Cannot poll targets individually. `reels_tray` is mandatory, not optional. |
| Instagram following cap | 7,500 per account | 8 shards minimum for 57k targets. |
| Safe follow rate | ~150/day per account | Bootstrap takes 6–10 weeks. Build the follower as a long-running, resumable process. |
| Story lifetime | 24 hours | A missed poll window means a permanently lost story. Poller uptime matters. |
| Detection target | ~60 seconds | Achieved via phase-shifted polling, not by polling faster. |

This uses Instagram's private mobile API via `instagrapi`, which is against Instagram's
Terms of Service. That decision has been made by the project owner. Your job is to make it
technically sound and operationally survivable — not to re-litigate it.

---

## 3. Stack

- Python 3.11+
- `instagrapi` — Instagram private API client
- PostgreSQL 15+ — all state
- Redis — work queues
- `pydantic-settings` — config
- `asyncio` for pollers, separate processes for pipeline workers
- `structlog` — JSON logging
- `alembic` — migrations
- `pytest` — tests
- `uv` or `pip-tools` for dependency pinning

Do not add a heavy framework (Celery, Airflow, Django). Redis lists plus small worker loops
are sufficient and easier to debug.

---

## 4. Credentials — read this carefully

**Real Instagram credentials and API keys do not exist yet.** They will be supplied later.

Build the entire system against a **fixture transport** so it is fully testable without a
single real account.

```
IG_TRANSPORT=fixture   # replays recorded JSON from ./fixtures/
IG_TRANSPORT=live      # real instagrapi calls
```

Requirements:

- Every Instagram call goes through one interface, `InstagramTransport`, with two
  implementations: `LiveTransport` and `FixtureTransport`. No module outside the transport
  layer may import `instagrapi` directly.
- `FixtureTransport` reads JSON files from `fixtures/reels_tray/*.json` and
  `fixtures/reels_media/*.json`. Commit hand-written fixtures now, covering: normal tray,
  empty tray, tray containing `highlight:` entries, tray with prefetched `items`, private
  account, deleted account, `feedback_required` error, `challenge_required` error,
  `login_required` error.
- The full pipeline (poller → diff → fetch → AI → business checks → Slack) must run
  end-to-end in fixture mode. Slack in fixture mode writes to stdout.
- Secrets come from environment variables only. **Never** hardcode a credential, never
  commit a `.env`, never log a password or session cookie. Provide `.env.example` with
  placeholder values.
- Passwords in `worker_accounts` are encrypted at rest with a key from `SECRET_KEY` env var
  (use `cryptography.fernet`).

---

## 5. Database schema

Use Alembic. Table names and column names below are normative — later phases reference them.

### `worker_accounts`
Our operational Instagram accounts.

```
id                bigserial pk
username          text unique not null
password_enc      bytea not null
shard_id          int not null
proxy_url         text not null          -- bound permanently, never rotate
device_settings   jsonb not null         -- generated once at creation, immutable
session_json      jsonb                  -- instagrapi dump_settings output
status            text not null          -- warming | active | challenged | banned | reserve
phase_offset_sec  int not null default 0 -- stagger within shard
follows_count     int not null default 0
last_login_at     timestamptz
last_poll_at      timestamptz
last_error        text
created_at        timestamptz not null default now()
```

### `targets`
The 57k accounts we monitor.

```
user_id            bigint pk              -- Instagram numeric pk, NOT username
username           text not null
instagram_url      text
shard_id           int
status             text not null default 'active'  -- active | private | deleted | unreachable
last_reel_media_ts bigint                 -- the change-detection watermark
last_seen_in_tray  timestamptz
imported_at        timestamptz not null default now()
```

Index on `(shard_id, status)`.

### `target_follows`
Which worker follows which target, and the state of that relationship.

```
worker_account_id  bigint not null references worker_accounts(id)
target_user_id     bigint not null references targets(user_id)
state              text not null    -- queued | requested | following | rejected | failed
requested_at       timestamptz
confirmed_at       timestamptz
attempts           int not null default 0
last_error         text
primary key (worker_account_id, target_user_id)
```

Index on `(worker_account_id, state)` — the follower process polls this constantly.

### `stories`
One row per discovered story item. This is the dedup table.

```
story_id        text pk                -- Instagram media pk
target_user_id  bigint not null references targets(user_id)
taken_at        timestamptz not null
expiring_at     timestamptz
media_type      int not null           -- 1 = photo, 2 = video
discovered_at   timestamptz not null default now()
pipeline_state  text not null default 'new'
                -- new | skipped_video | analyzing | analyzed | checked | sent | failed
```

### `story_analysis`

```
story_id            text pk references stories(story_id)
ocr_text            text
cheap_score         int
cheap_result        jsonb
smart_score         int
smart_result        jsonb
final_score         int
service_category    text
intent_type         text
ai_explanation      text
analyzed_at         timestamptz
```

### `business_checks`

```
story_id           text pk references stories(story_id)
vendor_id          text
service_fit        numeric
geo_ok             boolean
community_conflict boolean
final_status       text     -- approved | rejected | review
reject_reason      text
checked_at         timestamptz
```

### `slack_deliveries`

```
story_id     text pk references stories(story_id)
status       text not null   -- pending | sent | failed
slack_ts     text
attempts     int not null default 0
last_error   text
sent_at      timestamptz
```

Primary key on `story_id` is the idempotency guarantee — a story can never be posted twice.

### `account_events`
Audit trail for worker account incidents.

```
id                bigserial pk
worker_account_id bigint not null
event_type        text not null   -- challenge | login_required | feedback_required | ban | recovered
detail            text
occurred_at       timestamptz not null default now()
```

### `daily_action_counters`
Rate-limit ledger. Survives restarts, unlike an in-memory counter.

```
worker_account_id bigint not null
day               date not null
follows_done      int not null default 0
requests_done     int not null default 0
primary key (worker_account_id, day)
```

---

## 6. Processes

Seven long-running processes. Each is independently restartable and picks up state from
Postgres. No process may assume another is running.

| Process | Count | Job |
|---|---|---|
| `poller` | 1 (asyncio, N tasks) | Poll `reels_tray` per worker account, diff, enqueue |
| `fetcher` | 1–2 | Drain queue, batch `reels_media`, write `stories` |
| `analyzer` | 2–4 | OCR + cheap model + smart model |
| `bizcheck` | 1 | Vendor / service-fit / geo / community checks |
| `notifier` | 1 | Slack delivery with retry |
| `follower` | 1 | Bootstrap follows at safe rate |
| `warden` | 1 | Account health, alerts, recovery orchestration |

---

## 7. Implementation detail per process

### 7.1 `poller`

One asyncio task per `active` worker account. Loop:

1. Sleep until `now + 120s ± jitter(0..30s)`, offset initially by `phase_offset_sec`.
2. Call `reels_tray()` via the transport.
3. For each entry in `tray`:
   - Skip entries whose `id` is not all digits (highlights, `highlight:1234...`).
   - Read `latest_reel_media` (unix seconds). If absent, skip.
   - Compare against `targets.last_reel_media_ts` for that `user_id`.
   - If greater: push `user_id` to Redis list `q:fetch`, update the watermark.
   - If the entry has a prefetched `items` array, push the full entry to `q:fetch_direct`
     instead — it already contains story ids and media types, so the fetcher can skip the
     `reels_media` call entirely.
4. Update `worker_accounts.last_poll_at`, increment `daily_action_counters.requests_done`.

Transport call:

```python
tray = client.private_request("feed/reels_tray/", data={
    "reason": "pull_to_refresh",
    "timezone_offset": "0",
    "tray_session_id": client.generate_uuid(),
    "request_id": client.generate_uuid(),
    "_uuid": client.uuid,
    "page_size": "50",
})
```

Use `reason="cold_start"` on the first call after a login, `pull_to_refresh` thereafter.
The exact payload varies with the Instagram app version instagrapi emulates — verify against
the installed instagrapi source and against `ReelsTrayFeed` in `dilame/instagram-private-api`,
which has the response shape typed out.

**Phase offset:** workers sharing a `shard_id` must be evenly spread across the poll
interval. With 3 workers on a 120s interval, offsets are 0 / 40 / 80. Compute at startup
from each worker's rank within its shard.

**Never call `media/seen/`.** Marking stories as viewed puts our worker accounts into the
target's viewer list, which gets us reported. Read-only always.

### 7.2 Tray truncation — resolve this before anything else

We do not know whether `reels_tray` returns every following with an active story, or a
ranked subset. **The entire architecture depends on the answer.**

Build `scripts/probe_tray.py` in Phase 1: given one worker account, dump the full tray
response, count entries, count how many have `latest_reel_media` within the last 24h, and
write the raw JSON to disk for fixture creation. Run it repeatedly across a day.

If the tray is truncated, `ReelsTrayFeed` exposes `page_size` and cursor pagination — the
poller must then paginate, which raises per-shard request cost and may force smaller shards
(more worker accounts). Design the poller so pagination can be switched on via config
without a rewrite.

### 7.3 `fetcher`

Drain `q:fetch`, accumulate up to 50 user ids or 5 seconds, whichever first:

```python
resp = client.private_request("feed/reels_media/", data={
    "user_ids": [str(u) for u in batch],
    "source": "feed_timeline",
    "_uuid": client.uuid,
})
```

For each item in each returned reel:

- `INSERT ... ON CONFLICT (story_id) DO NOTHING` into `stories`. A conflict means we have
  already seen it — that is the dedup mechanism, and it must be the only one.
- `media_type == 2` → set `pipeline_state = 'skipped_video'`, stop.
- `media_type == 1` → download the largest `image_versions2` candidate to a temp file, push
  `story_id` + temp path to `q:analyze`.

Media URLs are short-lived. If a download 403s, retry once immediately, then mark the story
`failed`; do not re-queue indefinitely.

### 7.4 `analyzer`

Per story:

1. OCR the image (Tesseract locally, or the cheap model's vision if it reads text well
   enough — measure both during the pilot and keep the cheaper one).
2. Cheap model. System prompt demands **JSON only**, no prose, no markdown fences. Required
   fields:
   `score` (0–10), `explicit_purchase_intent` (bool), `seeking_contractor` (bool),
   `allowed_category` (bool), `is_spam` (bool), `is_offering_services` (bool),
   `asking_for_free` (bool), `complaint_only` (bool), `service_category` (string|null),
   `geography` (string|null), `email_visible` (string|null).
3. Routing on `score`:
   - `0–4` → reject, write result, stop.
   - `5–6` → smart model. It returns `confirmed` (bool), `final_score` (0–10),
     `service_category`, `intent_type`, `explanation` (max 2 sentences).
   - `7+` → skip the smart model, `final_score = cheap score`, go to business checks.
4. **Delete the temp file in a `finally` block.** No exception path may leave media on disk.
5. Write `story_analysis`, advance `pipeline_state`.

Validate model output against a Pydantic schema. On parse failure, retry once with a
"return valid JSON only" nudge, then mark `failed` — never guess at malformed output.

### 7.5 `bizcheck`

Connects to the Recommend.us database. **The exact schema and access method are not yet
known.** Put every query behind a `VendorRepository` interface with a stub implementation
that returns fixture data, and a `# TODO: real schema pending` marker. The pipeline must run
end to end against the stub.

Checks, in order, short-circuiting on first failure:

1. A matching vendor exists for `service_category`.
2. Vendor Service Fit ≥ 70 (make the threshold a config value; the spec says "roughly 70–75").
3. Lead community ≠ vendor community.
4. Geography is serviceable.
5. Internal forwarding rules permit this lead.

Write `business_checks`. `final_status = 'approved'` only if `final_score >= 7` **and** every
check passed. AI score alone is never sufficient.

### 7.6 `notifier`

Only `final_status = 'approved'` reaches Slack. Message fields: username, Instagram URL,
service category, final score, AI explanation, story publish time, business check summary.

Insert into `slack_deliveries` with status `pending` **before** calling Slack. The primary
key on `story_id` prevents duplicates even if the process crashes between send and commit.
Exponential backoff on failure, 5 attempts, then `failed` plus an alert.

### 7.7 `follower` — the long pole

Reads `target_follows` rows in state `queued` for each `active` worker account.

- Hard cap from config, default 150 follows per account per day, enforced through
  `daily_action_counters` so a restart cannot reset it.
- Randomised gap between follows: 4–12 minutes, no fixed cadence.
- Only operate during a configurable local-time window (e.g. 09:00–23:00 in the account's
  claimed timezone). Humans sleep.
- `user_follow()` returns `True` only on a new follow or a new outgoing request; `False`
  means already following or already pending. Handle both without treating `False` as an
  error.
- Private targets go to state `requested`. A separate slow sweep checks
  `user_friendship_v1()` to promote `requested` → `following` when approved.
- On `feedback_required`: stop that account's follows for 24h, log an `account_event`, keep
  polling (reading is much safer than writing).
- Fully resumable. Killing the process mid-run must lose nothing.

Expose progress: follows completed, follows remaining, projected completion date per shard.
The project owner will be asked about this number weekly.

### 7.8 `warden`

- Marks an account `challenged` on `challenge_required` and stops all its activity. **Never
  attempt to auto-solve a challenge** — that reliably converts a recoverable account into a
  banned one. Alert a human.
- On `login_required`: attempt one re-login using saved credentials with the *same* device
  settings and *same* proxy. Two consecutive failures → `challenged`.
- On `feedback_required` / `please_wait_a_few_minutes`: back off 5–30 minutes with jitter,
  keep the account active.
- When an account is lost, promote a `reserve` account into that shard and enqueue its
  `target_follows` rows so it rebuilds the follow graph from the database.
- Alert when: any shard drops below 2 active workers, poller lag exceeds 5 minutes, queue
  depth exceeds threshold, or projected monthly spend exceeds budget.

---

## 8. Session and identity hygiene

These are correctness requirements, not suggestions. Violating any of them burns accounts.

- `device_settings` generated once per account, stored in the DB, **never regenerated**.
- Proxy bound to an account permanently. Never rotate, never share between accounts.
- Persist sessions with `dump_settings` / `load_settings`. Log in only when the saved session
  is rejected. Repeated logins are the single strongest ban signal.
- All jitter must be real randomness, not a fixed offset.
- One account = one asyncio task = one proxy = one identity, for the account's whole life.

---

## 9. Build order

Do not skip ahead. Each phase must pass its acceptance criteria before the next starts.

**Phase 1 — Foundation**
Repo layout, config, Alembic schema, transport interface, fixture transport, `probe_tray.py`,
CSV importer for the 57k accounts (normalise `user_id`, drop duplicates, assign `shard_id`).
*Accept:* full schema migrates cleanly; CSV imports with a count report; fixture transport
returns parsed tray data in tests.

**Phase 2 — Poller and dedup**
Poller with phase offsets, watermark diffing, `q:fetch`. Fetcher with batching and
`ON CONFLICT` dedup. Video skip.
*Accept:* replaying the same fixture tray twice enqueues zero work the second time; videos
never leave `skipped_video`.

**Phase 3 — AI pipeline**
OCR, cheap model, smart model, score routing, temp file cleanup, Pydantic validation.
*Accept:* a fixture photo flows to `analyzed`; scores 5–6 demonstrably hit the smart model
and 7+ demonstrably do not; no temp files remain after a forced mid-pipeline exception.

**Phase 4 — Business checks and Slack**
`VendorRepository` stub, check chain, notifier with idempotency.
*Accept:* an approved lead posts once; running the notifier twice posts nothing the second
time.

**Phase 5 — Follower and warden**
Rate-limited follower, resumability, health monitoring, alerts.
*Accept:* killing the follower mid-run and restarting loses no progress and does not exceed
the daily cap; a simulated `challenge_required` moves the account out of rotation and
promotes a reserve.

**Phase 6 — Live cutover**
Switch `IG_TRANSPORT=live` with 1–2 real accounts and 200–500 targets. Measure everything the
pilot section of the original brief asks for: stories per day, photo/video ratio, cost per
analysed photo, detection latency, classification accuracy, duplicate rate, API error rate.
*Accept:* 3–7 days of stable operation with real measured numbers.

---

## 10. Observability

Structured JSON logs, plus a `/metrics` endpoint or a simple stats table. Track at minimum:

- Poll latency and success rate per worker account
- Tray entry count per poll (this is the truncation canary — a sudden drop means trouble)
- Queue depths
- Stories discovered / photos analysed / videos skipped per hour
- Cheap-model and smart-model call counts and estimated spend, daily
- Leads approved and Slack deliveries
- Detection latency: `discovered_at − taken_at`, p50 and p95

Detection latency is the number that proves or disproves the whole design. Instrument it from
day one.

---

## 11. Explicit do-nots

- Do not call `media/seen/` or any other write endpoint against target accounts.
- Do not store media beyond the analysis step.
- Do not hardcode credentials, proxies, or API keys anywhere.
- Do not rotate proxies or regenerate device settings for an existing account.
- Do not auto-solve challenges.
- Do not import `instagrapi` outside the transport layer.
- Do not poll targets individually — if you find yourself writing a loop over 57,000
  `user_stories(user_id)` calls, the design has been misunderstood; re-read section 1.
- Do not add a distributed task framework.
- Do not build the Harvard followers harvester.

---

## 12. Open questions to surface, not guess

Flag these in the README rather than inventing answers:

1. Does `reels_tray` truncate? Blocks final shard sizing and worker account count.
2. Recommend.us database schema and access method.
3. Is one "story" one media item or the whole per-account sequence? Affects `stories` row
   granularity and Slack message shape.
4. Exact Service Fit threshold (70 vs 75).
5. Final list of allowed service categories.
