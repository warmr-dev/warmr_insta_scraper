"""SPEC Phase 4 acceptance criteria (SPEC section 9).

*Accept:* an approved lead posts once; running the notifier twice posts nothing
the second time.

Plus the SPEC 7.5 check chain (order, short-circuiting, reject reasons) and the
SPEC 7.5 approval rule: `approved` requires final_score >= 7 AND every check
passing - the AI score alone is never sufficient.

Slack runs through `StdoutSlackNotifier` (SPEC section 4: fixture mode writes to
stdout); no token, no network.
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa
from conftest import drain

from stories_monitor.config import Q_NOTIFY
from stories_monitor.db.models import BusinessCheck, SlackDelivery, Story
from stories_monitor.notify.slack import LeadMessage, StdoutSlackNotifier, get_notifier
from stories_monitor.vendors.stub import StubVendorRepository
from stories_monitor.workers.bizcheck import (
    REASON_COMMUNITY_CONFLICT,
    REASON_FORWARDING_BLOCKED,
    REASON_GEO_NOT_SERVICEABLE,
    REASON_LOW_SCORE,
    REASON_LOW_SERVICE_FIT,
    REASON_NO_VENDOR,
    STATUS_APPROVED,
    STATUS_REJECTED,
    BizChecker,
)
from stories_monitor.workers.notifier import Notifier

# The stub vendor fixture (fixtures/vendors.json) is shaped to exercise each branch:
#   plumbing   -> vnd_plumb_001, fit 88, community boston-north, geo Boston/Cambridge/Somerville
#   landscaping-> vnd_land_003,  fit 62  (below the 70 threshold)
#   moving     -> vnd_move_004,  community boston-north
#   electrical -> vnd_elec_005,  geo Boston/Everett only
#   cleaning   -> vnd_clean_006, accepts_forwarded_leads = false
#   painting   -> vnd_paint_008, active = false (so no vendor matches)


def _analysis(
    *,
    final_score: int = 9,
    service_category: str | None = "plumbing",
    geography: str | None = "Boston",
    community: str | None = None,
    intent_type: str | None = None,
) -> dict:
    cheap: dict = {"score": final_score, "geography": geography}
    if community is not None:
        cheap["community"] = community
    return {
        "ocr_text": "Need a plumber in Boston asap, DM me",
        "cheap_score": final_score,
        "cheap_result": cheap,
        "final_score": final_score,
        "service_category": service_category,
        "intent_type": intent_type,
        "ai_explanation": "Explicit request for a plumber. Clear hiring intent.",
        "analyzed_at": dt.datetime.now(dt.UTC),
    }


@pytest.fixture()
def checker(fake_queues):
    """A BizChecker on the committed vendor stub, with in-memory queues."""
    return BizChecker(repository=StubVendorRepository())


def _story(make_story, story_id: str, **analysis_kwargs) -> str:
    return make_story(
        story_id,
        1234567890,
        pipeline_state="analyzed",
        analysis=_analysis(**analysis_kwargs),
    )


# --- the check chain (SPEC 7.5) -----------------------------------------------


def test_check_one_no_matching_vendor_short_circuits(
    db, fixture_targets, make_story, checker
):
    """Check 1: no vendor for the category -> reject, nothing else is evaluated."""
    _story(make_story, "s_no_vendor", service_category="underwater_basket_weaving")

    outcome = checker.process_one("s_no_vendor")

    assert outcome.final_status == STATUS_REJECTED
    assert outcome.reject_reason == REASON_NO_VENDOR
    assert outcome.vendor_id is None
    # Later checks never ran, so their columns stay NULL.
    assert outcome.service_fit is None
    assert outcome.geo_ok is None
    assert outcome.community_conflict is None


def test_check_one_ignores_inactive_vendors(db, fixture_targets, make_story, checker):
    """`vnd_paint_008` is active=false, so `painting` has no vendor at all."""
    _story(make_story, "s_inactive", service_category="painting")
    outcome = checker.process_one("s_inactive")
    assert outcome.reject_reason == REASON_NO_VENDOR


def test_check_two_service_fit_below_threshold(
    db, fixture_targets, make_story, checker, settings
):
    """Check 2: Service Fit >= threshold (config, SPEC open question #4)."""
    _story(
        make_story, "s_low_fit", service_category="landscaping", geography="Cambridge"
    )

    outcome = checker.process_one("s_low_fit")

    assert outcome.final_status == STATUS_REJECTED
    assert outcome.reject_reason == REASON_LOW_SERVICE_FIT
    assert outcome.vendor_id == "vnd_land_003"
    assert outcome.service_fit == 62.0
    assert outcome.service_fit < settings.service_fit_threshold
    # Short-circuit: checks 3-5 never ran.
    assert outcome.community_conflict is None
    assert outcome.geo_ok is None


def test_check_three_community_conflict(db, fixture_targets, make_story, checker):
    """Check 3: lead community must differ from the vendor's."""
    _story(
        make_story,
        "s_community",
        service_category="moving",
        geography="Boston",
        community="boston-north",  # same as vnd_move_004
    )

    outcome = checker.process_one("s_community")

    assert outcome.final_status == STATUS_REJECTED
    assert outcome.reject_reason == REASON_COMMUNITY_CONFLICT
    assert outcome.vendor_id == "vnd_move_004"
    assert outcome.service_fit == 81.0, "check 2 ran and passed before check 3"
    assert outcome.community_conflict is True
    assert outcome.geo_ok is None, "check 4 must not run after check 3 fails"


def test_check_four_geography_not_serviceable(db, fixture_targets, make_story, checker):
    """Check 4: geography must be serviceable."""
    _story(
        make_story,
        "s_geo",
        service_category="electrical",
        geography="Worcester",  # vnd_elec_005 serves Boston/Everett only
    )

    outcome = checker.process_one("s_geo")

    assert outcome.final_status == STATUS_REJECTED
    assert outcome.reject_reason == REASON_GEO_NOT_SERVICEABLE
    assert outcome.vendor_id == "vnd_elec_005"
    assert outcome.community_conflict is False, "check 3 ran and passed"
    assert outcome.geo_ok is False


def test_check_five_forwarding_not_permitted(db, fixture_targets, make_story, checker):
    """Check 5: internal forwarding rules must permit the lead."""
    _story(
        make_story,
        "s_forwarding",
        service_category="cleaning",  # vnd_clean_006 accepts_forwarded_leads = false
        geography="Boston",
    )

    outcome = checker.process_one("s_forwarding")

    assert outcome.final_status == STATUS_REJECTED
    assert outcome.reject_reason == REASON_FORWARDING_BLOCKED
    assert outcome.vendor_id == "vnd_clean_006"
    assert outcome.geo_ok is True, "check 4 ran and passed before check 5"


def test_the_chain_short_circuits_in_spec_7_5_order(
    db, fixture_targets, make_story, monkeypatch
):
    """The five checks run in SPEC 7.5 order and stop at the first failure.

    Asserted by recording the repository call sequence: a story that fails
    check 3 must have called checks 1 and 2 and nothing after.
    """
    calls: list[str] = []
    repo = StubVendorRepository()

    def _record(name, fn):
        def wrapper(*args, **kwargs):
            calls.append(name)
            return fn(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(repo, "find_vendor_for_category",
                        _record("vendor", repo.find_vendor_for_category))
    monkeypatch.setattr(repo, "service_fit", _record("fit", repo.service_fit))
    monkeypatch.setattr(repo, "vendor_community", _record("community", repo.vendor_community))
    monkeypatch.setattr(repo, "serves_geography", _record("geo", repo.serves_geography))
    monkeypatch.setattr(repo, "forwarding_allowed",
                        _record("forwarding", repo.forwarding_allowed))

    checker = BizChecker(repository=repo)

    # Fails at check 3 (community).
    _story(
        make_story, "s_order", service_category="moving",
        geography="Boston", community="boston-north",
    )
    checker.process_one("s_order")
    assert calls == ["vendor", "fit", "community"], (
        f"chain did not short-circuit in SPEC 7.5 order: {calls}"
    )

    # A fully passing lead runs all five, in order.
    calls.clear()
    _story(make_story, "s_order_ok", service_category="plumbing", geography="Boston")
    checker.process_one("s_order_ok")
    assert calls == ["vendor", "fit", "community", "geo", "forwarding"]


# --- approval requires BOTH the chain and the score ---------------------------


def test_approved_requires_all_checks_and_a_score_of_at_least_seven(
    db, fixture_targets, make_story, checker, settings
):
    """SPEC 7.5: `approved` only if final_score >= 7 AND every check passed."""
    _story(make_story, "s_approved", final_score=7, service_category="plumbing")

    outcome = checker.process_one("s_approved")

    assert outcome.final_status == STATUS_APPROVED
    assert outcome.reject_reason is None
    assert settings.approval_score_min == 7

    with db() as session:
        row = session.get(BusinessCheck, "s_approved")
        story = session.get(Story, "s_approved")
    assert row.final_status == STATUS_APPROVED
    assert row.vendor_id == "vnd_plumb_001"
    assert float(row.service_fit) == 88.0
    assert row.geo_ok is True
    assert row.community_conflict is False
    assert row.checked_at is not None
    assert story.pipeline_state == "checked"


def test_a_clean_chain_with_a_low_score_is_not_approved(
    db, fixture_targets, make_story, checker
):
    """The chain alone is never sufficient - the score gate is the last check."""
    _story(make_story, "s_low_score", final_score=6, service_category="plumbing")

    outcome = checker.process_one("s_low_score")

    assert outcome.final_status == STATUS_REJECTED
    assert outcome.reject_reason == REASON_LOW_SCORE
    # The chain itself passed - every column is populated.
    assert outcome.vendor_id == "vnd_plumb_001"
    assert outcome.geo_ok is True
    assert outcome.community_conflict is False


def test_a_nine_score_with_a_failing_check_is_not_approved(
    db, fixture_targets, make_story, checker
):
    """SPEC 7.5: the AI score alone is never sufficient.

    A 9 - comfortably above the approval threshold - must still be rejected when
    any business check fails.
    """
    _story(
        make_story,
        "s_nine_but_blocked",
        final_score=9,
        service_category="cleaning",  # forwarding blocked
        geography="Boston",
    )

    outcome = checker.process_one("s_nine_but_blocked")

    assert outcome.final_status != STATUS_APPROVED, (
        "a 9-score lead was approved despite a failing business check"
    )
    assert outcome.final_status == STATUS_REJECTED
    assert outcome.reject_reason == REASON_FORWARDING_BLOCKED


def test_only_approved_leads_are_enqueued_for_slack(
    db, fixture_targets, make_story, checker, fake_queues
):
    _story(make_story, "s_yes", final_score=9, service_category="plumbing")
    _story(make_story, "s_no", final_score=9, service_category="cleaning")

    checker.process_one("s_yes")
    checker.process_one("s_no")

    queued = [item["story_id"] for item in drain(fake_queues[Q_NOTIFY])]
    assert queued == ["s_yes"]


def test_bizcheck_is_idempotent_on_rerun(db, fixture_targets, make_story, checker):
    _story(make_story, "s_rerun", final_score=9, service_category="plumbing")
    checker.process_one("s_rerun")
    checker.process_one("s_rerun")

    with db() as session:
        count = session.execute(
            sa.select(sa.func.count()).select_from(BusinessCheck)
        ).scalar()
    assert count == 1


# --- notifier idempotency (THE Phase 4 acceptance criterion) -------------------


class CountingStdoutNotifier(StdoutSlackNotifier):
    """StdoutSlackNotifier with a send counter, so 'posts once' is observable."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.sends: list[LeadMessage] = []

    def send(self, message: LeadMessage) -> str:
        self.sends.append(message)
        return super().send(message)


@pytest.fixture()
def approved_story(db, fixture_targets, make_story, checker):
    """A story that has passed the whole chain and is genuinely `approved`."""
    _story(make_story, "s_lead", final_score=9, service_category="plumbing")
    outcome = checker.process_one("s_lead")
    assert outcome.final_status == STATUS_APPROVED
    return "s_lead"


def test_an_approved_lead_posts_once_and_a_second_run_posts_nothing(
    db, approved_story, fake_queues, capsys
):
    """SPEC Phase 4 acceptance criterion, asserted on the notifier's send count."""
    slack = CountingStdoutNotifier()
    notifier = Notifier(notifier=slack, sleep=lambda _s: None)

    first = notifier.process_one(approved_story)
    assert first.status == "sent"
    assert first.skipped is False
    assert len(slack.sends) == 1, "the approved lead did not post exactly once"

    second = notifier.process_one(approved_story)

    assert second.skipped is True, "the second run was not suppressed"
    assert len(slack.sends) == 1, (
        "running the notifier twice posted the lead a second time"
    )

    # A brand-new Notifier instance (i.e. a restarted process) is also suppressed:
    # the guarantee lives in the slack_deliveries primary key, not in memory.
    slack_two = CountingStdoutNotifier()
    third = Notifier(notifier=slack_two, sleep=lambda _s: None).process_one(approved_story)
    assert third.skipped is True
    assert slack_two.sends == []

    with db() as session:
        rows = session.execute(sa.select(SlackDelivery)).scalars().all()
        story = session.get(Story, approved_story)
    assert len(rows) == 1, "slack_deliveries must hold exactly one row per story"
    assert rows[0].status == "sent"
    assert rows[0].slack_ts
    assert rows[0].attempts == 1
    assert rows[0].sent_at is not None
    assert story.pipeline_state == "sent"


def test_the_delivery_claim_is_written_before_slack_is_called(
    db, approved_story, monkeypatch
):
    """SPEC 7.6: the 'pending' row is inserted and committed BEFORE the send.

    A crash between send and commit must not re-post, so the claim - not the
    delivery status - is what gates the send.
    """
    observed: dict = {}

    class CrashingNotifier(StdoutSlackNotifier):
        def send(self, message):
            with db() as session:
                row = session.get(SlackDelivery, message.story_id)
                observed["claim_visible"] = row is not None
                observed["status_at_send"] = row.status if row else None
            raise RuntimeError("slack blew up right after we claimed the story")

    notifier = Notifier(notifier=CrashingNotifier(), sleep=lambda _s: None)
    result = notifier.process_one(approved_story)

    assert observed["claim_visible"] is True, "the claim was not committed before sending"
    assert observed["status_at_send"] == "pending"
    assert result.status == "failed"

    # And a later run still does not re-post: the claim survives the failure.
    slack = CountingStdoutNotifier()
    retry = Notifier(notifier=slack, sleep=lambda _s: None).process_one(approved_story)
    assert retry.skipped is True
    assert slack.sends == []


def test_notifier_retries_with_backoff_then_marks_failed(db, approved_story):
    """SPEC 7.6: exponential backoff, 5 attempts, then `failed` plus an alert."""
    attempts = {"n": 0}
    slept: list[float] = []

    class AlwaysFailing(StdoutSlackNotifier):
        def send(self, message):
            attempts["n"] += 1
            raise RuntimeError("slack is down")

    notifier = Notifier(notifier=AlwaysFailing(), sleep=slept.append)
    result = notifier.process_one(approved_story)

    assert result.status == "failed"
    assert attempts["n"] == 5, "SPEC 7.6 asks for 5 attempts"
    assert len(slept) == 4, "no sleep after the final attempt"
    assert all(s >= 0 for s in slept)

    with db() as session:
        row = session.get(SlackDelivery, approved_story)
        story = session.get(Story, approved_story)
    assert row.status == "failed"
    assert row.attempts == 5
    assert row.last_error
    assert story.pipeline_state == "failed"


def test_a_non_approved_story_never_reaches_slack(
    db, fixture_targets, make_story, checker
):
    """SPEC 7.6: only `final_status = 'approved'` reaches Slack.

    Enforced at send time as well as at enqueue time - the queue is not a
    trustworthy authorisation boundary.
    """
    _story(make_story, "s_rejected", final_score=9, service_category="cleaning")
    checker.process_one("s_rejected")

    slack = CountingStdoutNotifier()
    result = Notifier(notifier=slack, sleep=lambda _s: None).process_one("s_rejected")

    assert result.skipped is True
    assert slack.sends == []
    with db() as session:
        assert session.get(SlackDelivery, "s_rejected") is None


# --- Slack in fixture mode writes to stdout -----------------------------------


def test_slack_in_fixture_mode_writes_to_stdout(db, approved_story, settings, capsys):
    """SPEC section 4: Slack in fixture mode writes to stdout."""
    assert settings.is_fixture_mode
    assert isinstance(get_notifier(), StdoutSlackNotifier), (
        "fixture mode must not build a live Slack client"
    )

    result = Notifier(notifier=get_notifier(), sleep=lambda _s: None).process_one(
        approved_story
    )
    assert result.status == "sent"

    out = capsys.readouterr().out
    assert "=== SLACK" in out
    assert "[fixture mode]" in out
    # SPEC 7.6 message fields.
    assert "New qualified lead" in out
    assert "acme_dental" in out                      # username
    assert "instagram.com/acme_dental" in out        # Instagram URL
    assert "plumbing" in out                         # service category
    assert "9/10" in out                             # final score
    assert "Explicit request for a plumber." in out  # AI explanation
    assert "Story published:" in out                 # story publish time
    assert "vnd_plumb_001" in out                    # business check summary
    assert "service fit 88" in out


def test_slack_stdout_message_never_contains_a_token(db, approved_story, capsys):
    Notifier(notifier=StdoutSlackNotifier(), sleep=lambda _s: None).process_one(
        approved_story
    )
    out = capsys.readouterr().out
    assert "xoxb" not in out
    assert "Authorization" not in out


def test_lead_message_renders_every_spec_7_6_field():
    message = LeadMessage(
        story_id="s1",
        username="acme_dental",
        instagram_url="https://www.instagram.com/acme_dental/",
        service_category="plumbing",
        final_score=9,
        ai_explanation="Wants a plumber.",
        taken_at=dt.datetime(2026, 8, 6, 10, 0, tzinfo=dt.UTC),
        vendor_id="vnd_plumb_001",
        service_fit=88.0,
        geo_ok=True,
        community_conflict=False,
    )
    text = message.to_text()
    for expected in (
        "@acme_dental",
        "https://www.instagram.com/acme_dental/",
        "plumbing",
        "9/10",
        "2026-08-06 10:00 UTC",
        "Wants a plumber.",
        "vnd_plumb_001",
        "service fit 88",
        "geo ok",
        "no community conflict",
    ):
        assert expected in text, f"missing {expected!r} from the Slack message"

    blocks = message.to_blocks()
    assert blocks[0]["type"] == "header"
