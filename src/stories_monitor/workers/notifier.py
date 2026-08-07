"""The notifier process (SPEC 7.6).

Only `business_checks.final_status = 'approved'` reaches Slack.

Idempotency (SPEC 7.6, Phase 4 acceptance "running the notifier twice posts
nothing the second time"): before any Slack call we INSERT a `slack_deliveries`
row with status 'pending' and COMMIT it. The insert is
`ON CONFLICT (story_id) DO NOTHING`, and the primary key on `story_id` means a
second run - or a restart after a crash between send and commit - inserts zero
rows. Zero rows inserted is read as "someone already claimed this story", and we
return without sending. The claim is therefore what gates the send, not the
delivery status, so even a row stuck in 'pending' is never re-posted.
"""

from __future__ import annotations

import datetime as dt
import random
import signal
import time
import types
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..config import Q_NOTIFY, get_settings
from ..db.models import BusinessCheck, SlackDelivery, Story, StoryAnalysis, Target
from ..db.session import session_scope
from ..logging_setup import get_logger
from ..metrics import record_metric
from ..notify.slack import LeadMessage, SlackNotifier, get_notifier
from ..queue import get_queue

log = get_logger(__name__)

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"

# Exponential backoff: base * 2**attempt, jittered, capped (SPEC 7.6).
BACKOFF_BASE_SEC = 1.0
BACKOFF_MAX_SEC = 60.0


@dataclass(slots=True)
class DeliveryResult:
    story_id: str
    status: str
    slack_ts: str | None = None
    attempts: int = 0
    error: str | None = None
    # True when the story was already claimed by an earlier run - nothing sent.
    skipped: bool = False


class Notifier:
    """SPEC 7.6. `process_one` is the unit tests drive."""

    def __init__(
        self,
        notifier: SlackNotifier | None = None,
        queue: Any | None = None,
        sleep: Any | None = None,
    ) -> None:
        self.settings = get_settings()
        self.slack = notifier if notifier is not None else get_notifier()
        # Queues are bound to one name each: push(item) / pop_blocking(timeout).
        self.queue = queue if queue is not None else get_queue(Q_NOTIFY)
        self._sleep = sleep if sleep is not None else time.sleep
        self._stopping = False

    # --- idempotency claim ---

    @staticmethod
    def _claim(session: Session, story_id: str) -> bool:
        """Insert the 'pending' row. True if WE claimed it, False if already claimed.

        This is the single point where double-posting is prevented. The PK on
        story_id makes the insert a no-op the second time, so a False return
        means another run (or an earlier crashed attempt) already owns this
        story and we must not send.
        """
        stmt = (
            pg_insert(SlackDelivery)
            .values(story_id=story_id, status=STATUS_PENDING, attempts=0)
            .on_conflict_do_nothing(index_elements=["story_id"])
            .returning(SlackDelivery.story_id)
        )
        return session.execute(stmt).scalar_one_or_none() is not None

    # --- message assembly ---

    @staticmethod
    def _build_message(session: Session, story_id: str) -> LeadMessage | None:
        """Gather the SPEC 7.6 fields. None if the lead is not approved."""
        row = session.execute(
            select(Story, StoryAnalysis, BusinessCheck, Target)
            .join(StoryAnalysis, StoryAnalysis.story_id == Story.story_id, isouter=True)
            .join(BusinessCheck, BusinessCheck.story_id == Story.story_id, isouter=True)
            .join(Target, Target.user_id == Story.target_user_id, isouter=True)
            .where(Story.story_id == story_id)
        ).one_or_none()
        if row is None:
            log.warning("notify_story_missing", story_id=story_id)
            return None

        story, analysis, check, target = row
        # SPEC 7.6: only approved leads reach Slack. Enforced here as well as at
        # enqueue time - the queue is not a trustworthy authorisation boundary.
        if check is None or check.final_status != "approved":
            log.info(
                "notify_not_approved",
                story_id=story_id,
                final_status=check.final_status if check else None,
            )
            return None

        return LeadMessage(
            story_id=story_id,
            username=target.username if target else None,
            instagram_url=(target.instagram_url if target else None),
            service_category=analysis.service_category if analysis else None,
            final_score=analysis.final_score if analysis else None,
            ai_explanation=analysis.ai_explanation if analysis else None,
            taken_at=story.taken_at,
            vendor_id=check.vendor_id,
            service_fit=float(check.service_fit) if check.service_fit is not None else None,
            geo_ok=check.geo_ok,
            community_conflict=check.community_conflict,
        )

    # --- per-story processing ---

    def process_one(self, story_id: str) -> DeliveryResult:
        """Deliver one approved lead, exactly once."""
        # 1. Build the message and decide approval in one read transaction.
        with session_scope() as session:
            message = self._build_message(session, story_id)
        if message is None:
            return DeliveryResult(story_id=story_id, status=STATUS_FAILED, skipped=True)

        # 2. Claim the story BEFORE calling Slack, in its own committed
        #    transaction. If we crash after this point the claim survives and
        #    the next run will not re-send.
        with session_scope() as session:
            claimed = self._claim(session, story_id)
        if not claimed:
            log.info("notify_already_delivered", story_id=story_id)
            record_metric("notify.duplicate_suppressed", 1)
            return DeliveryResult(story_id=story_id, status=STATUS_PENDING, skipped=True)

        # 3. Send with exponential backoff.
        max_attempts = max(1, self.settings.slack_max_attempts)
        last_error: str | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                slack_ts = self.slack.send(message)
            except Exception as exc:  # SlackError and any transport failure are retryable
                last_error = str(exc)
                log.warning(
                    "notify_attempt_failed",
                    story_id=story_id,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error=last_error,
                )
                self._record_attempt(story_id, attempt, last_error)
                if attempt < max_attempts:
                    self._sleep(self._backoff(attempt))
                continue

            self._mark_sent(story_id, slack_ts, attempt)
            record_metric("notify.sent", 1)
            log.info("notify_sent", story_id=story_id, slack_ts=slack_ts, attempts=attempt)
            return DeliveryResult(
                story_id=story_id, status=STATUS_SENT, slack_ts=slack_ts, attempts=attempt
            )

        # 4. Exhausted. Mark failed and raise an alert-level event (SPEC 7.6).
        self._mark_failed(story_id, max_attempts, last_error)
        record_metric("notify.failed", 1)
        log.error(
            "notify_delivery_failed",
            alert=True,
            severity="alert",
            story_id=story_id,
            attempts=max_attempts,
            error=last_error,
        )
        return DeliveryResult(
            story_id=story_id,
            status=STATUS_FAILED,
            attempts=max_attempts,
            error=last_error,
        )

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential with full jitter, capped."""
        window = min(BACKOFF_MAX_SEC, BACKOFF_BASE_SEC * (2 ** (attempt - 1)))
        return random.uniform(0.0, window)

    # --- delivery row updates ---

    @staticmethod
    def _record_attempt(story_id: str, attempts: int, error: str | None) -> None:
        with session_scope() as session:
            row = session.get(SlackDelivery, story_id)
            if row is not None:
                row.attempts = attempts
                row.last_error = error

    @staticmethod
    def _mark_sent(story_id: str, slack_ts: str, attempts: int) -> None:
        with session_scope() as session:
            row = session.get(SlackDelivery, story_id)
            if row is not None:
                row.status = STATUS_SENT
                row.slack_ts = slack_ts
                row.attempts = attempts
                row.last_error = None
                row.sent_at = dt.datetime.now(dt.UTC)
            story = session.get(Story, story_id)
            if story is not None:
                story.pipeline_state = "sent"

    @staticmethod
    def _mark_failed(story_id: str, attempts: int, error: str | None) -> None:
        with session_scope() as session:
            row = session.get(SlackDelivery, story_id)
            if row is not None:
                row.status = STATUS_FAILED
                row.attempts = attempts
                row.last_error = error
            story = session.get(Story, story_id)
            if story is not None:
                story.pipeline_state = "failed"

    # --- process loop ---

    def stop(self, *_args: Any) -> None:
        self._stopping = True

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self.stop)

    def run(self, *, max_items: int | None = None) -> int:
        """Drain q:notify until stopped. `max_items` bounds the loop in tests."""
        log.info("notifier_start", max_attempts=self.settings.slack_max_attempts)
        processed = 0
        while not self._stopping:
            if max_items is not None and processed >= max_items:
                break
            payload = self.queue.pop_blocking(timeout=5)
            if payload is None:
                continue
            story_id = payload.get("story_id") if isinstance(payload, dict) else payload
            if not story_id:
                continue
            try:
                self.process_one(story_id)
            except Exception as exc:  # keep the loop alive
                log.exception("notify_failed", story_id=story_id, error=str(exc))
                record_metric("notify.error", 1)
            processed += 1
        log.info("notifier_stopped", processed=processed)
        return processed

    def __enter__(self) -> Notifier:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: types.TracebackType | None,
    ) -> None:
        close = getattr(self.slack, "close", None)
        if callable(close):
            close()


def main() -> None:
    notifier = Notifier()
    notifier.install_signal_handlers()
    notifier.run()


if __name__ == "__main__":
    main()
