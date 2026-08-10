"""SPEC Phase 2 acceptance criteria (SPEC section 9).

*Accept:* replaying the same fixture tray twice enqueues zero work the second
time; videos never leave `skipped_video`.

The watermark diff in `TrayDiffer` and the `ON CONFLICT (story_id) DO NOTHING`
insert in `Fetcher` are the only two dedup mechanisms in the system (SPEC 7.3),
so they carry most of this module.
"""

from __future__ import annotations

import sqlalchemy as sa

from stories_monitor.config import Q_ANALYZE, Q_FETCH, Q_FETCH_DIRECT
from stories_monitor.db.models import Story, Target
from stories_monitor.transport.base import StoryItem, TrayEntry, TrayResponse
from stories_monitor.workers.fetcher import Fetcher
from stories_monitor.workers.poller import TrayDiffer, compute_phase_offsets

# --- THE headline Phase 2 criterion -------------------------------------------


def test_replaying_the_same_tray_twice_enqueues_zero_work_the_second_time(
    db, fake_queues, fixture_targets, fixture_transport
):
    """SPEC Phase 2 acceptance criterion.

    The same tray, replayed, must produce no new work: every `latest_reel_media`
    is now equal to the stored watermark, and the diff is strictly-greater-than.
    """
    transport = fixture_transport(scenarios=["normal_tray", "normal_tray"])
    differ = TrayDiffer(worker_account_id=1)

    first = differ.diff_and_enqueue(transport.reels_tray(cold_start=True))
    assert first.new_stories_detected == 3, "first poll should detect all three targets"
    depth_after_first = fake_queues[Q_FETCH].depth()
    assert depth_after_first == 3

    second = differ.diff_and_enqueue(transport.reels_tray())

    assert second.entries_seen == 3, "the same tray was served again"
    assert second.user_entries == 3
    assert second.new_stories_detected == 0, "the replayed tray enqueued new work"
    assert second.direct_enqueued == 0
    assert fake_queues[Q_FETCH].depth() == depth_after_first, (
        "the second identical poll pushed extra items onto q:fetch"
    )
    assert fake_queues[Q_FETCH_DIRECT].depth() == 0


def test_watermarks_are_persisted_after_the_first_poll(
    db, fake_queues, fixture_targets, fixture_transport
):
    """The zero-work replay works because the watermark was written, not cached."""
    transport = fixture_transport(scenario="normal_tray")
    TrayDiffer(worker_account_id=1).diff_and_enqueue(transport.reels_tray())

    with db() as session:
        watermarks = dict(
            session.execute(
                sa.select(Target.user_id, Target.last_reel_media_ts)
            ).all()
        )
        seen = dict(
            session.execute(sa.select(Target.user_id, Target.last_seen_in_tray)).all()
        )

    assert watermarks == {
        1234567890: 1786095000,
        2345678901: 1786091400,
        3456789012: 1786096500,
    }
    assert all(v is not None for v in seen.values())

    # A brand-new TrayDiffer (i.e. a restarted poller) still sees zero new work.
    result = TrayDiffer(worker_account_id=1).diff_and_enqueue(transport.reels_tray())
    assert result.new_stories_detected == 0


# --- highlights ---------------------------------------------------------------


def test_highlight_entries_are_skipped_and_counted_never_enqueued(
    db, fake_queues, fixture_targets, fixture_transport
):
    """SPEC 7.1: skip entries whose `id` is not all digits (`highlight:1234...`)."""
    transport = fixture_transport(scenario="tray_with_highlights")
    tray = transport.reels_tray()

    highlight_ids = [e.id for e in tray.entries if e.id.startswith("highlight:")]
    assert highlight_ids, "the fixture must contain highlight entries"

    result = TrayDiffer(worker_account_id=1).diff_and_enqueue(tray)

    assert result.highlights_skipped == len(highlight_ids)
    assert result.entries_seen == result.user_entries + result.highlights_skipped
    assert result.user_entries == 3

    enqueued_ids = set()
    while (item := fake_queues[Q_FETCH].pop_blocking(timeout=0)) is not None:
        enqueued_ids.add(str(item["user_id"]))
    assert not any(i.startswith("highlight") for i in enqueued_ids)
    assert enqueued_ids <= {"1234567890", "2345678901", "3456789012"}


def test_tray_entry_is_user_entry_filters_non_numeric_ids():
    assert TrayEntry(id="1234567890").is_user_entry is True
    assert TrayEntry(id="highlight:17912345678901234").is_user_entry is False
    assert TrayEntry(id="highlight:17912345678901234").user_id is None


# --- prefetched items ---------------------------------------------------------


def test_entries_with_prefetched_items_go_to_fetch_direct_not_fetch(
    db, fake_queues, fixture_targets, fixture_transport
):
    """SPEC 7.1: a prefetched `items` array lets the fetcher skip reels_media."""
    transport = fixture_transport(scenario="tray_with_prefetched_items")
    tray = transport.reels_tray()

    with_items = {e.user_id for e in tray.entries if e.has_prefetched_items}
    without_items = {
        e.user_id for e in tray.entries if e.is_user_entry and not e.has_prefetched_items
    }
    assert with_items, "the fixture must contain prefetched entries"

    result = TrayDiffer(worker_account_id=1).diff_and_enqueue(tray)

    assert result.direct_enqueued == len(with_items)
    assert fake_queues[Q_FETCH_DIRECT].depth() == len(with_items)
    assert fake_queues[Q_FETCH].depth() == len(without_items)

    direct = fake_queues[Q_FETCH_DIRECT].pop_blocking(timeout=0)
    assert direct["user_id"] in with_items
    assert direct["items"], "the prefetched items must travel with the payload"
    assert "media_type" in direct["items"][0]


# --- watermark monotonicity ---------------------------------------------------


def _tray(user_id: int, latest: int) -> TrayResponse:
    return TrayResponse(
        entries=[TrayEntry(id=str(user_id), latest_reel_media=latest)],
        raw={},
    )


def test_watermark_only_advances_forward(db, fake_queues, make_target):
    """An older or equal `latest_reel_media` enqueues nothing and never rewinds."""
    make_target(1234567890, last_reel_media_ts=1786095000)
    differ = TrayDiffer(worker_account_id=1)

    older = differ.diff_and_enqueue(_tray(1234567890, 1786000000))
    assert older.new_stories_detected == 0
    equal = differ.diff_and_enqueue(_tray(1234567890, 1786095000))
    assert equal.new_stories_detected == 0
    assert fake_queues[Q_FETCH].depth() == 0

    with db() as session:
        assert session.get(Target, 1234567890).last_reel_media_ts == 1786095000

    newer = differ.diff_and_enqueue(_tray(1234567890, 1786095001))
    assert newer.new_stories_detected == 1
    assert fake_queues[Q_FETCH].depth() == 1
    with db() as session:
        assert session.get(Target, 1234567890).last_reel_media_ts == 1786095001


def test_entries_without_latest_reel_media_are_ignored(db, fake_queues, make_target):
    make_target(1234567890)
    tray = TrayResponse(entries=[TrayEntry(id="1234567890", latest_reel_media=None)])
    result = TrayDiffer(worker_account_id=1).diff_and_enqueue(tray)

    assert result.user_entries == 1
    assert result.new_stories_detected == 0
    assert fake_queues[Q_FETCH].depth() == 0


def test_targets_not_in_our_db_are_ignored_and_no_rows_are_invented(
    db, fake_queues, make_target
):
    """The tray covers everything a worker follows; we only care about our targets."""
    make_target(1234567890)
    tray = TrayResponse(
        entries=[
            TrayEntry(id="1234567890", latest_reel_media=1786095000),
            TrayEntry(id="9999999999", latest_reel_media=1786095000),  # not monitored
        ]
    )

    result = TrayDiffer(worker_account_id=1).diff_and_enqueue(tray)

    assert result.user_entries == 2
    assert result.new_stories_detected == 1, "only the monitored target counts"

    with db() as session:
        user_ids = set(session.execute(sa.select(Target.user_id)).scalars().all())
    assert user_ids == {1234567890}, "a target row was invented for an unmonitored user"

    queued = {fake_queues[Q_FETCH].pop_blocking(timeout=0)["user_id"]}
    assert queued == {1234567890}


# --- phase offsets ------------------------------------------------------------


def test_compute_phase_offsets_spreads_three_workers_over_120s():
    """SPEC 7.1: with 3 workers on a 120s interval, offsets are 0 / 40 / 80."""
    assert compute_phase_offsets([11, 12, 13], 120) == {11: 0, 12: 40, 13: 80}
    # Order-independent: rank comes from sorted worker id, not list order.
    assert compute_phase_offsets([13, 11, 12], 120) == {11: 0, 12: 40, 13: 80}


def test_compute_phase_offsets_empty_list_is_empty_dict():
    assert compute_phase_offsets([], 120) == {}


def test_compute_phase_offsets_other_shard_sizes():
    assert compute_phase_offsets([1], 120) == {1: 0}
    assert compute_phase_offsets([1, 2], 120) == {1: 0, 2: 60}
    assert compute_phase_offsets([1, 2, 3, 4], 120) == {1: 0, 2: 30, 3: 60, 4: 90}


def test_assign_phase_offsets_persists_per_shard(db, make_worker_account):
    from stories_monitor.db.models import WorkerAccount
    from stories_monitor.workers.poller import assign_phase_offsets

    shard_a = [make_worker_account(shard_id=1) for _ in range(3)]
    shard_b = [make_worker_account(shard_id=2) for _ in range(2)]
    make_worker_account(shard_id=1, status="reserve")  # reserves are not scheduled

    offsets = assign_phase_offsets()

    assert {offsets[w] for w in shard_a} == {0, 40, 80}
    assert {offsets[w] for w in shard_b} == {0, 60}

    with db() as session:
        stored = dict(
            session.execute(
                sa.select(WorkerAccount.id, WorkerAccount.phase_offset_sec)
            ).all()
        )
    for worker_id, offset in offsets.items():
        assert stored[worker_id] == offset


# --- fetcher: ON CONFLICT dedup ------------------------------------------------


def _photo(story_id: str, user_id: int, taken_at: int = 1786095000) -> StoryItem:
    return StoryItem(
        story_id=story_id,
        user_id=user_id,
        taken_at=taken_at,
        media_type=1,
        expiring_at=taken_at + 86400,
        image_versions=[
            {"width": 640, "height": 1138, "url": f"https://example/{story_id}_640.jpg"},
            {"width": 1080, "height": 1920, "url": f"https://example/{story_id}_1080.jpg"},
        ],
    )


def _video(story_id: str, user_id: int, taken_at: int = 1786091400) -> StoryItem:
    item = _photo(story_id, user_id, taken_at)
    item.media_type = 2
    return item


def test_fetcher_on_conflict_dedup_yields_one_row_and_no_second_enqueue(
    db, fake_queues, fixture_targets, fixture_transport, tmp_path, monkeypatch
):
    """SPEC 7.3: `ON CONFLICT (story_id) DO NOTHING` is the ONLY dedup mechanism."""
    monkeypatch.setenv("MEDIA_TMP_DIR", str(tmp_path))
    from stories_monitor.config import get_settings

    get_settings.cache_clear()

    fetcher = Fetcher(transport=fixture_transport())
    item = _photo("3111000000000000001", 1234567890)

    first = fetcher._persist(1234567890, [item], None)
    assert first == 1
    assert fake_queues[Q_ANALYZE].depth() == 1

    second = fetcher._persist(1234567890, [item], None)
    assert second == 0, "the same story_id was persisted twice"
    assert fake_queues[Q_ANALYZE].depth() == 1, "a duplicate story was re-enqueued"

    with db() as session:
        count = session.execute(
            sa.select(sa.func.count())
            .select_from(Story)
            .where(Story.story_id == "3111000000000000001")
        ).scalar()
    assert count == 1

    get_settings.cache_clear()


def test_fetcher_never_invents_a_target_row(
    db, fake_queues, fixture_transport, tmp_path, monkeypatch
):
    monkeypatch.setenv("MEDIA_TMP_DIR", str(tmp_path))
    from stories_monitor.config import get_settings

    get_settings.cache_clear()

    fetcher = Fetcher(transport=fixture_transport())
    written = fetcher._persist(9999999999, [_photo("s1", 9999999999)], None)

    assert written == 0
    with db() as session:
        assert session.execute(sa.select(sa.func.count()).select_from(Story)).scalar() == 0
        assert session.execute(sa.select(sa.func.count()).select_from(Target)).scalar() == 0

    get_settings.cache_clear()


# --- videos --------------------------------------------------------------------


def test_videos_never_leave_skipped_video_and_are_never_pushed_to_analyze(
    db, fake_queues, fixture_targets, fixture_transport, tmp_path, monkeypatch
):
    """SPEC Phase 2 acceptance criterion: videos never leave `skipped_video`.

    `media_type == 2` sets the state and stops - no download, no q:analyze push.
    """
    monkeypatch.setenv("MEDIA_TMP_DIR", str(tmp_path))
    from stories_monitor.config import get_settings

    get_settings.cache_clear()

    transport = fixture_transport()
    fetcher = Fetcher(transport=transport)

    video = _video("3222000000000000002", 2345678901)
    photo = _photo("3111000000000000001", 1234567890)

    assert fetcher._persist(2345678901, [video], None) == 0
    assert fetcher._persist(1234567890, [photo], None) == 1

    with db() as session:
        states = dict(
            session.execute(sa.select(Story.story_id, Story.pipeline_state)).all()
        )
    assert states["3222000000000000002"] == "skipped_video"
    assert states["3111000000000000001"] == "new"

    queued = []
    while (item := fake_queues[Q_ANALYZE].pop_blocking(timeout=0)) is not None:
        queued.append(item["story_id"])
    assert queued == ["3111000000000000001"], "a video reached q:analyze"

    # No media file was downloaded for the video.
    assert not any(p.name.startswith("3222") for p in tmp_path.iterdir())

    # Re-running never advances the video out of skipped_video either.
    fetcher._persist(2345678901, [video], None)
    with db() as session:
        assert session.get(Story, "3222000000000000002").pipeline_state == "skipped_video"

    get_settings.cache_clear()


def test_video_story_fixture_flows_through_the_fetcher_as_skipped_video(
    db, fake_queues, fixture_targets, fixture_transport, tmp_path, monkeypatch
):
    """End to end from the committed video fixture, not a hand-built StoryItem."""
    monkeypatch.setenv("MEDIA_TMP_DIR", str(tmp_path))
    from stories_monitor.config import get_settings

    get_settings.cache_clear()

    transport = fixture_transport(media_scenario="video_story")
    fetcher = Fetcher(transport=transport)

    fake_queues[Q_FETCH].push({"user_id": 2345678901, "latest_reel_media": 1786091400})
    written = fetcher._drain_fetch()

    assert written == 0
    with db() as session:
        rows = session.execute(sa.select(Story.story_id, Story.pipeline_state)).all()
    assert rows and all(state == "skipped_video" for _sid, state in rows)
    assert fake_queues[Q_ANALYZE].depth() == 0

    get_settings.cache_clear()


def test_fetcher_drains_prefetched_items_without_calling_reels_media(
    db, fake_queues, fixture_targets, fixture_transport, tmp_path, monkeypatch
):
    """q:fetch_direct payloads already carry story ids - reels_media is not called."""
    monkeypatch.setenv("MEDIA_TMP_DIR", str(tmp_path))
    from stories_monitor.config import get_settings

    get_settings.cache_clear()

    transport = fixture_transport(scenario="tray_with_prefetched_items")
    tray = transport.reels_tray()
    TrayDiffer(worker_account_id=1).diff_and_enqueue(tray)
    assert fake_queues[Q_FETCH_DIRECT].depth() > 0

    transport.calls.clear()
    fetcher = Fetcher(transport=transport)
    fetcher._drain_direct()

    called = [name for name, _ in transport.calls]
    assert "reels_media" not in called, "the fetcher called reels_media for prefetched items"

    with db() as session:
        states = dict(
            session.execute(sa.select(Story.story_id, Story.pipeline_state)).all()
        )
    assert states, "no stories were written from the prefetched items"
    # The prefetched fixture mixes a photo and a video for 1234567890.
    assert "skipped_video" in states.values()

    get_settings.cache_clear()
