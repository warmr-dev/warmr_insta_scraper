# Fixtures

Hand-written Instagram responses replayed by `FixtureTransport` (SPEC section 4).
No network I/O anywhere in fixture mode: `IG_TRANSPORT=fixture`.

Layout:

```
fixtures/
  reels_tray/*.json    # feed/reels_tray/ responses
  reels_media/*.json   # feed/reels_media/ responses
  vendors.json         # unrelated to the transport, do not edit here
```

`FIXTURES_DIR` (default `fixtures`) points at this directory.

## User ids

The same three ids are used across every tray and reels_media fixture, so an
end-to-end fixture run (poller -> fetcher -> analyzer) resolves consistently.

| user id      | username       | full name           |
| ------------ | -------------- | ------------------- |
| `1234567890` | `acme_dental`  | Acme Dental Clinic  |
| `2345678901` | `north_cafe`   | North Cafe          |
| `3456789012` | `vega_fitness` | Vega Fitness Studio |

Fixture "now" is `1786096800` (2026-08-06T10:00:00Z); every `latest_reel_media` and
`taken_at` is a small offset before it, and `expiring_at` is `taken_at + 86400`.

## Tray scenarios (`reels_tray/`)

| scenario                     | what it exercises                                                                  |
| ---------------------------- | ---------------------------------------------------------------------------------- |
| `normal_tray`                | Three user entries with fresh `latest_reel_media`. The default scenario.            |
| `empty_tray`                 | `"tray": []` - no work, no crash.                                                   |
| `tray_with_highlights`       | Two `highlight:1791...` / `highlight:1799...` entries interleaved with the three numeric user entries. The poller must skip non-digit ids (SPEC 7.1). |
| `tray_with_prefetched_items` | Two entries carry a populated `items` array, so the fetcher can skip `reels_media` and go straight to `q:fetch_direct` (SPEC 7.1). |
| `private_account`            | `user.is_private: true`, `media_count: 0`, `can_reply: false`.                     |
| `deleted_account`            | `1234567890` has vanished from the tray; only `3456789012` remains.                 |
| `error_feedback_required`    | Raises `FeedbackRequiredError`.                                                     |
| `error_challenge_required`   | Raises `ChallengeRequiredError`.                                                    |
| `error_login_required`       | Raises `LoginRequiredError`.                                                        |

Tray entry shape:

```json
{
  "id": "1234567890",
  "latest_reel_media": 1786095000,
  "seen": 0,
  "expiring_at": 1786181400,
  "media_count": 2,
  "user": { "pk": 1234567890, "username": "...", "full_name": "...", "profile_pic_url": "..." }
}
```

wrapped as `{"tray": [...], "status": "ok"}`.

## Media scenarios (`reels_media/`)

| scenario       | shape          | contents                                                             |
| -------------- | -------------- | -------------------------------------------------------------------- |
| `photo_story`  | `reels` dict   | Two `media_type: 1` items for `1234567890`. Candidates are 640x1138, 1080x1920, 320x569 - deliberately unsorted so `best_image_url()` must pick the 1080x1920 by area. |
| `video_story`  | `reels` dict   | One `media_type: 2` item for `2345678901` (`pipeline_state = skipped_video`). |
| `mixed_users`  | `reels_media` list | All three users, mixing photo and video items.                   |

Both response shapes are supported by the transport and are represented here on
purpose: `reels` (object keyed by user id string) and `reels_media` (list of reel
objects). The user id is read from the dict key, then `reel["id"]`, then
`reel["user"]["pk"]`.

Select the media fixture with `FixtureTransport(media_scenario="mixed_users")`.

## The `__error__` convention

A fixture whose top-level object contains `__error__` makes the corresponding
transport call raise instead of returning data:

```json
{ "__error__": "feedback_required", "status": "fail", "message": "..." }
```

| `__error__` value    | exception raised          |
| -------------------- | ------------------------- |
| `feedback_required`  | `FeedbackRequiredError`   |
| `challenge_required` | `ChallengeRequiredError`  |
| `login_required`     | `LoginRequiredError`      |
| `private_account`    | `PrivateAccountError`     |
| `user_not_found`     | `UserNotFoundError`       |

Any other value raises the base `TransportError`. `message` becomes the exception
text. This works for `reels_tray` and `reels_media` fixtures alike.

## Replaying a sequence

`reels_tray()` walks a queued scenario list, one entry per call, then repeats the
last one. Queue the same tray twice to assert the second poll yields zero new work:

```python
t = FixtureTransport(scenarios=["normal_tray", "normal_tray"])
t.reels_tray()  # first poll: three new watermarks
t.reels_tray()  # same tray: nothing newer than the watermark, no queue pushes
```

`queue_scenarios([...])` / `set_scenario(...)` reset the sequence at any point, and
`set_follow_result(False)` flips `user_follow()` for the follower tests.
