# Runbook

Operational procedures for the Instagram Stories Monitor. Read `README.md` first for
architecture and the safety invariants.

---

## Before live cutover — do these in order

1. **Answer the truncation question (SPEC 7.2).** This blocks shard sizing and worker
   account count, so it comes first.

   ```bash
   stories probe-tray --username <worker>
   ```

   Run it repeatedly across a full day. Each run appends to `probe_output/summary.csv` and
   writes the raw JSON for fixture creation. Compare `entries` against how many followings
   that account has with an active story. If `next_max_id` is ever present, or the entry
   count plateaus at a round number (50, 100), the tray is truncated — set
   `TRAY_PAGINATION_ENABLED=true` and re-do the shard-count math, because pagination raises
   per-shard request cost.

2. **Import targets and check the shard split.**

   ```bash
   stories import-targets targets.csv
   ```

   Verify per-shard counts sit under `MAX_FOLLOWS_PER_ACCOUNT` (7,000). Re-running is
   idempotent and will not clobber `last_reel_media_ts`.

   Note: rows with no resolvable `user_id` get a negative surrogate id and
   `status='unreachable'`. They need a resolver pass before they can be followed — do not
   create `target_follows` rows for negative ids.

3. **Seed worker accounts.** Each needs a permanently bound proxy and device settings
   generated exactly once. Never rotate either (SPEC section 8).

4. **Bootstrap the follow graph.** This is the long pole — 6–10 weeks at 150 follows/day.

   ```bash
   stories follower
   stories progress    # follows completed / remaining / projected completion per shard
   ```

   The project owner will ask for `stories progress` weekly.

---

## Normal operation

Start the seven processes (each is independently restartable):

```bash
stories poller     # 1 process, N asyncio tasks
stories fetcher    # 1-2
stories analyzer   # 2-4
stories bizcheck   # 1
stories notifier   # 1
stories follower   # 1
stories warden     # 1
```

Daily checks:

```bash
stories health     # active alerts
stories queues     # queue depths
stories latency    # detection latency p50/p95
stories progress   # follow bootstrap progress
```

---

## Incident response

### `challenge_required` on a worker account

The warden marks the account `challenged` and stops all its activity automatically, then
promotes a `reserve` into that shard and re-queues its follow graph from the database.

**Never attempt to auto-solve the challenge.** It reliably converts a recoverable account
into a permanently banned one. A human must resolve it in the Instagram app, on the same
device profile and proxy. Once cleared, set `status='active'` manually.

```sql
SELECT * FROM account_events WHERE event_type = 'challenge' ORDER BY occurred_at DESC;
```

### `login_required`

The warden attempts exactly one re-login with the *same* device settings and *same* proxy.
Two consecutive failures move the account to `challenged`. Repeated logins are the single
strongest ban signal, so do not loop re-logins manually.

### `feedback_required`

Follows for that account stop for 24h (persisted in `account_events`, so a restart honours
it). Polling continues — reading is much safer than writing. No action needed unless it
recurs, which suggests the follow rate is too high.

### Tray entry count drops suddenly

This is the truncation canary. Check the `tray_entry_count` metric per worker. A sudden
drop means either the account lost followings (check `follows_count`) or Instagram changed
tray behaviour — re-run `probe-tray` before trusting detection coverage.

### Detection latency climbing

```bash
stories latency
```

p95 well above ~120s means the poller is falling behind. Check: poller lag alerts, queue
depths, whether any shard dropped below 2 active workers, and whether phase offsets are
still evenly spread (they are recomputed at poller startup).

### Stories missing entirely

A missed poll window loses the story permanently — story lifetime is 24h and there is no
backfill. Check poller uptime first, then whether the target is still in
`target_follows.state = 'following'`.

---

## Cost control

Budget is $650/month. The warden alerts when projected monthly spend exceeds it.

Levers, cheapest first:
- Raise `SMART_MODEL_SCORE_MIN` so fewer stories reach the smart model.
- Confirm videos are being skipped (`videos_skipped` metric) — they must never reach AI.
- Switch `OCR_ENGINE` between `tesseract` and `vision` and compare measured cost per
  analysed photo. SPEC 7.4 says measure both during the pilot and keep the cheaper one.

Never lower the poll interval to save money — polling is a single `reels_tray` call per
worker and is not the dominant cost. Detection latency is.

---

## Pilot measurements (SPEC Phase 6)

Collect over 3–7 days of stable operation with 1–2 real accounts and 200–500 targets:

- stories per day
- photo/video ratio
- cost per analysed photo
- detection latency p50/p95
- classification accuracy (manual review sample)
- duplicate rate (should be zero — dedup is `ON CONFLICT` on `story_id`)
- API error rate per worker account

---

## Database quick queries

```sql
-- pipeline funnel
SELECT pipeline_state, count(*) FROM stories GROUP BY 1 ORDER BY 2 DESC;

-- detection latency
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(epoch FROM discovered_at - taken_at)) AS p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY EXTRACT(epoch FROM discovered_at - taken_at)) AS p95
FROM stories;

-- worker fleet health
SELECT shard_id, status, count(*) FROM worker_accounts GROUP BY 1,2 ORDER BY 1;

-- today's rate-limit ledger
SELECT * FROM daily_action_counters WHERE day = CURRENT_DATE;

-- leads delivered
SELECT status, count(*) FROM slack_deliveries GROUP BY 1;
```
