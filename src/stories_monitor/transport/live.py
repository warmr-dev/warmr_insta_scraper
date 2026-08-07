"""LiveTransport - real Instagram calls via instagrapi.

This is the ONLY module in the codebase allowed to import instagrapi (SPEC section 11).
Every instagrapi exception is translated into a `base.py` transport exception before it
crosses the package boundary.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, TypeVar

import httpx
from instagrapi import Client as _BaseClient
from instagrapi import exceptions as ig_exc

from ..config import get_settings
from ..logging_setup import get_logger
from .base import (
    ChallengeRequiredError,
    FeedbackRequiredError,
    InstagramTransport,
    LoginRequiredError,
    PleaseWaitError,
    PrivateAccountError,
    ProxyBlockedError,
    RateLimitedError,
    StoryItem,
    TransportError,
    TrayEntry,
    TrayResponse,
    UserNotFoundError,
)

log = get_logger(__name__)

T = TypeVar("T")


# --- instagrapi exception lookup -------------------------------------------------
#
# instagrapi renames/moves exception classes between releases, so every name is
# resolved defensively. A missing name falls back to a private sentinel class that
# can never be raised, which keeps the mapping table total.


class _Unraisable(Exception):
    """Placeholder for an instagrapi exception absent from the installed version."""


def _exc(*names: str) -> type[BaseException]:
    for name in names:
        candidate = getattr(ig_exc, name, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            return candidate
    return _Unraisable


_IG_CHALLENGE_REQUIRED = _exc("ChallengeRequired")
_IG_CHALLENGE_ERROR = _exc("ChallengeError")
_IG_LOGIN_REQUIRED = _exc("LoginRequired")
_IG_CLIENT_LOGIN_REQUIRED = _exc("ClientLoginRequired")
_IG_FEEDBACK_REQUIRED = _exc("FeedbackRequired")
_IG_PLEASE_WAIT = _exc("PleaseWaitFewMinutes")
_IG_THROTTLED = _exc("ClientThrottledError")
_IG_RATE_LIMIT = _exc("RateLimitError")
_IG_PRIVATE_ACCOUNT = _exc("PrivateAccount")
_IG_PRIVATE_ERROR = _exc("PrivateError")
_IG_USER_NOT_FOUND = _exc("UserNotFound")
_IG_PROXY_BLOCKED = _exc("ProxyAddressIsBlocked")

# Ordered most-specific first: the first isinstance hit wins.
_ERROR_MAP: tuple[tuple[type[BaseException], type[TransportError]], ...] = (
    (_IG_CHALLENGE_REQUIRED, ChallengeRequiredError),
    (_IG_CHALLENGE_ERROR, ChallengeRequiredError),
    (_IG_LOGIN_REQUIRED, LoginRequiredError),
    (_IG_CLIENT_LOGIN_REQUIRED, LoginRequiredError),
    (_IG_FEEDBACK_REQUIRED, FeedbackRequiredError),
    (_IG_PLEASE_WAIT, PleaseWaitError),
    (_IG_THROTTLED, RateLimitedError),
    (_IG_RATE_LIMIT, RateLimitedError),
    (_IG_PROXY_BLOCKED, ProxyBlockedError),
    (_IG_USER_NOT_FOUND, UserNotFoundError),
    (_IG_PRIVATE_ACCOUNT, PrivateAccountError),
    (_IG_PRIVATE_ERROR, PrivateAccountError),
)


def _translate(exc: BaseException) -> TransportError:
    """Map an instagrapi exception onto its transport equivalent."""
    if isinstance(exc, TransportError):
        return exc
    for ig_type, transport_type in _ERROR_MAP:
        if ig_type is not _Unraisable and isinstance(exc, ig_type):
            return transport_type(str(exc) or type(exc).__name__)
    return TransportError(f"{type(exc).__name__}: {exc}")


def translate_errors(fn: Callable[..., T]) -> Callable[..., T]:
    """Single reusable boundary: no method duplicates this try/except."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        try:
            return fn(*args, **kwargs)
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001 - deliberate total translation
            translated = _translate(exc)
            log.warning(
                "transport_error",
                method=fn.__name__,
                error=type(translated).__name__,
                original=type(exc).__name__,
            )
            raise translated from exc

    return wrapper


# --- client -----------------------------------------------------------------------


class _NoChallengeClient(_BaseClient):
    """instagrapi Client that refuses to auto-solve challenges.

    instagrapi calls `challenge_resolve` from inside `private_request` whenever
    Instagram returns a challenge. Auto-solving turns a recoverable account into a
    banned one, so SPEC 7.8 forbids it: raise instead and alert a human.
    """

    def challenge_resolve(self, last_json: dict[str, Any]) -> bool:  # type: ignore[override]
        raise ChallengeRequiredError(
            "challenge required - human intervention needed (SPEC 7.8)"
        )


class LiveTransport(InstagramTransport):
    """Real instagrapi-backed transport."""

    def __init__(
        self,
        username: str | None = None,
        device_settings: dict[str, Any] | None = None,
        proxy_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.username = username
        self.proxy_url = proxy_url
        self._settings = get_settings()
        self.client = _NoChallengeClient()

        # SPEC section 8: device settings are generated once per account and stored in
        # the DB. Apply what we were given; NEVER regenerate here.
        if device_settings:
            self.client.set_device(dict(device_settings), reset=False)
            user_agent = device_settings.get("user_agent")
            if user_agent:
                self.client.set_user_agent(user_agent)

        # SPEC section 8: one account = one proxy, for the account's whole life.
        # Never rotate it, never share it.
        if proxy_url:
            self.client.set_proxy(proxy_url)

        self._request_timeout = int(kwargs.get("request_timeout", 30))

    # --- session ---

    @translate_errors
    def login(self, username: str, password: str, **kwargs: Any) -> dict[str, Any]:
        self.username = username
        self.client.login(username, password, **kwargs)
        return self.client.get_settings()

    @translate_errors
    def load_session(self, session_json: dict[str, Any]) -> None:
        self.client.set_settings(dict(session_json))
        # set_settings restores the stored device; re-apply the bound proxy because
        # instagrapi drops it along with the rest of the client state.
        if self.proxy_url:
            self.client.set_proxy(self.proxy_url)

    @translate_errors
    def dump_session(self) -> dict[str, Any]:
        return self.client.get_settings()

    # --- reads ---

    @translate_errors
    def reels_tray(
        self, *, cold_start: bool = False, max_id: str | None = None
    ) -> TrayResponse:
        """One request covering every following with an active story (SPEC 7.1)."""
        data: dict[str, Any] = {
            "reason": "cold_start" if cold_start else "pull_to_refresh",
            "timezone_offset": "0",
            "tray_session_id": self.client.generate_uuid(),
            "request_id": self.client.generate_uuid(),
            "_uuid": self.client.uuid,
            "page_size": str(self._settings.tray_page_size),
        }
        # Cursor pagination for a truncated tray (SPEC 7.2).
        if max_id:
            data["max_id"] = max_id

        response = self.client.private_request("feed/reels_tray/", data=data) or {}
        entries = [_parse_tray_entry(raw) for raw in (response.get("tray") or [])]
        log.info(
            "reels_tray",
            entry_count=len(entries),
            cold_start=cold_start,
            paginated=bool(max_id),
        )
        return TrayResponse(entries=entries, raw=response)

    @translate_errors
    def reels_media(self, user_ids: list[int]) -> dict[int, list[StoryItem]]:
        """Batch story fetch for up to ~50 user ids (SPEC 7.3)."""
        if not user_ids:
            return {}
        response = (
            self.client.private_request(
                "feed/reels_media/",
                data={
                    "user_ids": [str(u) for u in user_ids],
                    "source": "feed_timeline",
                    "_uuid": self.client.uuid,
                },
            )
            or {}
        )
        return _parse_reels_media(response)

    @translate_errors
    def user_friendship(self, user_id: int) -> dict[str, Any]:
        result = self.client.user_friendship_v1(str(user_id))
        if hasattr(result, "model_dump"):
            return result.model_dump()
        if hasattr(result, "dict"):
            return result.dict()
        return dict(result or {})

    # --- the only write, and only against our own account's follow graph ---

    @translate_errors
    def user_follow(self, user_id: int) -> bool:
        """True on a new follow / new outgoing request, False if already following.

        This is the only write endpoint in the system. SPEC section 11 forbids
        `media/seen/` and every other write against a target's content.
        """
        return bool(self.client.user_follow(str(user_id)))

    # --- media ---

    def download_media(self, url: str, dest_path: str) -> str:
        """Fetch media bytes through the account's proxy.

        Media URLs are short-lived: retry a 403 exactly once, then give up so the
        fetcher can mark the story failed rather than re-queue forever (SPEC 7.3).
        """
        proxy = self.proxy_url or None
        last_error: Exception | None = None
        for attempt in (1, 2):
            try:
                with httpx.Client(
                    proxy=proxy, timeout=self._request_timeout, follow_redirects=True
                ) as http:
                    resp = http.get(url)
                    if resp.status_code == 403 and attempt == 1:
                        log.warning("media_403_retry", dest=dest_path)
                        continue
                    resp.raise_for_status()
                    with open(dest_path, "wb") as fh:
                        fh.write(resp.content)
                    return dest_path
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if exc.response.status_code == 403 and attempt == 1:
                    continue
                break
            except httpx.HTTPError as exc:
                last_error = exc
                break
        raise TransportError(f"download failed for {dest_path}: {last_error}")

    def close(self) -> None:
        session = getattr(self.client, "private", None)
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - close must never raise
                pass


# --- response parsing --------------------------------------------------------------


def _parse_tray_entry(raw: dict[str, Any]) -> TrayEntry:
    user = raw.get("user") or {}
    entry_id = raw.get("id")
    if entry_id is None:
        entry_id = user.get("pk", "")
    return TrayEntry(
        id=str(entry_id),
        latest_reel_media=_as_int(raw.get("latest_reel_media")),
        seen=_as_int(raw.get("seen")),
        user=dict(user),
        items=list(raw.get("items") or []),
        raw=dict(raw),
    )


def _parse_reels_media(response: dict[str, Any]) -> dict[int, list[StoryItem]]:
    """Handle both observed shapes: `reels` (dict by user id) and `reels_media` (list)."""
    reels: list[tuple[str | None, dict[str, Any]]] = []

    reels_dict = response.get("reels")
    if isinstance(reels_dict, dict):
        reels.extend((key, value) for key, value in reels_dict.items() if isinstance(value, dict))

    reels_list = response.get("reels_media")
    if isinstance(reels_list, list):
        reels.extend((None, reel) for reel in reels_list if isinstance(reel, dict))

    out: dict[int, list[StoryItem]] = {}
    for key, reel in reels:
        user_id = _reel_user_id(reel, key)
        if user_id is None:
            continue
        items = [
            _parse_story_item(item, user_id)
            for item in (reel.get("items") or [])
            if isinstance(item, dict)
        ]
        out.setdefault(user_id, []).extend(item for item in items if item is not None)
    return out


def _reel_user_id(reel: dict[str, Any], key: str | None) -> int | None:
    # `reels` keys are user ids; `reels_media` entries carry the id on the reel itself.
    for candidate in (key, reel.get("id"), (reel.get("user") or {}).get("pk")):
        value = _as_int(candidate)
        if value is not None:
            return value
    return None


def _parse_story_item(item: dict[str, Any], user_id: int) -> StoryItem | None:
    story_id = item.get("pk") or item.get("id")
    if story_id is None:
        return None
    candidates = ((item.get("image_versions2") or {}).get("candidates")) or []
    return StoryItem(
        story_id=str(story_id),
        user_id=user_id,
        taken_at=_as_int(item.get("taken_at")) or 0,
        media_type=_as_int(item.get("media_type")) or 1,
        expiring_at=_as_int(item.get("expiring_at")),
        image_versions=[c for c in candidates if isinstance(c, dict)],
        raw=dict(item),
    )


def _as_int(value: Any) -> int | None:
    """Instagram returns ids and timestamps as ints, strings, or `1234_5678` pairs."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        head = value.split("_", 1)[0]
        try:
            return int(head)
        except ValueError:
            return None
    return None
