# Instagram Stories Monitor  

Watches ~57,000 Instagram accounts for newly posted stories, detects them within ~1 minute,
runs photo stories through a two-stage AI classifier, applies business rules against an
internal vendor database, and pushes qualified leads to Slack.

**The core architectural trick:** we do not poll 57,000 accounts individually. ~20 worker
Instagram accounts collectively follow all 57,000 targets. Each worker polls
`feed/reels_tray/` — a single request returning the story tray for *all* of that worker's
followings. 57,000 monitored objects collapse into ~20 polled objects.

Videos are ignored. Only photo stories reach the AI pipeline. Media is never stored
permanently.

> This uses Instagram's private mobile API via `instagrapi`, which is against Instagram's
> Terms of Service. That decision was made by the project owner.

---

## Quick start (fixture mode — no real credentials needed)

The entire pipeline runs end to end without a single real Instagram account.

```bash
uv venv --python 3.11
uv pip install -e ".[dev]"

cp .env.example .env
# Generate a Fernet key for SECRET_KEY:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

createdb stories_monitor
alembic upgrade head

pytest                       # full suite against fixtures
python -m stories_monitor.cli --help
```

With `IG_TRANSPORT=fixture` (the default) no network calls are made and Slack writes to
stdout.

---

## Admin dashboard

A Next.js dashboard lives in `web/` and reads the same Supabase database.
Sessions, leads, per-target activity, funnel and cost. See `web/README.md`.

The two deploy independently and must stay that way:

| | Deploys from | Trigger |
|---|---|---|
| Scraper (Python) | repo root, `Dockerfile` | changes under `src/ scripts/ migrations/ fixtures/` |
| Dashboard (Next.js) | `web/`, Vercel Root Directory = `web` | any push |

`web/` is in `.dockerignore` and absent from every `COPY` in the `Dockerfile`, so
it never enters the Python image. `railway.toml` sets `watchPatterns`, so editing
the dashboard does **not** rebuild the scraper - without that filter every
dashboard commit would restart collection mid-cycle.

---

## Processes

Seven long-running processes. Each is independently restartable and picks up state from
Postgres. No process assumes another is running.

| Process | Count | Job | Command |
|---|---|---|---|
| `poller` | 1 (asyncio, N tasks) | Poll `reels_tray`, diff, enqueue | `stories poller` |
| `fetcher` | 1–2 | Drain queue, batch `reels_media`, write `stories` | `stories fetcher` |
| `analyzer` | 2–4 | OCR + cheap model + smart model | `stories analyzer` |
| `bizcheck` | 1 | Vendor / service-fit / geo / community checks | `stories bizcheck` |
| `notifier` | 1 | Slack delivery with retry | `stories notifier` |
| `follower` | 1 | Bootstrap follows at safe rate | `stories follower` |
| `warden` | 1 | Account health, alerts, recovery | `stories warden` |

Operational commands: `import-targets`, `probe-tray`, `health`, `progress`, `queues`,
`latency`, `gen-key`, `config`.

---

## Architecture

```
reels_tray poll (per worker)
        │  diff vs targets.last_reel_media_ts  (the watermark)
        ▼
    q:fetch ──────────────► fetcher ── ON CONFLICT (story_id) DO NOTHING ── the ONLY dedup
    q:fetch_direct (prefetched items skip the reels_media call)
        │  media_type 2 → skipped_video (videos never reach AI)
        ▼
    q:analyze ────────────► analyzer  OCR → cheap model → (5–6) smart model → final_score
        │  temp media deleted in a finally block, always
        ▼
    q:bizcheck ───────────► bizcheck  vendor → fit ≥ threshold → community → geo → rules
        │  approved only if final_score ≥ 7 AND every check passed
        ▼
    q:notify ─────────────► notifier  slack_deliveries PK on story_id = never posted twice
```

### Detection latency

Detection latency (`discovered_at − taken_at`, p50/p95) is the number that proves or
disproves the whole design. It is instrumented from day one — `stories latency`.

---

## Safety invariants

These are correctness requirements, not suggestions. Violating any of them burns accounts.

- **Never call `media/seen/`** or any write endpoint against targets. Marking stories viewed
  puts our workers in the target's viewer list and gets us reported. Read-only always.
- **Never auto-solve a challenge.** It reliably converts a recoverable account into a banned
  one. `LiveTransport` overrides instagrapi's `challenge_resolve` to raise instead, and the
  warden alerts a human.
- **`device_settings` generated once per account, never regenerated.**
- **Proxy bound to an account permanently.** Never rotate, never share.
- Persist sessions; log in only when the saved session is rejected. Repeated logins are the
  single strongest ban signal.
- All jitter is real randomness, not a fixed offset.
- One account = one asyncio task = one proxy = one identity, for the account's whole life.
- **`instagrapi` is imported only inside `stories_monitor/transport/live.py`.**
- No permanent media storage — temp files only, deleted in a `finally` block.
- Secrets come from environment variables only. Worker passwords are encrypted at rest with
  Fernet. Logs redact passwords, sessions, tokens, and proxy URLs.

---

## Transport layer

Every Instagram call goes through the `InstagramTransport` interface with two
implementations:

- `LiveTransport` — real `instagrapi` calls. Translates every instagrapi exception into
  transport-level exceptions so instagrapi types never escape the boundary.
- `FixtureTransport` — replays JSON from `fixtures/`. Covers normal tray, empty tray,
  highlight entries, prefetched items, private account, deleted account, and the
  `feedback_required` / `challenge_required` / `login_required` errors.

See `fixtures/README.md` for scenario names and conventions.

### Upstream references — track both

Instagram changes its private API without notice, and these two repositories are how we
find out. Watch releases on both; when a call starts failing, check them before debugging
our code.

- **[subzeroid/instagrapi](https://github.com/subzeroid/instagrapi)** — the client we
  depend on. Exception classes get renamed and moved between releases, which is why
  `live.py` resolves every name defensively via `getattr` rather than importing it
  directly. Worth reading the source before trusting a method: `instagrapi` has **no
  `reels_tray` method**, which is why we call `private_request("feed/reels_tray/")`
  ourselves.
- **[dilame/instagram-private-api](https://github.com/dilame/instagram-private-api)** —
  a TypeScript client with the request payloads and response shapes typed out. It is the
  reference the SPEC names in 7.1, and it is the better source for *what the app actually
  sends*, since it models each feed explicitly.

This is not decoration. Checking `ReelsMediaFeed` in the second repo found a real bug:
our `reels_media` call was sending 3 fields where the app sends 7. The missing
`supported_capabilities_new` tells Instagram which media formats the client can decode,
and `_uid` / `device_id` make the request look like the app rather than a bare API call.

When adding or changing an endpoint, compare against **both**: instagrapi for how to call
it, dilame for what the payload must contain.

---

## AI provider

The classifier sits behind an `AIClient` protocol with three implementations:
`AnthropicAIClient`, `GeminiAIClient`, and a deterministic `FakeAIClient`. Switching
provider is a config change:

```bash
AI_PROVIDER=gemini
GEMINI_API_KEY=...
CHEAP_MODEL=gemini-flash-lite-latest   # stable aliases; dated 2.5/2.0 ids 404
SMART_MODEL=gemini-pro-latest
```

An empty key for the active provider falls back to `FakeAIClient`, so the pipeline still
runs end to end. Note the AI provider is independent of `IG_TRANSPORT` — a real key with
`IG_TRANSPORT=fixture` exercises the real classifier against fixture stories, which is the
cheapest way to tune prompts.

`OCR_ENGINE=vision` routes OCR through the cheap model instead of a local Tesseract binary.
SPEC 7.4 asks for both to be measured during the pilot; keep the cheaper one.

Live-test results — which accounts worked, proxy findings, session scoping:
**[FINDINGS.md](FINDINGS.md)** (Russian).

---

## Open questions — flagged, not guessed

These are unresolved. The code accommodates both answers rather than inventing one.

1. **Does `reels_tray` truncate?** *Blocks final shard sizing and worker account count.*
   We do not know whether the tray returns every following with an active story or a ranked
   subset. **The entire architecture depends on the answer.** `scripts/probe_tray.py` exists
   to settle it — run it repeatedly across a day. If the tray truncates, the poller must
   paginate; that path is already implemented and switchable via
   `TRAY_PAGINATION_ENABLED=true` without a rewrite, but it raises per-shard request cost
   and may force smaller shards (more worker accounts).
2. **Recommend.us database schema and access method.** Everything sits behind
   `VendorRepository` with a stub implementation; the pipeline runs end to end against
   fixture vendor data. Marked `# TODO: real schema pending`.
3. **Is one "story" one media item or the whole per-account sequence?** We currently store
   one row per media item. This affects `stories` row granularity and the Slack message
   shape — if a sequence is the unit, the notifier should group rather than post per item.
4. **Exact Service Fit threshold (70 vs 75).** Config value `SERVICE_FIT_THRESHOLD`,
   currently 70.
5. **Final list of allowed service categories.** The classifier returns a free-form
   `service_category`; no allowlist is enforced yet.

---

## Build status against the spec phases

- **Phase 1 — Foundation:** repo layout, config, Alembic schema, transport interface,
  fixture transport, `probe_tray.py`, CSV importer.
- **Phase 2 — Poller and dedup:** phase offsets, watermark diffing, `q:fetch`, batched
  fetcher, `ON CONFLICT` dedup, video skip.
- **Phase 3 — AI pipeline:** OCR, cheap model, smart model, score routing, temp cleanup,
  Pydantic validation.
- **Phase 4 — Business checks and Slack:** `VendorRepository` stub, check chain, idempotent
  notifier.
- **Phase 5 — Follower and warden:** rate-limited resumable follower, health monitoring,
  reserve promotion, alerts.
- **Phase 6 — Live cutover:** not started. Requires real credentials and the `probe_tray`
  answer to question 1.

## Cost

Budget is $650/month. `reels_tray` is mandatory, not optional — polling targets individually
would cost orders of magnitude more. The warden alerts when projected monthly spend exceeds
budget.
