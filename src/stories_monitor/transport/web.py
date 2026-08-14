"""WebTransport - stories through the browser (web) API.

An alternative to `LiveTransport` for when a mobile-API login is unavailable but
you can copy cookies out of a logged-in browser.

Deliberately separate from `LiveTransport`:

- It talks to `www.instagram.com/api/v1/...`, not `i.instagram.com`. Those are
  different surfaces with different auth scoping. Sending browser cookies to the
  mobile host is what makes Instagram treat the request as a stolen session.
- It does not import instagrapi at all, so it cannot inherit the mobile device
  fingerprint that causes that mismatch.

Limitations, measured rather than assumed:

- The web API is rate-limited harder than the mobile one, so this does not scale
  to ~20 workers polling every 120s. Treat it as a diagnostic path.
- A `sessionid` alone is NOT enough: the feed endpoints answer 302 without the
  supporting cookies (`ig_did`, `mid`, `datr`, `rur`). Copy them all.
- Sessions expire and cannot be renewed from here - there is no login flow.

Read-only. Never calls `media/seen/` or any write endpoint (SPEC section 11).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx

from ..logging_setup import get_logger
from .base import (
    LoginRequiredError,
    RateLimitedError,
    StoryItem,
    TransportError,
    TrayEntry,
    TrayResponse,
)

log = get_logger(__name__)

_BASE = "https://www.instagram.com/api/v1"

# The public web client id the instagram.com frontend sends on every XHR.
_WEB_APP_ID = "936619743392459"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Cookies the feed endpoints need. sessionid alone yields a 302 to the login page.
COOKIE_NAMES = ("sessionid", "csrftoken", "ds_user_id", "ig_did", "mid", "datr", "rur")


def parse_cookie_header(raw: str) -> dict[str, str]:
    """Parse a pasted `document.cookie` string or a DevTools cookie dump.

    Accepts `a=1; b=2` and newline-separated `name<TAB>value` pairs, so a copy
    from either the console or the Application tab works.
    """
    cookies: dict[str, str] = {}
    text = (raw or "").strip()
    if not text:
        return cookies

    separator = ";" if ";" in text else "\n"
    for chunk in text.split(separator):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk:
            name, _, value = chunk.partition("=")
        elif "\t" in chunk:
            name, _, value = chunk.partition("\t")
        else:
            continue
        name, value = name.strip(), value.strip()
        if name:
            cookies[name] = value
    return cookies


class WebTransport:
    """Reads the story tray and story items using browser cookies.

    Implements the subset of `InstagramTransport` that the web API supports:
    `reels_tray` and `reels_media`. Login, follow, and friendship are not
    available here by design - this is a read path, not an account driver.
    """

    def __init__(self, cookies: dict[str, str] | str, timeout: float = 30.0) -> None:
        jar = parse_cookie_header(cookies) if isinstance(cookies, str) else dict(cookies)
        if not jar.get("sessionid"):
            raise ValueError("cookies must include sessionid")

        missing = [n for n in COOKIE_NAMES if n not in jar]
        if missing:
            # Not fatal - some accounts work without all of them - but the feed
            # endpoints usually 302 when ig_did/mid are absent, so say so early.
            log.warning(
                "web_cookies_incomplete",
                missing=missing,
                detail="feed endpoints often answer 302 without these",
            )

        self.cookies = jar
        self.user_id = jar.get("ds_user_id", "")
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=False,  # a 302 means "not authorised", not "go here"
            headers={
                "User-Agent": _UA,
                "X-IG-App-ID": _WEB_APP_ID,
                "X-CSRFToken": jar.get("csrftoken", ""),
                "X-Requested-With": "XMLHttpRequest",
                "X-ASBD-ID": "129477",
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.instagram.com/",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
            },
        )

    # --- requests ---

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._client.get(f"{_BASE}/{path}", params=params, cookies=self.cookies)

        if response.status_code in (301, 302, 303, 307, 308):
            raise LoginRequiredError(
                "web API redirected to login - the session is not authorised for this "
                "endpoint. Copy the full cookie set (ig_did, mid, datr, rur), not just "
                "sessionid."
            )
        if response.status_code == 429:
            raise RateLimitedError("web API rate limited (429)")
        if response.status_code == 403:
            raise LoginRequiredError("web API rejected the session (403)")
        if response.status_code == 400 and path.startswith("feed/"):
            # Instagram отвечает 400 на ленты, когда сессия больше не
            # действительна - отдельного "expired" статуса у веб-API нет.
            # Проверено: тот же ответ приходит и на users/web_profile_info,
            # то есть дело в сессии, а не в конкретном эндпоинте.
            raise LoginRequiredError(
                "web API returned 400 on a feed - the session is no longer valid. "
                "Скопируйте свежие куки из браузера."
            )
        if response.status_code >= 400:
            raise TransportError(f"web API {response.status_code} on {path}")

        try:
            return response.json()
        except ValueError as exc:
            raise TransportError(f"web API returned non-JSON on {path}") from exc

    # --- reads ---

    def whoami(self) -> str:
        """Username behind these cookies. Cheapest way to prove they still work."""
        data = self._get("accounts/current_user/")
        return str((data.get("user") or {}).get("username") or "")

    def reels_tray(self, *, cold_start: bool = False, max_id: str | None = None) -> TrayResponse:
        """Story tray for every following with an active story."""
        data = self._get("feed/reels_tray/")
        entries = [
            TrayEntry(
                id=str(raw.get("id", "")),
                latest_reel_media=raw.get("latest_reel_media"),
                seen=raw.get("seen"),
                user=raw.get("user") or {},
                items=raw.get("items") or [],
                raw=raw,
            )
            for raw in (data.get("tray") or [])
        ]
        log.info("web_reels_tray", entry_count=len(entries))
        return TrayResponse(entries=entries, raw=data)

    def reels_media(self, user_ids: list[int]) -> dict[int, list[StoryItem]]:
        """Batch story fetch. The web form takes repeated `reel_ids` query params."""
        if not user_ids:
            return {}

        data = self._get(
            "feed/reels_media/", params=[("reel_ids", str(u)) for u in user_ids]
        )

        result: dict[int, list[StoryItem]] = {}
        for reel_id, reel in (data.get("reels") or {}).items():
            try:
                owner = int(reel_id)
            except ValueError:
                continue
            items: list[StoryItem] = []
            for raw in reel.get("items") or []:
                story_id = str(raw.get("pk") or raw.get("id") or "").split("_")[0]
                taken_at = raw.get("taken_at")
                if not story_id or taken_at is None:
                    continue
                items.append(
                    StoryItem(
                        story_id=story_id,
                        user_id=owner,
                        taken_at=int(taken_at),
                        media_type=int(raw.get("media_type") or 1),
                        expiring_at=raw.get("expiring_at"),
                        image_versions=(raw.get("image_versions2") or {}).get("candidates")
                        or [],
                        raw=raw,
                    )
                )
            result[owner] = items

        log.info("web_reels_media", users=len(result))
        return result

    def download_media(self, url: str, dest_path: str) -> str:
        """Fetch media bytes. URLs are short-lived, so retry once (SPEC 7.3)."""
        for attempt in (1, 2):
            try:
                response = self._client.get(url, headers={"User-Agent": _UA})
                response.raise_for_status()
                with open(dest_path, "wb") as handle:
                    handle.write(response.content)
                return dest_path
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise TransportError(f"media download failed: {exc}") from exc
        return dest_path

    def close(self) -> None:
        self._client.close()


def story_age_hours(item: StoryItem, now: dt.datetime | None = None) -> float:
    """Hours since the story was posted - the detection-latency input."""
    reference = now or dt.datetime.now(dt.UTC)
    taken = dt.datetime.fromtimestamp(item.taken_at, tz=dt.UTC)
    return (reference - taken).total_seconds() / 3600
