"""The one write: following with browser cookies.

No network. `httpx.MockTransport` answers the request, which lets the exact
headers and body Instagram would see be asserted on - the point of the exercise,
since a follow that does not look like a browser's follow is what gets an
account action-blocked.
"""

from __future__ import annotations

import httpx
import pytest

from stories_monitor.transport import web as web_mod
from stories_monitor.transport.base import (
    ChallengeRequiredError,
    FeedbackRequiredError,
    LoginRequiredError,
    RateLimitedError,
    TransportError,
    UserNotFoundError,
)
from stories_monitor.transport.web import WebTransport

COOKIES = {
    "sessionid": "sess-1",
    "csrftoken": "csrf-1",
    "ds_user_id": "999",
    "ig_did": "did",
    "mid": "mid",
    "datr": "datr",
    "rur": "rur",
}


@pytest.fixture(autouse=True)
def _no_pauses(monkeypatch):
    """Skip the human-rhythm sleeps; they are tested in test_follow_rhythm."""
    monkeypatch.setattr(web_mod.time, "sleep", lambda *_: None)
    web_mod.close_all_connections()
    yield
    web_mod.close_all_connections()


def _transport(handler) -> WebTransport:
    """A WebTransport whose HTTP client is a mock, keyed to a unique account."""
    transport = WebTransport(dict(COOKIES))
    transport._client = httpx.Client(transport=httpx.MockTransport(handler))
    transport._client.headers.update({"X-CSRFToken": COOKIES["csrftoken"]})
    return transport


def _ok(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload)


class TestFollowRequestShape:
    def test_posts_to_the_friendships_create_endpoint(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["method"] = request.method
            return _ok({"friendship_status": {"following": True}})

        _transport(handler).user_follow(12345)

        assert seen["method"] == "POST"
        assert seen["url"] == "https://www.instagram.com/api/v1/friendships/create/12345/"

    def test_sends_the_csrf_token(self) -> None:
        """Without it every write is a 403, though reads carry on working."""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return _ok({"friendship_status": {"following": True}})

        _transport(handler).user_follow(1)
        assert seen["x-csrftoken"] == "csrf-1"

    def test_sends_browser_write_headers(self) -> None:
        """A POST without Origin, or with a JSON body, is a shape no browser makes."""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["origin"] = request.headers.get("origin")
            seen["content_type"] = request.headers.get("content-type")
            seen["body"] = request.content.decode()
            return _ok({"friendship_status": {"following": True}})

        _transport(handler).user_follow(777)

        assert seen["origin"] == "https://www.instagram.com"
        assert seen["content_type"] == "application/x-www-form-urlencoded"
        assert "user_id=777" in seen["body"]

    def test_referer_points_at_the_target_profile(self) -> None:
        """A person clicks Follow from the profile they are looking at."""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["referer"] = request.headers.get("referer")
            return _ok({"friendship_status": {"following": True}})

        _transport(handler).user_follow(5, username="somebody")
        assert seen["referer"] == "https://www.instagram.com/somebody/"

    def test_the_internal_referer_key_never_reaches_the_wire(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert "__referer" not in request.content.decode()
            return _ok({"friendship_status": {"following": True}})

        _transport(handler).user_follow(5, username="somebody")

    def test_a_session_without_csrf_refuses_to_write(self) -> None:
        """Fail loudly up front rather than one mystery 403 at a time."""
        cookies = {k: v for k, v in COOKIES.items() if k != "csrftoken"}
        transport = WebTransport(cookies)
        transport._client = httpx.Client(
            transport=httpx.MockTransport(lambda r: _ok({}))
        )
        with pytest.raises(LoginRequiredError, match="csrftoken"):
            transport.user_follow(1)


class TestFollowOutcomes:
    def test_a_public_follow_reports_success(self) -> None:
        transport = _transport(lambda r: _ok({"friendship_status": {"following": True}}))
        assert transport.user_follow(1) is True

    def test_a_private_target_yields_a_pending_request(self) -> None:
        """Still success: the action landed and must not be retried."""
        transport = _transport(
            lambda r: _ok({"friendship_status": {"following": False, "outgoing_request": True}})
        )
        assert transport.user_follow(1) is True

    def test_a_refusal_without_a_status_is_an_error(self) -> None:
        transport = _transport(lambda r: _ok({"status": "fail", "message": "oops"}))
        with pytest.raises(TransportError, match="oops"):
            transport.user_follow(1)

    def test_a_missing_target_is_reported_as_such(self) -> None:
        transport = _transport(
            lambda r: _ok({"status": "fail", "message": "User not found"})
        )
        with pytest.raises(UserNotFoundError):
            transport.user_follow(1)


class TestBlockHandling:
    def test_feedback_required_is_not_read_as_a_dead_session(self) -> None:
        """The whole point: an action block must not disable a working session."""
        transport = _transport(
            lambda r: httpx.Response(400, json={"message": "feedback_required"})
        )
        with pytest.raises(FeedbackRequiredError):
            transport.user_follow(1)

    def test_action_blocked_wording_is_caught_too(self) -> None:
        transport = _transport(
            lambda r: httpx.Response(400, json={"message": "action_blocked", "status": "fail"})
        )
        with pytest.raises(FeedbackRequiredError):
            transport.user_follow(1)

    def test_a_checkpoint_is_surfaced_for_a_human(self) -> None:
        transport = _transport(
            lambda r: httpx.Response(400, json={"message": "checkpoint_required"})
        )
        with pytest.raises(ChallengeRequiredError):
            transport.user_follow(1)

    def test_a_throttle_is_reported_as_a_throttle(self) -> None:
        transport = _transport(
            lambda r: httpx.Response(429, text="Please wait a few minutes before you try again")
        )
        with pytest.raises(RateLimitedError):
            transport.user_follow(1)


class TestCsrfRotation:
    def test_a_rotated_token_is_adopted(self) -> None:
        """Instagram rotates csrftoken; presenting the stale one fails every write."""
        responses = iter(
            [
                httpx.Response(
                    200,
                    json={"friendship_status": {"following": True}},
                    headers={"set-cookie": "csrftoken=csrf-2; Path=/"},
                ),
                httpx.Response(200, json={"friendship_status": {"following": True}}),
            ]
        )
        seen: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("x-csrftoken"))
            return next(responses)

        transport = _transport(handler)
        transport.user_follow(1)
        transport.user_follow(2)

        assert seen[0] == "csrf-1"
        assert seen[1] == "csrf-2", "the rotated token must be used on the next write"
