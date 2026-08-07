"""The analyzer process (SPEC 7.4).

Per story popped from q:analyze:
  1. OCR the image.
  2. Cheap model.
  3. Route on the cheap score: 0-4 reject and stop; 5-6 smart model; 7+ skip the
     smart model and take the cheap score as final.
  4. Delete the temp file in a `finally` block. No exception path may leave media
     on disk (SPEC 7.4 step 4, SPEC section 11).
  5. Write story_analysis and advance stories.pipeline_state.

Structured as a class so tests can drive `process_one` without the `run` loop.
Writes are upserts keyed on story_id, so re-processing an already-analyzed story
never double-writes.
"""

from __future__ import annotations

import datetime as dt
import json
import signal
import types
from pathlib import Path
from typing import Any

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..ai.client import AIClient, get_ai_client
from ..ai.ocr import OCREngine, OCRUnavailable, get_ocr_engine
from ..ai.schemas import CheapResult, SmartResult
from ..config import Q_ANALYZE, Q_BIZCHECK, get_settings
from ..db.models import Story, StoryAnalysis
from ..db.session import session_scope
from ..logging_setup import get_logger
from ..metrics import record_metric
from ..queue import get_queue

logger = get_logger(__name__)

# pipeline_state values this worker owns (SPEC section 5).
STATE_ANALYZING = "analyzing"
STATE_ANALYZED = "analyzed"
STATE_FAILED = "failed"


class Analyzer:
    """OCR + cheap model + smart model, with routing and temp-file cleanup."""

    def __init__(
        self,
        ai_client: AIClient | None = None,
        ocr_engine: OCREngine | None = None,
        queue: Any | None = None,
    ) -> None:
        settings = get_settings()
        self.settings = settings
        self.ai = ai_client or get_ai_client()
        self.ocr = ocr_engine or get_ocr_engine(client=self.ai)
        # Queues are bound to one name each: push(item) / pop_blocking(timeout).
        self.queue = queue if queue is not None else get_queue(Q_ANALYZE)
        self.bizcheck_queue = get_queue(Q_BIZCHECK)
        self._stopping = False

    # -- routing thresholds (SPEC 7.4 step 3) --

    @property
    def smart_min(self) -> int:
        return self.settings.smart_model_score_min  # default 5

    @property
    def smart_max(self) -> int:
        return self.settings.smart_model_score_max  # default 6

    def route(self, cheap_score: int) -> str:
        """`reject` (0-4), `smart` (5-6), or `accept` (7+)."""
        if cheap_score < self.smart_min:
            return "reject"
        if cheap_score <= self.smart_max:
            return "smart"
        return "accept"

    # -- single story --

    def process_one(self, payload: dict[str, Any] | str) -> dict[str, Any]:
        """Analyze one story. Returns a small result dict for tests and logs.

        The temp file is deleted in a `finally` that wraps every step including
        the DB write, so no exception path leaves media on disk.
        """
        job = _coerce_payload(payload)
        story_id = job.get("story_id")
        # The fetcher enqueues `media_path`; accept the aliases too so a payload
        # from either side of the queue is understood (SPEC 7.3 -> 7.4 handoff).
        temp_path = job.get("media_path") or job.get("temp_path") or job.get("path")

        if not story_id:
            logger.error("analyze_bad_payload", reason="missing story_id")
            record_metric("analyzer_bad_payloads", 1, {})
            return {"story_id": None, "state": STATE_FAILED, "reason": "missing story_id"}

        outcome: dict[str, Any] = {"story_id": story_id, "state": STATE_FAILED}

        try:
            self._mark_analyzing(story_id)
            outcome = self._analyze(story_id, temp_path)
        except Exception as exc:  # noqa: BLE001 - one bad story must not kill the loop
            logger.error("analyze_failed", story_id=story_id, error=str(exc))
            record_metric("stories_analysis_failed", 1, {})
            outcome = {"story_id": story_id, "state": STATE_FAILED, "reason": str(exc)}
            # Best-effort state write; cleanup below runs regardless of its outcome.
            try:
                self._set_state(story_id, STATE_FAILED)
            except Exception as state_exc:  # noqa: BLE001
                logger.error(
                    "analyze_state_write_failed", story_id=story_id, error=str(state_exc)
                )
        finally:
            # SPEC 7.4 step 4 / section 11: media never survives the analysis step.
            delete_temp_file(temp_path, story_id=story_id)

        return outcome

    def _analyze(self, story_id: str, temp_path: str | None) -> dict[str, Any]:
        if not temp_path:
            raise ValueError("payload has no temp_path")

        ocr_text = self._run_ocr(temp_path)

        cheap = self.ai.call_cheap(temp_path, ocr_text)
        record_metric("cheap_model_scores", cheap.score, {})

        decision = self.route(cheap.score)
        smart: SmartResult | None = None

        if decision == "reject":
            final_score = cheap.score
            record_metric("stories_rejected_cheap", 1, {})
        elif decision == "smart":
            smart = self.ai.call_smart(temp_path, ocr_text, cheap)
            final_score = smart.final_score
            record_metric("smart_model_invocations", 1, {})
        else:  # accept - 7+ skips the smart model entirely (SPEC 7.4)
            final_score = cheap.score
            record_metric("stories_accepted_cheap", 1, {})

        self._write_analysis(
            story_id=story_id,
            ocr_text=ocr_text,
            cheap=cheap,
            smart=smart,
            final_score=final_score,
            state=STATE_ANALYZED,
        )

        # Rejected stories still get a written result but do not advance to
        # business checks (SPEC 7.4 step 3: "reject, write result, stop").
        if decision != "reject":
            self.bizcheck_queue.push({"story_id": story_id, "final_score": final_score})

        record_metric("stories_analyzed", 1, {"decision": decision})
        logger.info(
            "analyze_done",
            story_id=story_id,
            decision=decision,
            cheap_score=cheap.score,
            final_score=final_score,
            smart_used=smart is not None,
        )
        return {
            "story_id": story_id,
            "state": STATE_ANALYZED,
            "decision": decision,
            "cheap_score": cheap.score,
            "final_score": final_score,
            "smart_used": smart is not None,
        }

    def _run_ocr(self, temp_path: str) -> str:
        try:
            return self.ocr.extract_text(temp_path)
        except OCRUnavailable as exc:
            # A missing OCR backend must not lose the story; the models still see
            # the image. Surface it once per story rather than failing the run.
            logger.warning("ocr_unavailable", engine=getattr(self.ocr, "name", "?"), error=str(exc))
            record_metric("ocr_unavailable", 1, {})
            return ""

    # -- persistence --

    def _mark_analyzing(self, story_id: str) -> None:
        self._set_state(story_id, STATE_ANALYZING)

    def _set_state(self, story_id: str, state: str) -> None:
        with session_scope() as session:
            session.execute(
                update(Story).where(Story.story_id == story_id).values(pipeline_state=state)
            )

    def _write_analysis(
        self,
        *,
        story_id: str,
        ocr_text: str,
        cheap: CheapResult,
        smart: SmartResult | None,
        final_score: int,
        state: str,
    ) -> None:
        """Upsert on story_id so re-processing never double-writes (idempotent)."""
        values: dict[str, Any] = {
            "story_id": story_id,
            "ocr_text": ocr_text or None,
            "cheap_score": cheap.score,
            "cheap_result": cheap.model_dump(mode="json"),
            "smart_score": smart.final_score if smart else None,
            "smart_result": smart.model_dump(mode="json") if smart else None,
            "final_score": final_score,
            "service_category": (smart.service_category if smart else cheap.service_category),
            # A 7+ story skips the smart model, so fall back to the cheap model's
            # signals - otherwise the highest-scoring leads reach Slack with an
            # empty explanation, which SPEC 7.6 requires in the message.
            "intent_type": smart.intent_type if smart else _cheap_intent(cheap),
            "ai_explanation": smart.explanation if smart else _cheap_explanation(cheap),
            "analyzed_at": dt.datetime.now(dt.UTC),
        }

        stmt = pg_insert(StoryAnalysis).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[StoryAnalysis.story_id],
            set_={k: v for k, v in values.items() if k != "story_id"},
        )

        with session_scope() as session:
            session.execute(stmt)
            session.execute(
                update(Story).where(Story.story_id == story_id).values(pipeline_state=state)
            )

    # -- loop --

    def stop(self) -> None:
        self._stopping = True

    def run(self) -> None:
        """Drain q:analyze until stopped. Each story is independent."""
        self._install_signal_handlers()
        logger.info(
            "analyzer_started",
            ocr_engine=getattr(self.ocr, "name", "?"),
            smart_range=[self.smart_min, self.smart_max],
        )

        while not self._stopping:
            payload = self.queue.pop_blocking(timeout=5)
            if payload is None:
                continue
            self.process_one(payload)

        logger.info("analyzer_stopped")

    def _install_signal_handlers(self) -> None:
        def _handle(signum: int, _frame: types.FrameType | None) -> None:
            logger.info("analyzer_signal", signal=signum)
            self.stop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handle)
            except ValueError:  # not on the main thread (tests)
                pass


# --- module-level helpers ---------------------------------------------------


def _cheap_intent(cheap: CheapResult) -> str | None:
    """Intent label derived from the cheap model's booleans (no smart call)."""
    if cheap.seeking_contractor:
        return "seeking_contractor"
    if cheap.explicit_purchase_intent:
        return "purchase_intent"
    return None


def _cheap_explanation(cheap: CheapResult) -> str:
    """One-line rationale for a 7+ story, built from the cheap model's fields."""
    bits: list[str] = []
    if cheap.seeking_contractor:
        bits.append("actively seeking a contractor")
    if cheap.explicit_purchase_intent:
        bits.append("explicit purchase intent")
    if cheap.service_category:
        bits.append(f"category: {cheap.service_category}")
    if cheap.geography:
        bits.append(f"location: {cheap.geography}")
    detail = "; ".join(bits) if bits else "no qualifying signals recorded"
    return f"Scored {cheap.score}/10 by the fast classifier ({detail})."


def delete_temp_file(temp_path: str | None, *, story_id: str | None = None) -> None:
    """Delete downloaded media. Must never raise (SPEC 7.4 step 4).

    `missing_ok=True` so a second call, or a file the fetcher never wrote, is a
    no-op; any residual OS error is logged rather than propagated, because this
    runs inside a `finally` and must not mask the original exception.
    """
    if not temp_path:
        return
    try:
        Path(temp_path).unlink(missing_ok=True)
    except OSError as exc:
        logger.error("temp_file_delete_failed", story_id=story_id, error=str(exc))
        record_metric("temp_file_delete_failures", 1, {})
    else:
        record_metric("temp_files_deleted", 1, {})


def _coerce_payload(payload: dict[str, Any] | str) -> dict[str, Any]:
    """Accept either a decoded dict or a raw JSON string from the queue."""
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        try:
            decoded = json.loads(payload)
        except ValueError:
            return {"story_id": payload}
        return decoded if isinstance(decoded, dict) else {"story_id": str(decoded)}
    return {}


def main() -> None:  # pragma: no cover - process entrypoint
    Analyzer().run()


if __name__ == "__main__":  # pragma: no cover
    main()
