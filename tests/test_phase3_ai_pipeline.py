"""SPEC Phase 3 acceptance criteria (SPEC section 9).

*Accept:* a fixture photo flows to `analyzed`; scores 5-6 demonstrably hit the
smart model and 7+ demonstrably do not; no temp files remain after a forced
mid-pipeline exception.

Every AI call goes through `FakeAIClient` (no Anthropic key in this environment)
and OCR through `NullOCR` (no tesseract binary). The routing assertions are made
against the fake's `smart_calls` counter, so "demonstrably" means an observed
call count, not an inferred one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import drain
from pydantic import ValidationError

from stories_monitor.ai.client import (
    AIClientError,
    FakeAIClient,
    parse_json_object,
    strip_fences,
)
from stories_monitor.ai.schemas import CheapResult, SmartResult
from stories_monitor.config import Q_BIZCHECK
from stories_monitor.db.models import Story, StoryAnalysis
from stories_monitor.workers.analyzer import Analyzer, delete_temp_file

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944"
    "415478da63fccfc00f0004850180084a98c2100000000049454e44ae426082"
)


def _make_image(tmp_path: Path, name: str) -> str:
    path = tmp_path / name
    path.write_bytes(PNG_1X1)
    return str(path)


@pytest.fixture()
def analyzer(fake_ai, null_ocr, fake_queues):
    """An Analyzer wired to the deterministic fake client and the NullOCR backend.

    `fake_queues` seeds the queue cache, so the Analyzer's own `get_queue(...)`
    calls resolve to in-memory queues (there is no Redis in this environment).
    """
    return Analyzer(ai_client=fake_ai, ocr_engine=null_ocr)


def _score_payload(tmp_path, story_id: str, score: int) -> dict:
    """FakeAIClient pins the cheap score from a `score=N` marker in the path."""
    return {
        "story_id": story_id,
        "temp_path": _make_image(tmp_path, f"{story_id}_score={score}.png"),
    }


# --- a fixture photo reaches 'analyzed' ---------------------------------------


def test_a_fixture_photo_flows_to_pipeline_state_analyzed(
    db, fixture_targets, make_story, analyzer, fake_ai, tmp_path
):
    """SPEC Phase 3 acceptance: a fixture photo flows to `analyzed`."""
    story_id = "3111000000000000001"
    make_story(story_id, 1234567890, media_type=1, pipeline_state="new")

    result = analyzer.process_one(_score_payload(tmp_path, story_id, 8))

    assert result["state"] == "analyzed"
    assert fake_ai.cheap_calls == 1

    with db() as session:
        story = session.get(Story, story_id)
        analysis = session.get(StoryAnalysis, story_id)

    assert story.pipeline_state == "analyzed"
    assert analysis is not None
    assert analysis.cheap_score == 8
    assert analysis.final_score == 8
    assert analysis.analyzed_at is not None
    assert isinstance(analysis.cheap_result, dict)


def test_the_full_fetch_to_analyze_hop_lands_on_analyzed(
    db, fake_queues, fixture_targets, fixture_transport, fake_ai,
    null_ocr, tmp_path, monkeypatch,
):
    """The fetcher downloads a real fixture image and the analyzer consumes it."""
    monkeypatch.setenv("MEDIA_TMP_DIR", str(tmp_path))
    from stories_monitor.config import Q_ANALYZE, Q_FETCH, get_settings
    from stories_monitor.workers.fetcher import Fetcher

    get_settings.cache_clear()

    transport = fixture_transport(media_scenario="photo_story")
    fake_queues[Q_FETCH].push({"user_id": 1234567890, "latest_reel_media": 1786095900})
    written = Fetcher(transport=transport)._drain_fetch()
    assert written == 2, "the photo fixture carries two photo items"

    job = fake_queues[Q_ANALYZE].pop_blocking(timeout=0)
    assert job is not None
    media_path = job["media_path"]
    assert Path(media_path).is_file(), "the fetcher did not write the media file"

    analyzer = Analyzer(ai_client=fake_ai, ocr_engine=null_ocr)
    result = analyzer.process_one({"story_id": job["story_id"], "temp_path": media_path})

    assert result["state"] == "analyzed"
    with db() as session:
        assert session.get(Story, job["story_id"]).pipeline_state == "analyzed"
    assert not Path(media_path).exists(), "media survived the analysis step"

    get_settings.cache_clear()


# --- score routing (SPEC 7.4 step 3) ------------------------------------------


@pytest.mark.parametrize("score", [5, 6])
def test_scores_five_and_six_demonstrably_hit_the_smart_model(
    db, fixture_targets, make_story, analyzer, fake_ai, tmp_path, score
):
    """SPEC Phase 3 acceptance: 5-6 demonstrably hit the smart model."""
    story_id = f"story_smart_{score}"
    make_story(story_id, 1234567890)

    result = analyzer.process_one(_score_payload(tmp_path, story_id, score))

    assert result["cheap_score"] == score
    assert result["decision"] == "smart"
    assert result["smart_used"] is True
    assert fake_ai.smart_calls == 1, "the smart model was NOT called for a 5-6 score"

    with db() as session:
        analysis = session.get(StoryAnalysis, story_id)
    assert analysis.smart_result is not None
    assert analysis.smart_score is not None
    assert analysis.final_score == analysis.smart_score, (
        "a 5-6 score must take its final score from the smart model"
    )


@pytest.mark.parametrize("score", [7, 8, 9, 10])
def test_scores_seven_plus_demonstrably_do_not_hit_the_smart_model(
    db, fixture_targets, make_story, analyzer, fake_ai, tmp_path, score
):
    """SPEC Phase 3 acceptance: 7+ demonstrably does not hit the smart model."""
    story_id = f"story_accept_{score}"
    make_story(story_id, 1234567890)

    result = analyzer.process_one(_score_payload(tmp_path, story_id, score))

    assert result["decision"] == "accept"
    assert result["smart_used"] is False
    assert fake_ai.smart_calls == 0, "the smart model was called for a 7+ score"
    assert result["final_score"] == score, "final_score must be the cheap score"

    with db() as session:
        analysis = session.get(StoryAnalysis, story_id)
    assert analysis.smart_result is None
    assert analysis.smart_score is None
    assert analysis.final_score == score
    # A 7+ story skips the smart model, but SPEC 7.6 still requires an
    # explanation in the Slack message - it must fall back to cheap signals
    # rather than reaching the notifier empty.
    assert analysis.ai_explanation, "a 7+ lead reached Slack with no explanation"
    assert str(score) in analysis.ai_explanation


@pytest.mark.parametrize("score", [0, 1, 2, 3, 4])
def test_scores_zero_to_four_reject_without_calling_the_smart_model(
    db, fixture_targets, make_story, analyzer, fake_ai, fake_queues, tmp_path, score
):
    """SPEC 7.4: 0-4 rejects, writes the result, and stops - no smart model, no bizcheck."""
    story_id = f"story_reject_{score}"
    make_story(story_id, 1234567890)

    result = analyzer.process_one(_score_payload(tmp_path, story_id, score))

    assert result["decision"] == "reject"
    assert fake_ai.smart_calls == 0, "the smart model was called for a rejected score"
    assert fake_queues[Q_BIZCHECK].depth() == 0, (
        "a rejected story reached business checks"
    )

    with db() as session:
        analysis = session.get(StoryAnalysis, story_id)
    assert analysis is not None, "SPEC 7.4: reject still writes the result"
    assert analysis.cheap_score == score
    assert analysis.final_score == score


def test_non_rejected_stories_are_enqueued_for_business_checks(
    db, fixture_targets, make_story, analyzer, fake_queues, tmp_path
):
    make_story("story_to_bizcheck", 1234567890)
    analyzer.process_one(_score_payload(tmp_path, "story_to_bizcheck", 9))

    queued = drain(fake_queues[Q_BIZCHECK])
    assert len(queued) == 1
    assert queued[0]["story_id"] == "story_to_bizcheck"
    assert queued[0]["final_score"] == 9


def test_route_boundaries_come_from_config(analyzer, settings):
    assert analyzer.smart_min == settings.smart_model_score_min == 5
    assert analyzer.smart_max == settings.smart_model_score_max == 6
    assert [analyzer.route(s) for s in range(11)] == [
        "reject", "reject", "reject", "reject", "reject",
        "smart", "smart",
        "accept", "accept", "accept", "accept",
    ]


# --- temp file cleanup (SPEC 7.4 step 4, SPEC section 11) ---------------------


def test_no_temp_files_remain_after_a_forced_mid_pipeline_exception(
    db, fixture_targets, make_story, analyzer, fake_ai, tmp_path, monkeypatch
):
    """SPEC Phase 3 acceptance: no temp files remain after a forced exception.

    The exception is injected into the cheap-model call - i.e. after the file
    exists and OCR has run, but before any analysis is written. That is the path
    most likely to leak media.
    """
    story_id = "story_boom"
    make_story(story_id, 1234567890)
    media_path = _make_image(tmp_path, "story_boom_score=8.png")
    assert Path(media_path).is_file()

    def _explode(*_args, **_kwargs):
        raise RuntimeError("forced mid-pipeline failure")

    monkeypatch.setattr(fake_ai, "call_cheap", _explode)

    result = analyzer.process_one({"story_id": story_id, "temp_path": media_path})

    assert result["state"] == "failed"
    assert "forced mid-pipeline failure" in result["reason"]
    assert not Path(media_path).exists(), (
        "the temp media file survived a mid-pipeline exception"
    )
    assert list(tmp_path.iterdir()) == [], f"temp dir not clean: {list(tmp_path.iterdir())}"

    with db() as session:
        assert session.get(Story, story_id).pipeline_state == "failed"


def test_temp_file_is_deleted_even_when_the_db_write_explodes(
    db, fixture_targets, make_story, analyzer, tmp_path, monkeypatch
):
    """A failure in the *final* step must not leave media on disk either."""
    story_id = "story_db_boom"
    make_story(story_id, 1234567890)
    media_path = _make_image(tmp_path, "story_db_boom_score=9.png")

    def _explode(**_kwargs):
        raise RuntimeError("write_analysis exploded")

    monkeypatch.setattr(analyzer, "_write_analysis", _explode)

    result = analyzer.process_one({"story_id": story_id, "temp_path": media_path})

    assert result["state"] == "failed"
    assert not Path(media_path).exists()


def test_temp_file_is_deleted_on_the_happy_path(
    db, fixture_targets, make_story, analyzer, tmp_path
):
    story_id = "story_happy"
    make_story(story_id, 1234567890)
    media_path = _make_image(tmp_path, "story_happy_score=8.png")

    analyzer.process_one({"story_id": story_id, "temp_path": media_path})

    assert not Path(media_path).exists()
    assert list(tmp_path.iterdir()) == []


def test_delete_temp_file_never_raises(tmp_path):
    """It runs inside a `finally` - it must never mask the original exception."""
    delete_temp_file(None)
    delete_temp_file(str(tmp_path / "does_not_exist.jpg"))
    delete_temp_file(str(tmp_path))  # a directory: OSError, swallowed and logged


# --- Pydantic validation (SPEC 7.4) -------------------------------------------


CHEAP_FIELDS = {
    "explicit_purchase_intent": True,
    "seeking_contractor": True,
    "allowed_category": True,
    "is_spam": False,
    "is_offering_services": False,
    "asking_for_free": False,
    "complaint_only": False,
}


def test_out_of_range_scores_are_rejected_by_the_schema():
    """SPEC 7.4: score is 0-10. An 11 or a -1 must never become a lead."""
    for bad in (11, -1, 99):
        with pytest.raises(ValidationError):
            CheapResult(score=bad, **CHEAP_FIELDS)
    for bad in (11, -1):
        with pytest.raises(ValidationError):
            SmartResult(confirmed=True, final_score=bad, explanation="Fine.")

    assert CheapResult(score=0, **CHEAP_FIELDS).score == 0
    assert CheapResult(score=10, **CHEAP_FIELDS).score == 10


def test_schema_forbids_unknown_fields_and_missing_required_ones():
    with pytest.raises(ValidationError):
        CheapResult(score=5, hallucinated_field="x", **CHEAP_FIELDS)
    with pytest.raises(ValidationError):
        CheapResult(score=5)  # missing required booleans


def test_smart_explanation_is_capped_at_two_sentences():
    """SPEC 7.4: `explanation` is at most 2 sentences. Enforced, not trusted."""
    ok = SmartResult(
        confirmed=True, final_score=8, explanation="First sentence. Second sentence."
    )
    assert ok.explanation.startswith("First")
    with pytest.raises(ValidationError):
        SmartResult(
            confirmed=True,
            final_score=8,
            explanation="One. Two. Three.",
        )


def test_malformed_model_output_retries_once_then_raises(monkeypatch):
    """SPEC 7.4: on parse failure retry ONCE with a JSON-only nudge, then fail.

    Never guess at malformed output - two strikes and the analyzer marks the
    story `failed`.
    """
    from stories_monitor.ai.client import AnthropicAIClient

    client = AnthropicAIClient.__new__(AnthropicAIClient)
    client.cheap_model = "cheap"
    client.smart_model = "smart"

    calls: list[list[dict]] = []

    def _bad_then_bad(*, model, stage, system, messages, max_tokens):
        calls.append(messages)
        return "I'm afraid I can't do that."

    monkeypatch.setattr(client, "_create", _bad_then_bad)
    monkeypatch.setattr(
        "stories_monitor.ai.client.encode_image", lambda p: ("image/png", "AA==")
    )

    with pytest.raises(AIClientError, match="failed validation twice"):
        client.call_cheap("/tmp/x.png", "some ocr text")

    assert len(calls) == 2, "exactly one retry, no more and no fewer"
    retry_roles = [m["role"] for m in calls[1]]
    assert retry_roles == ["user", "assistant", "user"], (
        "the retry must replay the bad reply plus a JSON-only nudge"
    )


def test_malformed_output_succeeds_when_the_retry_is_valid(monkeypatch):
    from stories_monitor.ai.client import AnthropicAIClient

    client = AnthropicAIClient.__new__(AnthropicAIClient)
    client.cheap_model = "cheap"
    client.smart_model = "smart"

    good = '{"score": 6, "explicit_purchase_intent": true, "seeking_contractor": true, ' \
           '"allowed_category": true, "is_spam": false, "is_offering_services": false, ' \
           '"asking_for_free": false, "complaint_only": false, "service_category": "plumbing", ' \
           '"geography": "Boston", "email_visible": null}'
    replies = iter(["not json at all", good])

    monkeypatch.setattr(
        client, "_create", lambda **_kw: next(replies)
    )
    monkeypatch.setattr(
        "stories_monitor.ai.client.encode_image", lambda p: ("image/png", "AA==")
    )

    result = client.call_cheap("/tmp/x.png", "")
    assert isinstance(result, CheapResult)
    assert result.score == 6
    assert result.service_category == "plumbing"


def test_out_of_range_score_from_the_model_fails_validation_twice(monkeypatch):
    """A syntactically valid but out-of-range score is still refused."""
    from stories_monitor.ai.client import AnthropicAIClient

    client = AnthropicAIClient.__new__(AnthropicAIClient)
    client.cheap_model = "cheap"
    client.smart_model = "smart"

    out_of_range = (
        '{"score": 42, "explicit_purchase_intent": true, "seeking_contractor": true, '
        '"allowed_category": true, "is_spam": false, "is_offering_services": false, '
        '"asking_for_free": false, "complaint_only": false}'
    )
    monkeypatch.setattr(client, "_create", lambda **_kw: out_of_range)
    monkeypatch.setattr(
        "stories_monitor.ai.client.encode_image", lambda p: ("image/png", "AA==")
    )

    with pytest.raises(AIClientError, match="failed validation twice"):
        client.call_cheap("/tmp/x.png", "")


def test_analyzer_marks_a_story_failed_when_the_client_gives_up(
    db, fixture_targets, make_story, analyzer, fake_ai, tmp_path, monkeypatch
):
    story_id = "story_unparseable"
    make_story(story_id, 1234567890)
    media_path = _make_image(tmp_path, "story_unparseable_score=8.png")

    def _give_up(*_a, **_k):
        raise AIClientError("cheap model output failed validation twice: nope")

    monkeypatch.setattr(fake_ai, "call_cheap", _give_up)

    result = analyzer.process_one({"story_id": story_id, "temp_path": media_path})

    assert result["state"] == "failed"
    with db() as session:
        assert session.get(Story, story_id).pipeline_state == "failed"
        assert session.get(StoryAnalysis, story_id) is None, (
            "a story that never produced valid output must not get an analysis row"
        )
    assert not Path(media_path).exists()


def test_json_parsing_tolerates_fences_and_prose():
    assert strip_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert parse_json_object('```\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Here you go: {"a": 1} hope that helps') == {"a": 1}
    with pytest.raises(AIClientError):
        parse_json_object("no object here")
    with pytest.raises(AIClientError):
        parse_json_object("[1, 2, 3]")


def test_fake_ai_client_score_is_deterministic():
    """The routing tests depend on this - assert it rather than assuming it."""
    assert FakeAIClient.score_for("/tmp/x_score=6.png", "") == 6
    assert FakeAIClient.score_for("/tmp/x.png", "score=3") == 3
    stable = FakeAIClient.score_for("/tmp/nothing.png", "plain text")
    assert stable == FakeAIClient.score_for("/tmp/nothing.png", "plain text")
    assert 0 <= stable <= 10


def test_analyzer_survives_a_missing_ocr_backend(
    db, fixture_targets, make_story, analyzer, tmp_path, monkeypatch
):
    """No tesseract in this environment: OCRUnavailable must not lose the story."""
    from stories_monitor.ai.ocr import OCRUnavailable

    story_id = "story_no_ocr"
    make_story(story_id, 1234567890)
    media_path = _make_image(tmp_path, "story_no_ocr_score=8.png")

    def _unavailable(_path):
        raise OCRUnavailable("tesseract binary not found on PATH")

    monkeypatch.setattr(analyzer.ocr, "extract_text", _unavailable)

    result = analyzer.process_one({"story_id": story_id, "temp_path": media_path})

    assert result["state"] == "analyzed"
    with db() as session:
        assert session.get(StoryAnalysis, story_id).ocr_text is None


def test_analyzer_reprocessing_is_idempotent(
    db, fixture_targets, make_story, analyzer, tmp_path
):
    """`story_analysis` is upserted on story_id - re-processing never double-writes."""
    story_id = "story_reprocess"
    make_story(story_id, 1234567890)

    analyzer.process_one(_score_payload(tmp_path, story_id, 8))
    analyzer.process_one(_score_payload(tmp_path, story_id, 8))

    with db() as session:
        count = session.execute(
            sa.select(sa.func.count()).select_from(StoryAnalysis)
        ).scalar()
    assert count == 1
