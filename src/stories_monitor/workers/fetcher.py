"""The fetcher (SPEC 7.3).

Drains q:fetch, batches user ids into `reels_media` calls, writes `stories` rows.

`INSERT ... ON CONFLICT (story_id) DO NOTHING` is the ONLY dedup mechanism in the
system (SPEC 7.3). A conflict means we have already seen that story.

Videos are ignored - only photos reach the AI pipeline (SPEC section 1).
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..config import Q_ANALYZE, Q_FETCH, Q_FETCH_DIRECT, get_settings
from ..db.models import Story, Target, WorkerAccount
from ..db.session import session_scope
from ..logging_setup import get_logger
from ..metrics import record_metric
from ..queue import get_queue
from ..transport import InstagramTransport, StoryItem, TransportError, get_transport

log = get_logger(__name__)


def _to_dt(ts: int | None) -> dt.datetime | None:
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(int(ts), tz=dt.UTC)


def _story_item_from_payload(user_id: int, raw: dict) -> StoryItem | None:
    """Build a StoryItem from a prefetched tray `items` entry (SPEC 7.1)."""
    story_id = str(raw.get("pk") or raw.get("id") or "").split("_")[0]
    if not story_id:
        return None
    taken_at = raw.get("taken_at")
    if taken_at is None:
        return None
    candidates = (raw.get("image_versions2") or {}).get("candidates") or []
    return StoryItem(
        story_id=story_id,
        user_id=user_id,
        taken_at=int(taken_at),
        media_type=int(raw.get("media_type") or 1),
        expiring_at=raw.get("expiring_at"),
        image_versions=candidates,
        raw=raw,
    )


class Fetcher:
    """Batches reels_media calls and writes deduped story rows."""

    def __init__(self, transport: InstagramTransport | None = None):
        self.settings = get_settings()
        self.fetch_queue = get_queue(Q_FETCH)
        self.direct_queue = get_queue(Q_FETCH_DIRECT)
        self.analyze_queue = get_queue(Q_ANALYZE)
        self._transport = transport
        self._tmp_dir = self.settings.media_tmp_dir
        os.makedirs(self._tmp_dir, exist_ok=True)

    @property
    def transport(self) -> InstagramTransport:
        """Any active worker's session can serve reels_media; reads are shard-agnostic."""
        if self._transport is None:
            with session_scope() as session:
                account = session.scalars(
                    select(WorkerAccount).where(WorkerAccount.status == "active").limit(1)
                ).first()
                if account is None:
                    raise RuntimeError("no active worker account available for fetching")
                username = account.username
                device_settings = dict(account.device_settings or {})
                proxy_url = account.proxy_url
                session_json = dict(account.session_json) if account.session_json else None

            self._transport = get_transport(
                username=username,
                device_settings=device_settings,
                proxy_url=proxy_url,
            )
            if session_json:
                self._transport.load_session(session_json)
        return self._transport

    # --- batching ---

    def run_once(self) -> int:
        """Process one batch from each queue. Returns stories written."""
        written = self._drain_direct()
        written += self._drain_fetch()
        return written

    def _drain_direct(self) -> int:
        """q:fetch_direct entries already carry story items - no reels_media call needed."""
        payloads = self.direct_queue.pop_batch(
            self.settings.fetch_batch_size, self.settings.fetch_batch_timeout_sec
        )
        if not payloads:
            return 0

        written = 0
        for payload in payloads:
            user_id = int(payload["user_id"])
            items = [
                item
                for raw in payload.get("items", [])
                if (item := _story_item_from_payload(user_id, raw)) is not None
            ]
            if items:
                written += self._persist(user_id, items, payload.get("detected_at"))
        log.info("direct_batch_processed", users=len(payloads), stories_written=written)
        return written

    def _drain_fetch(self) -> int:
        """Accumulate up to 50 user ids or 5 seconds, whichever first (SPEC 7.3)."""
        payloads = self.fetch_queue.pop_batch(
            self.settings.fetch_batch_size, self.settings.fetch_batch_timeout_sec
        )
        if not payloads:
            return 0

        detected_at = {int(p["user_id"]): p.get("detected_at") for p in payloads}
        user_ids = list(detected_at)

        try:
            reels = self.transport.reels_media(user_ids)
        except TransportError as exc:
            log.warning("reels_media_failed", user_count=len(user_ids), error=str(exc))
            return 0

        written = 0
        for user_id, items in reels.items():
            written += self._persist(int(user_id), items, detected_at.get(int(user_id)))

        record_metric("fetch_batch_size", len(user_ids), None)
        log.info("fetch_batch_processed", users=len(user_ids), stories_written=written)
        return written

    # --- persistence + dedup ---

    def _persist(self, user_id: int, items: list[StoryItem], detected_at: str | None) -> int:
        """Insert stories with ON CONFLICT DO NOTHING - the only dedup mechanism."""
        if not items:
            return 0

        new_story_ids: list[str] = []
        with session_scope() as session:
            # Never invent a target row; the tray can contain accounts we do not monitor.
            if session.get(Target, user_id) is None:
                log.debug("skipping_unmonitored_target", user_id=user_id)
                return 0

            for item in items:
                taken_at = _to_dt(item.taken_at)
                if taken_at is None:
                    continue

                state = "skipped_video" if item.media_type == 2 else "new"
                stmt = (
                    pg_insert(Story)
                    .values(
                        story_id=item.story_id,
                        target_user_id=user_id,
                        taken_at=taken_at,
                        expiring_at=_to_dt(item.expiring_at),
                        media_type=item.media_type,
                        pipeline_state=state,
                    )
                    .on_conflict_do_nothing(index_elements=["story_id"])
                    .returning(Story.story_id)
                )
                inserted = session.execute(stmt).scalar_one_or_none()
                if inserted is None:
                    continue  # Already seen - this is the dedup path.

                if item.media_type == 2:
                    # Videos are ignored: they never leave skipped_video (SPEC section 1).
                    record_metric("videos_skipped", 1, None)
                    continue

                new_story_ids.append(item.story_id)
                record_metric("stories_discovered", 1, None)
                self._record_detection_latency(taken_at)

        # Download outside the DB transaction - network I/O must not hold a connection.
        downloaded = 0
        by_id = {i.story_id: i for i in items}
        for story_id in new_story_ids:
            if self._download_and_enqueue(by_id[story_id]):
                downloaded += 1
        return downloaded

    def _record_detection_latency(self, taken_at: dt.datetime) -> None:
        """discovered_at - taken_at. The number that proves the design (SPEC section 10)."""
        latency = (dt.datetime.now(dt.UTC) - taken_at).total_seconds()
        record_metric("detection_latency_sec", latency, None)

    def _download_and_enqueue(self, item: StoryItem) -> bool:
        """Download the largest image candidate to a temp file, push to q:analyze.

        Media URLs are short-lived. Retry once on failure, then mark the story failed -
        do not re-queue indefinitely (SPEC 7.3). The analyzer owns deleting the file.
        """
        url = item.best_image_url()
        if not url:
            _set_state(item.story_id, "failed")
            log.warning("no_image_candidate", story_id=item.story_id)
            return False

        suffix = ".jpg"
        dest = os.path.join(
            self._tmp_dir, f"{item.story_id}_{uuid.uuid4().hex[:8]}{suffix}"
        )

        for attempt in (1, 2):
            try:
                self.transport.download_media(url, dest)
                self.analyze_queue.push(
                    {
                        "story_id": item.story_id,
                        "user_id": item.user_id,
                        "media_path": dest,
                        "taken_at": item.taken_at,
                    }
                )
                return True
            except Exception as exc:  # noqa: BLE001 - any download failure is retryable once
                if attempt == 1:
                    log.info("media_download_retry", story_id=item.story_id, error=str(exc))
                    continue
                log.warning("media_download_failed", story_id=item.story_id, error=str(exc))
                _cleanup(dest)
                _set_state(item.story_id, "failed")
                return False
        return False

    def run(self) -> None:
        log.info("fetcher_starting")
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                log.info("fetcher_stopping")
                break
            except Exception as exc:  # noqa: BLE001 - loop must never die
                log.exception("fetcher_error", error=str(exc))


def _cleanup(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("temp_cleanup_failed", path=path, error=str(exc))


def _set_state(story_id: str, state: str) -> None:
    from sqlalchemy import update

    with session_scope() as session:
        session.execute(
            update(Story).where(Story.story_id == story_id).values(pipeline_state=state)
        )


def run_fetcher() -> None:
    Fetcher().run()


def make_temp_path(story_id: str) -> str:
    """Temp path helper shared with tests."""
    return os.path.join(tempfile.gettempdir(), f"story_{story_id}_{uuid.uuid4().hex[:8]}.jpg")
