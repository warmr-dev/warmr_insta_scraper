"""The bizcheck process (SPEC 7.5).

Applies vendor / service-fit / community / geography / forwarding rules to
analysed stories, writes `business_checks`, and pushes approved leads to q:notify.

The chain short-circuits on the first failure: each check runs only if every
earlier one passed, so a rejected lead costs at most one vendor lookup past the
failing step. `final_status = 'approved'` requires BOTH a passing chain AND
`final_score >= settings.approval_score_min` - the AI score alone is never
sufficient (SPEC 7.5).
"""

from __future__ import annotations

import datetime as dt
import signal
import types
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Q_BIZCHECK, Q_NOTIFY, get_settings
from ..db.models import BusinessCheck, Story, StoryAnalysis, Target
from ..db.session import session_scope
from ..logging_setup import get_logger
from ..metrics import record_metric
from ..queue import get_queue
from ..vendors import LeadContext, StoryContext, Vendor, VendorRepository
from ..vendors.stub import get_vendor_repository

log = get_logger(__name__)

# Reject reasons - stable strings, they land in business_checks.reject_reason
# and are read back by tests and by the Slack summary line.
REASON_NO_ANALYSIS = "no_analysis"
REASON_NO_VENDOR = "no_matching_vendor"
REASON_LOW_SERVICE_FIT = "service_fit_below_threshold"
REASON_COMMUNITY_CONFLICT = "community_conflict"
REASON_GEO_NOT_SERVICEABLE = "geography_not_serviceable"
REASON_FORWARDING_BLOCKED = "forwarding_not_permitted"
REASON_LOW_SCORE = "final_score_below_threshold"

STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_REVIEW = "review"


@dataclass(slots=True)
class CheckOutcome:
    """Result of running the chain for one story."""

    story_id: str
    final_status: str
    vendor_id: str | None = None
    service_fit: float | None = None
    geo_ok: bool | None = None
    community_conflict: bool | None = None
    reject_reason: str | None = None

    @property
    def approved(self) -> bool:
        return self.final_status == STATUS_APPROVED


def _analysis_blob(analysis: StoryAnalysis) -> dict[str, Any]:
    """Merged cheap+smart model output.

    `story_analysis` has no geography/community columns, but SPEC 7.4 has the
    cheap model return `geography`, so it lives in the JSONB result blobs.
    Smart output wins where both are present - it is the later, better judgement.
    """
    blob: dict[str, Any] = {}
    for source in (analysis.cheap_result, analysis.smart_result):
        if isinstance(source, dict):
            blob.update({k: v for k, v in source.items() if v is not None})
    return blob


class BizChecker:
    """SPEC 7.5. One process; `process_one` is the unit tests drive."""

    def __init__(
        self,
        repository: VendorRepository | None = None,
        queue: Any | None = None,
    ) -> None:
        self.settings = get_settings()
        self.repo = repository if repository is not None else get_vendor_repository()
        # Queues are bound to one name each: push(item) / pop_blocking(timeout).
        self.queue = queue if queue is not None else get_queue(Q_BIZCHECK)
        self.notify_queue = get_queue(Q_NOTIFY)
        self._stopping = False

    # --- the check chain ---

    def _run_chain(
        self,
        story: Story,
        analysis: StoryAnalysis,
        story_ctx: StoryContext,
        lead_ctx: LeadContext,
    ) -> CheckOutcome:
        """Checks in SPEC 7.5 order, short-circuiting on the first failure.

        Each early return leaves the not-yet-run checks' columns NULL, which is
        how a reader tells "failed here" from "passed but irrelevant".
        """
        outcome = CheckOutcome(story_id=story.story_id, final_status=STATUS_REJECTED)

        # Check 1 - a matching vendor exists for service_category.
        vendor: Vendor | None = self.repo.find_vendor_for_category(
            story_ctx.service_category
        )
        if vendor is None:
            outcome.reject_reason = REASON_NO_VENDOR
            return outcome
        outcome.vendor_id = vendor.vendor_id

        # Check 2 - Service Fit >= threshold (config; SPEC open question #4).
        fit = self.repo.service_fit(vendor.vendor_id, story_ctx)
        outcome.service_fit = fit
        if fit < self.settings.service_fit_threshold:
            outcome.reject_reason = REASON_LOW_SERVICE_FIT
            return outcome

        # Check 3 - lead community must differ from the vendor's.
        vendor_community = self.repo.vendor_community(vendor.vendor_id)
        conflict = bool(
            story_ctx.community
            and vendor_community
            and story_ctx.community.strip().casefold()
            == vendor_community.strip().casefold()
        )
        outcome.community_conflict = conflict
        if conflict:
            outcome.reject_reason = REASON_COMMUNITY_CONFLICT
            return outcome

        # Check 4 - geography is serviceable.
        geo_ok = self.repo.serves_geography(vendor.vendor_id, story_ctx.geography)
        outcome.geo_ok = geo_ok
        if not geo_ok:
            outcome.reject_reason = REASON_GEO_NOT_SERVICEABLE
            return outcome

        # Check 5 - internal forwarding rules permit this lead.
        if not self.repo.forwarding_allowed(vendor.vendor_id, lead_ctx):
            outcome.reject_reason = REASON_FORWARDING_BLOCKED
            return outcome

        # Every check passed. Approval ALSO needs the AI score - SPEC 7.5 is
        # explicit that the score alone is never sufficient, and neither is a
        # clean chain.
        final_score = analysis.final_score
        if final_score is None or final_score < self.settings.approval_score_min:
            outcome.reject_reason = REASON_LOW_SCORE
            return outcome

        outcome.final_status = STATUS_APPROVED
        return outcome

    # --- per-story processing ---

    def process_one(self, story_id: str) -> CheckOutcome | None:
        """Check one story, persist the result, enqueue if approved.

        Returns None when the story or its analysis is missing.
        """
        with session_scope() as session:
            story = session.get(Story, story_id)
            if story is None:
                log.warning("bizcheck_story_missing", story_id=story_id)
                return None

            analysis = session.get(StoryAnalysis, story_id)
            if analysis is None:
                log.warning("bizcheck_analysis_missing", story_id=story_id)
                outcome = CheckOutcome(
                    story_id=story_id,
                    final_status=STATUS_REVIEW,
                    reject_reason=REASON_NO_ANALYSIS,
                )
                self._persist(session, story, outcome)
                return outcome

            blob = _analysis_blob(analysis)
            username = session.scalar(
                select(Target.username).where(Target.user_id == story.target_user_id)
            )

            story_ctx = StoryContext(
                story_id=story_id,
                service_category=analysis.service_category,
                intent_type=analysis.intent_type,
                final_score=analysis.final_score,
                geography=blob.get("geography"),
                community=blob.get("community"),
                ocr_text=analysis.ocr_text,
                raw_analysis=blob,
            )
            lead_ctx = LeadContext(
                story_id=story_id,
                target_user_id=story.target_user_id,
                username=username,
                service_category=analysis.service_category,
                final_score=analysis.final_score,
                geography=story_ctx.geography,
                community=story_ctx.community,
            )

            outcome = self._run_chain(story, analysis, story_ctx, lead_ctx)
            self._persist(session, story, outcome)

        # Enqueue only after the transaction committed - the notifier must never
        # see a story_id whose business_checks row is not yet visible.
        if outcome.approved:
            self.notify_queue.push({"story_id": story_id})

        log.info(
            "bizcheck_done",
            story_id=story_id,
            final_status=outcome.final_status,
            vendor_id=outcome.vendor_id,
            service_fit=outcome.service_fit,
            reject_reason=outcome.reject_reason,
        )
        record_metric(
            "bizcheck.decision",
            1,
            labels={
                "final_status": outcome.final_status,
                "reject_reason": outcome.reject_reason or "none",
            },
        )
        return outcome

    def _persist(self, session: Session, story: Story, outcome: CheckOutcome) -> None:
        """Upsert business_checks and advance the story to 'checked'."""
        row = session.get(BusinessCheck, outcome.story_id)
        if row is None:
            row = BusinessCheck(story_id=outcome.story_id)
            session.add(row)

        row.vendor_id = outcome.vendor_id
        row.service_fit = outcome.service_fit
        row.geo_ok = outcome.geo_ok
        row.community_conflict = outcome.community_conflict
        row.final_status = outcome.final_status
        row.reject_reason = outcome.reject_reason
        row.checked_at = dt.datetime.now(dt.UTC)

        story.pipeline_state = "checked"

    # --- process loop ---

    def stop(self, *_args: Any) -> None:
        self._stopping = True

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self.stop)

    def run(self, *, max_items: int | None = None) -> int:
        """Drain q:bizcheck until stopped. `max_items` bounds the loop in tests."""
        log.info(
            "bizcheck_start",
            service_fit_threshold=self.settings.service_fit_threshold,
            approval_score_min=self.settings.approval_score_min,
        )
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
            except Exception as exc:  # keep the loop alive; one bad story is not fatal
                log.exception("bizcheck_failed", story_id=story_id, error=str(exc))
                record_metric("bizcheck.error", 1)
            processed += 1
        log.info("bizcheck_stopped", processed=processed)
        return processed

    def __enter__(self) -> BizChecker:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: types.TracebackType | None,
    ) -> None:
        self.repo.close()


def main() -> None:
    checker = BizChecker()
    checker.install_signal_handlers()
    checker.run()


if __name__ == "__main__":
    main()
