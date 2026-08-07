"""FixtureTransport - replays committed JSON, performs zero network I/O.

The whole pipeline must run end-to-end in fixture mode (SPEC section 4).
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from ..config import get_settings
from ..logging_setup import get_logger
from .base import (
    ChallengeRequiredError,
    FeedbackRequiredError,
    InstagramTransport,
    LoginRequiredError,
    PrivateAccountError,
    StoryItem,
    TransportError,
    TrayEntry,
    TrayResponse,
    UserNotFoundError,
)

log = get_logger(__name__)

# Fixture convention: {"__error__": "<key>"} makes reels_tray raise this exception.
ERROR_KEY = "__error__"
_ERROR_CODES: dict[str, type[TransportError]] = {
    "feedback_required": FeedbackRequiredError,
    "challenge_required": ChallengeRequiredError,
    "login_required": LoginRequiredError,
    "private_account": PrivateAccountError,
    "user_not_found": UserNotFoundError,
}

# A 1x1 opaque PNG - a real, decodable image so the analyzer can open the file.
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class FixtureTransport(InstagramTransport):
    """Replays `fixtures/reels_tray/*.json` and `fixtures/reels_media/*.json`.

    A scenario is a fixture basename without `.json`. Successive `reels_tray()` calls
    walk the queued scenario list; once exhausted, the last scenario repeats. Queuing
    the same scenario twice lets a test assert the second poll produces zero new work.
    """

    def __init__(
        self,
        username: str | None = None,
        device_settings: dict[str, Any] | None = None,
        proxy_url: str | None = None,
        scenario: str = "normal_tray",
        scenarios: list[str] | None = None,
        media_scenario: str = "photo_story",
        follow_result: bool = True,
        fixtures_dir: str | os.PathLike[str] | None = None,
        **kwargs: Any,
    ) -> None:
        self.username = username
        self.device_settings = dict(device_settings or {})
        self.proxy_url = proxy_url
        self.media_scenario = media_scenario
        self.follow_result = follow_result
        self._root = Path(fixtures_dir or get_settings().fixtures_dir)
        self._queue: list[str] = list(scenarios) if scenarios else [scenario]
        self._tray_calls = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # --- scenario control (test helpers) ---

    @property
    def scenario(self) -> str:
        """The scenario the next `reels_tray()` call will serve."""
        index = min(self._tray_calls, len(self._queue) - 1)
        return self._queue[index]

    def set_scenario(self, scenario: str) -> None:
        self._queue = [scenario]
        self._tray_calls = 0

    def queue_scenarios(self, scenarios: list[str]) -> None:
        """Serve these tray fixtures in order across successive `reels_tray()` calls."""
        self._queue = list(scenarios)
        self._tray_calls = 0

    def set_follow_result(self, value: bool) -> None:
        self.follow_result = value

    # --- fixture loading ---

    def load_fixture(self, kind: str, name: str) -> dict[str, Any]:
        path = self._root / kind / f"{name}.json"
        if not path.exists():
            raise TransportError(f"fixture not found: {path}")
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise TransportError(f"fixture is not an object: {path}")
        return data

    @staticmethod
    def _raise_if_error(payload: dict[str, Any], source: str) -> None:
        code = payload.get(ERROR_KEY)
        if not code:
            return
        exc_type = _ERROR_CODES.get(str(code), TransportError)
        raise exc_type(payload.get("message") or f"{code} (fixture {source})")

    # --- session: no-ops with plausible shapes ---

    def login(self, username: str, password: str, **kwargs: Any) -> dict[str, Any]:
        self.username = username
        self.calls.append(("login", {"username": username}))
        return self.dump_session()

    def load_session(self, session_json: dict[str, Any]) -> None:
        self.calls.append(("load_session", {}))

    def dump_session(self) -> dict[str, Any]:
        return {
            "uuids": {
                "phone_id": "00000000-0000-0000-0000-00000000f001",
                "uuid": "00000000-0000-0000-0000-00000000f002",
                "client_session_id": "00000000-0000-0000-0000-00000000f003",
                "advertising_id": "00000000-0000-0000-0000-00000000f004",
                "device_id": "android-f1x7u5e0000000",
            },
            "cookies": {},
            "last_login": 1754400000.0,
            "device_settings": self.device_settings
            or {
                "app_version": "269.0.0.18.75",
                "android_version": 26,
                "android_release": "8.0.0",
                "dpi": "480dpi",
                "resolution": "1080x1920",
                "manufacturer": "Xiaomi",
                "device": "capricorn",
                "model": "MI 5s Plus",
                "cpu": "qcom",
                "version_code": "314665256",
            },
            "user_agent": "Instagram 269.0.0.18.75 Android (26/8.0.0; 480dpi; 1080x1920; Xiaomi)",
            "country": "US",
            "locale": "en_US",
            "timezone_offset": 0,
            "authorization_data": {"ds_user_id": "9999999999"},
        }

    # --- reads ---

    def reels_tray(
        self, *, cold_start: bool = False, max_id: str | None = None
    ) -> TrayResponse:
        name = self.scenario
        self._tray_calls += 1
        self.calls.append(
            ("reels_tray", {"scenario": name, "cold_start": cold_start, "max_id": max_id})
        )
        payload = self.load_fixture("reels_tray", name)
        self._raise_if_error(payload, name)

        entries = [
            _parse_tray_entry(raw)
            for raw in (payload.get("tray") or [])
            if isinstance(raw, dict)
        ]
        log.info("fixture_reels_tray", scenario=name, entry_count=len(entries))
        return TrayResponse(entries=entries, raw=payload)

    def reels_media(self, user_ids: list[int]) -> dict[int, list[StoryItem]]:
        if not user_ids:
            return {}
        self.calls.append(("reels_media", {"user_ids": list(user_ids)}))
        payload = self.load_fixture("reels_media", self.media_scenario)
        self._raise_if_error(payload, self.media_scenario)

        all_reels = _parse_reels_media(payload)
        wanted = {int(u) for u in user_ids}
        return {uid: items for uid, items in all_reels.items() if uid in wanted}

    def user_friendship(self, user_id: int) -> dict[str, Any]:
        return {
            "following": True,
            "followed_by": False,
            "blocking": False,
            "muting": False,
            "is_private": False,
            "incoming_request": False,
            "outgoing_request": False,
            "is_bestie": False,
            "is_restricted": False,
        }

    def user_follow(self, user_id: int) -> bool:
        self.calls.append(("user_follow", {"user_id": int(user_id)}))
        return self.follow_result

    # --- media: real bytes, zero network ---

    def download_media(self, url: str, dest_path: str) -> str:
        parent = os.path.dirname(dest_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(dest_path, "wb") as fh:
            fh.write(_PNG_1X1)
        self.calls.append(("download_media", {"dest": dest_path}))
        return dest_path


# --- parsing (mirrors live.py so both transports yield identical objects) ---


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


def _parse_reels_media(payload: dict[str, Any]) -> dict[int, list[StoryItem]]:
    reels: list[tuple[str | None, dict[str, Any]]] = []

    reels_dict = payload.get("reels")
    if isinstance(reels_dict, dict):
        reels.extend(
            (key, value) for key, value in reels_dict.items() if isinstance(value, dict)
        )

    reels_list = payload.get("reels_media")
    if isinstance(reels_list, list):
        reels.extend((None, reel) for reel in reels_list if isinstance(reel, dict))

    out: dict[int, list[StoryItem]] = {}
    for key, reel in reels:
        user_id = None
        for candidate in (key, reel.get("id"), (reel.get("user") or {}).get("pk")):
            user_id = _as_int(candidate)
            if user_id is not None:
                break
        if user_id is None:
            continue
        for item in reel.get("items") or []:
            if not isinstance(item, dict):
                continue
            story_id = item.get("pk") or item.get("id")
            if story_id is None:
                continue
            candidates = ((item.get("image_versions2") or {}).get("candidates")) or []
            out.setdefault(user_id, []).append(
                StoryItem(
                    story_id=str(story_id),
                    user_id=user_id,
                    taken_at=_as_int(item.get("taken_at")) or 0,
                    media_type=_as_int(item.get("media_type")) or 1,
                    expiring_at=_as_int(item.get("expiring_at")),
                    image_versions=[c for c in candidates if isinstance(c, dict)],
                    raw=dict(item),
                )
            )
    return out


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.split("_", 1)[0])
        except ValueError:
            return None
    return None
