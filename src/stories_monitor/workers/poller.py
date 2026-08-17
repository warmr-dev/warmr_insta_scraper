"""The poller (SPEC 7.1).

The core architectural trick: we do not poll 57,000 accounts individually. Each worker
account polls `feed/reels_tray/` once - a single request that returns the story tray for
ALL of that worker's followings. 57,000 monitored objects collapse into ~20 polled objects.

One asyncio task per active worker account. One account = one task = one proxy = one
identity, for the account's whole life (SPEC section 8).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import random
import time
from dataclasses import dataclass

from sqlalchemy import select, update

from ..config import Q_FETCH, Q_FETCH_DIRECT, get_settings
from ..crypto import SecretBox
from ..db.models import Target, WorkerAccount
from ..db.session import session_scope
from ..logging_setup import get_logger
from ..metrics import record_metric
from ..queue import get_queue
from ..transport import (
    ChallengeRequiredError,
    FeedbackRequiredError,
    InstagramTransport,
    LoginRequiredError,
    PleaseWaitError,
    ProxyBlockedError,
    RateLimitedError,
    TransportError,
    TrayResponse,
    get_transport,
)
from ._common import _bump_counter, _mark_account

log = get_logger(__name__)


@dataclass(slots=True)
class PollResult:
    """Outcome of one tray poll, used for metrics and tests."""

    worker_account_id: int
    entries_seen: int
    user_entries: int
    highlights_skipped: int
    new_stories_detected: int
    direct_enqueued: int
    duration_sec: float


def compute_phase_offsets(worker_ids: list[int], interval_sec: int) -> dict[int, int]:
    """Spread workers sharing a shard evenly across the poll interval (SPEC 7.1).

    With 3 workers on a 120s interval, offsets are 0 / 40 / 80. Detection latency of
    ~60s comes from phase-shifted polling, not from polling faster.
    """
    if not worker_ids:
        return {}
    ordered = sorted(worker_ids)
    step = interval_sec / len(ordered)
    return {wid: int(round(rank * step)) for rank, wid in enumerate(ordered)}


def assign_phase_offsets() -> dict[int, int]:
    """Compute and persist phase offsets for all active workers, per shard."""
    settings = get_settings()
    offsets: dict[int, int] = {}
    with session_scope() as session:
        rows = session.execute(
            select(WorkerAccount.id, WorkerAccount.shard_id).where(
                WorkerAccount.status == "active"
            )
        ).all()

        by_shard: dict[int, list[int]] = {}
        for worker_id, shard_id in rows:
            by_shard.setdefault(shard_id, []).append(worker_id)

        for shard_id, worker_ids in by_shard.items():
            shard_offsets = compute_phase_offsets(worker_ids, settings.poll_interval_sec)
            offsets.update(shard_offsets)
            for worker_id, offset in shard_offsets.items():
                session.execute(
                    update(WorkerAccount)
                    .where(WorkerAccount.id == worker_id)
                    .values(phase_offset_sec=offset)
                )
            log.info(
                "phase_offsets_assigned",
                shard_id=shard_id,
                worker_count=len(worker_ids),
                offsets=shard_offsets,
            )
    return offsets


class TrayDiffer:
    """Diffs a tray response against the `targets.last_reel_media_ts` watermark.

    This is the entire change-detection mechanism. A story is "new" iff its
    `latest_reel_media` is strictly greater than the stored watermark.
    """

    def __init__(self, worker_account_id: int):
        self.worker_account_id = worker_account_id
        self.fetch_queue = get_queue(Q_FETCH)
        self.direct_queue = get_queue(Q_FETCH_DIRECT)

    def diff_and_enqueue(self, tray: TrayResponse) -> PollResult:
        started = time.monotonic()
        user_entries = 0
        highlights_skipped = 0
        new_stories = 0
        direct_enqueued = 0
        now = dt.datetime.now(dt.UTC)

        # Collect candidates first, then resolve watermarks in one query.
        candidates: dict[int, tuple[int, object]] = {}
        for entry in tray.entries:
            # Skip entries whose id is not all digits: highlights, `highlight:1234...`.
            if not entry.is_user_entry:
                highlights_skipped += 1
                continue
            user_entries += 1

            latest = entry.latest_reel_media
            if latest is None:  # No timestamp means nothing to compare - skip.
                continue

            user_id = entry.user_id
            assert user_id is not None
            candidates[user_id] = (int(latest), entry)

        if not candidates:
            return PollResult(
                worker_account_id=self.worker_account_id,
                entries_seen=tray.entry_count,
                user_entries=user_entries,
                highlights_skipped=highlights_skipped,
                new_stories_detected=0,
                direct_enqueued=0,
                duration_sec=time.monotonic() - started,
            )

        with session_scope() as session:
            existing = dict(
                session.execute(
                    select(Target.user_id, Target.last_reel_media_ts).where(
                        Target.user_id.in_(list(candidates))
                    )
                ).all()
            )

            advanced: list[int] = []
            for user_id, (latest, entry) in candidates.items():
                if user_id not in existing:
                    # Tray contains someone we do not monitor - ignore, do not invent a row.
                    continue
                watermark = existing[user_id]
                if watermark is not None and latest <= watermark:
                    continue

                new_stories += 1
                advanced.append(user_id)

                # A prefetched `items` array already carries story ids and media types,
                # so the fetcher can skip the reels_media call entirely (SPEC 7.1).
                if entry.has_prefetched_items:
                    self.direct_queue.push(
                        {
                            "user_id": user_id,
                            "items": entry.items,
                            "latest_reel_media": latest,
                            "detected_at": now.isoformat(),
                        }
                    )
                    direct_enqueued += 1
                else:
                    self.fetch_queue.push(
                        {
                            "user_id": user_id,
                            "latest_reel_media": latest,
                            "detected_at": now.isoformat(),
                        }
                    )

                # Advance the watermark only after enqueueing, so a crash between the
                # two re-detects rather than silently drops the story.
                session.execute(
                    update(Target)
                    .where(Target.user_id == user_id)
                    .values(last_reel_media_ts=latest, last_seen_in_tray=now)
                )

            # Mark everyone we saw as seen, even if unchanged.
            seen_ids = [uid for uid in candidates if uid in existing and uid not in advanced]
            if seen_ids:
                session.execute(
                    update(Target)
                    .where(Target.user_id.in_(seen_ids))
                    .values(last_seen_in_tray=now)
                )

        return PollResult(
            worker_account_id=self.worker_account_id,
            entries_seen=tray.entry_count,
            user_entries=user_entries,
            highlights_skipped=highlights_skipped,
            new_stories_detected=new_stories,
            direct_enqueued=direct_enqueued,
            duration_sec=time.monotonic() - started,
        )


class WorkerPoller:
    """Polls one worker account's tray forever. Owns exactly one transport/identity."""

    def __init__(self, worker_account_id: int, transport: InstagramTransport | None = None):
        self.worker_account_id = worker_account_id
        self.settings = get_settings()
        self.differ = TrayDiffer(worker_account_id)
        self._transport = transport
        self._cold_start = True
        self._stopped = asyncio.Event()

    # --- transport lifecycle ---

    def _build_transport(self) -> InstagramTransport:
        """Create the transport, restoring the saved session rather than logging in.

        Repeated logins are the single strongest ban signal (SPEC section 8).
        """
        with session_scope() as session:
            account = session.get(WorkerAccount, self.worker_account_id)
            if account is None:
                raise RuntimeError(f"worker account {self.worker_account_id} not found")
            username = account.username
            device_settings = dict(account.device_settings or {})
            proxy_url = account.proxy_url
            session_json = dict(account.session_json) if account.session_json else None
            password_enc = account.password_enc

        transport = get_transport(
            username=username,
            device_settings=device_settings,
            proxy_url=proxy_url,
        )
        if session_json:
            transport.load_session(session_json)
        else:
            password = SecretBox().decrypt(password_enc)
            dumped = transport.login(username, password)
            self._persist_session(dumped)
        return transport

    def _persist_session(self, session_json: dict) -> None:
        with session_scope() as session:
            session.execute(
                update(WorkerAccount)
                .where(WorkerAccount.id == self.worker_account_id)
                .values(
                    session_json=session_json,
                    last_login_at=dt.datetime.now(dt.UTC),
                )
            )

    @property
    def transport(self) -> InstagramTransport:
        if self._transport is None:
            self._transport = self._build_transport()
        return self._transport

    # --- one poll ---

    def poll_once(self) -> PollResult:
        """Fetch the tray, diff it, enqueue work. Synchronous - run in a thread."""
        started = time.monotonic()
        tray = self.transport.reels_tray(cold_start=self._cold_start)
        # Use reason="cold_start" only on the first call after a login (SPEC 7.1).
        self._cold_start = False

        if self.settings.tray_pagination_enabled:
            tray = self._paginate(tray)

        result = self.differ.diff_and_enqueue(tray)
        result.duration_sec = time.monotonic() - started

        self._record_poll(result)
        return result

    def _paginate(self, first_page: TrayResponse) -> TrayResponse:
        """Follow tray cursors when the tray is truncated (SPEC 7.2).

        Off by default. probe_tray.py must prove truncation before this is enabled;
        pagination raises per-shard request cost.
        """
        entries = list(first_page.entries)
        cursor = first_page.next_max_id
        pages = 1
        while cursor and pages < 20:
            page = self.transport.reels_tray(cold_start=False, max_id=cursor)
            entries.extend(page.entries)
            cursor = page.next_max_id
            pages += 1
        if pages > 1:
            log.info("tray_paginated", worker_account_id=self.worker_account_id, pages=pages)
        return TrayResponse(entries=entries, raw=first_page.raw)

    def _record_poll(self, result: PollResult) -> None:
        now = dt.datetime.now(dt.UTC)
        with session_scope() as session:
            session.execute(
                update(WorkerAccount)
                .where(WorkerAccount.id == self.worker_account_id)
                .values(last_poll_at=now, last_error=None)
            )
            _bump_counter(session, self.worker_account_id, now.date(), requests=1)

        labels = {"worker_account_id": self.worker_account_id}
        record_metric("poll_latency_sec", result.duration_sec, labels)
        # Tray entry count is the truncation canary - a sudden drop means trouble.
        record_metric("tray_entry_count", result.entries_seen, labels)
        record_metric("stories_detected", result.new_stories_detected, labels)
        record_metric("poll_success", 1, labels)

        log.info(
            "tray_polled",
            worker_account_id=self.worker_account_id,
            entries=result.entries_seen,
            user_entries=result.user_entries,
            highlights_skipped=result.highlights_skipped,
            new_stories=result.new_stories_detected,
            direct_enqueued=result.direct_enqueued,
            duration_sec=round(result.duration_sec, 3),
        )

    # --- error handling ---

    def _handle_error(self, exc: Exception) -> float:
        """Record the incident and return how long to back off before the next poll."""
        wid = self.worker_account_id

        if isinstance(exc, ChallengeRequiredError):
            # Never attempt to auto-solve. Stop this account and alert (SPEC 7.8).
            _mark_account(wid, status="challenged", error=str(exc), event="challenge")
            log.error("challenge_required", worker_account_id=wid, alert=True)
            self._stopped.set()
            return 0.0

        if isinstance(exc, LoginRequiredError):
            # Warden owns re-login policy; drop the session so it is rebuilt with the
            # SAME device settings and SAME proxy.
            _mark_account(wid, status=None, error=str(exc), event="login_required")
            self._transport = None
            self._cold_start = True
            log.warning("login_required", worker_account_id=wid)
            return 60.0

        if isinstance(exc, (FeedbackRequiredError, PleaseWaitError)):
            # Reading is much safer than writing - keep the account active, just back off.
            _mark_account(wid, status=None, error=str(exc), event="feedback_required")
            backoff = random.uniform(300, 1800)
            log.warning("feedback_required", worker_account_id=wid, backoff_sec=int(backoff))
            return backoff

        if isinstance(exc, RateLimitedError):
            return random.uniform(120, 600)

        if isinstance(exc, ProxyBlockedError):
            _mark_account(wid, status="challenged", error=str(exc), event="ban")
            log.error("proxy_blocked", worker_account_id=wid, alert=True)
            self._stopped.set()
            return 0.0

        if isinstance(exc, TransportError):
            log.warning("poll_transport_error", worker_account_id=wid, error=str(exc))
            return random.uniform(30, 120)

        log.exception("poll_unexpected_error", worker_account_id=wid, error=str(exc))
        return 60.0

    # --- the loop ---

    def _sleep_seconds(self) -> float:
        """now + interval +/- jitter. Real randomness, not a fixed offset (SPEC section 8)."""
        return self.settings.poll_interval_sec + random.uniform(
            0, self.settings.poll_jitter_max_sec
        )

    async def run(self, initial_offset: int | None = None) -> None:
        offset = initial_offset
        if offset is None:
            with session_scope() as session:
                account = session.get(WorkerAccount, self.worker_account_id)
                offset = account.phase_offset_sec if account else 0
        if offset:
            log.info("poller_phase_offset", worker_account_id=self.worker_account_id, offset=offset)
            await asyncio.sleep(offset)

        while not self._stopped.is_set():
            try:
                await asyncio.to_thread(self.poll_once)
                delay = self._sleep_seconds()
            except Exception as exc:  # noqa: BLE001 - loop must never die
                record_metric("poll_success", 0, {"worker_account_id": self.worker_account_id})
                delay = self._handle_error(exc)
                if self._stopped.is_set():
                    break
                delay = delay or self._sleep_seconds()

            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=delay)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stopped.set()


async def run_poller() -> None:
    """Entry point: one asyncio task per active worker account (SPEC section 6)."""
    offsets = assign_phase_offsets()
    with session_scope() as session:
        worker_ids = list(
            session.scalars(
                select(WorkerAccount.id).where(WorkerAccount.status == "active")
            ).all()
        )

    if not worker_ids:
        log.warning("no_active_workers")
        return

    log.info("poller_starting", worker_count=len(worker_ids))
    pollers = [WorkerPoller(wid) for wid in worker_ids]
    await asyncio.gather(
        *(p.run(initial_offset=offsets.get(p.worker_account_id)) for p in pollers),
        return_exceptions=True,
    )
