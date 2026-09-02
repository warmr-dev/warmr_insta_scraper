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
import random
import time
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

# Pause between requests from one session. Irregular on purpose - see `_pause`.
_MIN_GAP_SEC = 1.5
_MAX_GAP_SEC = 4.0

# reels_media takes reel ids as repeated query params, and too many at once
# gives a 400 that is indistinguishable from an expired session. Measured: 74
# ids failed, 20 succeeded, on the same cookies seconds apart.
_REELS_CHUNK = 20

# Cookies the feed endpoints need. sessionid alone yields a 302 to the login page.
COOKIE_NAMES = ("sessionid", "csrftoken", "ds_user_id", "ig_did", "mid", "datr", "rur")


def parse_cookie_header(raw: str) -> dict[str, str]:
    """Parse cookies from any of the shapes a browser hands out.

    Accepts `a=1; b=2`, newline-separated `name<TAB>value`, and the JSON array
    that cookie-export extensions produce (`[{"name": ..., "value": ...}]`).
    Supporting all three means nobody has to reformat by hand.
    """
    import json as _json

    cookies: dict[str, str] = {}
    text = (raw or "").strip()
    if not text:
        return cookies

    # JSON export from a cookie-manager extension.
    if text.startswith("[") or text.startswith("{"):
        try:
            parsed = _json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            for entry in parsed:
                if isinstance(entry, dict) and entry.get("name"):
                    cookies[str(entry["name"])] = str(entry.get("value", ""))
            return cookies
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items() if v is not None}

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


# Пул соединений по аккаунтам. Живёт столько же, сколько процесс: цикл
# создаёт WebTransport заново каждый раз, а рукопожатие должно случиться один
# раз, а не 30 раз в час на каждый аккаунт.
_CLIENTS: dict[str, httpx.Client] = {}


def _client_for(key: str, *, timeout: float, headers: dict[str, str]) -> httpx.Client:
    """Соединение для этого аккаунта, создаваемое один раз."""
    existing = _CLIENTS.get(key)
    if existing is not None and not existing.is_closed:
        # Куки и заголовки могли обновиться (новая вставка cookies, свой UA).
        existing.headers.update(headers)
        return existing

    client = httpx.Client(
        timeout=timeout,
        follow_redirects=False,  # a 302 means "not authorised", not "go here"
        headers=headers,
        # Держим соединение живым между циклами - как браузер.
        limits=httpx.Limits(
            max_keepalive_connections=1, max_connections=2, keepalive_expiry=600.0
        ),
    )
    _CLIENTS[key] = client
    return client


def close_all_connections() -> None:
    """Закрыть все пулы. Для остановки процесса и для тестов."""
    for client in list(_CLIENTS.values()):
        try:
            client.close()
        except Exception:  # noqa: BLE001 - закрытие не должно ничего ронять
            pass
    _CLIENTS.clear()


class WebTransport:
    """Reads the story tray and story items using browser cookies.

    Implements the subset of `InstagramTransport` that the web API supports:
    `reels_tray` and `reels_media`. Login, follow, and friendship are not
    available here by design - this is a read path, not an account driver.
    """

    def __init__(
        self,
        cookies: dict[str, str] | str,
        timeout: float = 30.0,
        user_agent: str | None = None,
    ) -> None:
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
        # Ключ пула соединений. ds_user_id стабилен для аккаунта и переживает
        # обновление куки, поэтому вставка свежих куки не рвёт соединение.
        self.username_key = self.user_id or jar.get("sessionid", "")[:24]

        # The browser these cookies came from. `datr` is Facebook's DEVICE
        # identity cookie - it is minted for one browser on one machine and
        # lives for years, and Instagram checks it against the User-Agent that
        # presents it. Replaying a Windows/Chrome-133 `datr` while claiming to
        # be Chrome 120 on a Mac is a contradiction that only ever happens with
        # copied credentials, and it shortens session life.
        #
        # Falling back to a shared constant also made every account look like
        # the SAME machine running N sessions. Storing the real UA per session
        # makes `datr` corroborate the request instead of undermining it.
        self.user_agent = user_agent or _UA
        if not user_agent:
            log.warning(
                "web_user_agent_missing",
                detail="using the shared default; store the browser's own UA "
                "with the cookies so datr matches the fingerprint",
            )

        # Instagram hands back `x-ig-set-www-claim` and expects it echoed as
        # `X-IG-WWW-Claim` on later requests. Real browsers do; never sending it
        # marks the session as automated. Starts empty and is learned from the
        # first response.
        self._www_claim = ""

        # Одно соединение на сессию, переживающее циклы.
        #
        # Замерено, и это оказалось НЕ про частоту: 10 запросов подряд по
        # одному соединению - 10 раз 200; те же 10 запросов, каждый со своим
        # новым соединением - четыре 401. Медленнее (пауза 3с) давало БОЛЬШЕ
        # ошибок, чем без пауз, чего лимитер частоты дать не может.
        #
        # Дело в TLS-рукопожатии: браузер держит соединение открытым и шлёт по
        # нему десятки запросов, а мы открывали новое на каждый аккаунт в
        # каждом цикле. Именно эта картина и ловилась как "не браузер".
        self._client = _client_for(
            self.username_key,
            timeout=timeout,
            headers={
                "User-Agent": self.user_agent,
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

    def _pause(self) -> None:
        """Wait a random moment between requests.

        A browser makes requests when a person does something; it never fires
        two XHRs exactly 0ms apart, and never on a fixed cadence. Perfectly
        regular timing is one of the cheapest automation signals there is, so
        spread requests out irregularly rather than uniformly.
        """
        time.sleep(random.uniform(_MIN_GAP_SEC, _MAX_GAP_SEC))

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"X-IG-WWW-Claim": self._www_claim} if self._www_claim else {}
        response = self._client.get(
            f"{_BASE}/{path}", params=params, cookies=self.cookies, headers=headers
        )

        # Learn the claim for subsequent calls. Instagram rotates it, so take
        # whatever the latest response carries.
        claim = response.headers.get("x-ig-set-www-claim")
        if claim:
            self._www_claim = claim

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
        if response.status_code == 401:
            # NOT an expired session. Measured across 11 accounts: four answered
            # 401 on `friendships/.../following/` while serving `reels_media`
            # normally with the same cookies, seconds apart. Instagram throttles
            # the social graph separately from the feeds, so treating this as
            # expiry would disable working sessions - which is the exact bug
            # this rewrite exists to fix.
            raise RateLimitedError(f"web API 401 on {path} - throttled, not expired")
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

    def following(self, limit: int = 200) -> list[tuple[int, str]]:
        """Accounts this session follows, as (user_id, username).

        The web API has no story-tray endpoint (see `reels_tray`), so the set of
        accounts to ask about has to be enumerated directly.
        """
        if not self.user_id:
            raise TransportError("cookies have no ds_user_id, cannot list following")

        out: list[tuple[int, str]] = []
        max_id: str | None = None
        while len(out) < limit:
            params: dict[str, Any] = {"count": 100}
            if max_id:
                params["max_id"] = max_id
            data = self._get(f"friendships/{self.user_id}/following/", params=params)

            users = data.get("users") or []
            for u in users:
                pk = u.get("pk") or u.get("id")
                if pk is None:
                    continue
                out.append((int(pk), str(u.get("username") or pk)))

            max_id = data.get("next_max_id")
            if not max_id or not users:
                break
            self._pause()

        log.info("web_following", count=len(out))
        return out[:limit]

    def reels_tray(self, *, cold_start: bool = False, max_id: str | None = None) -> TrayResponse:
        """Every following that currently has a story.

        NOT `feed/reels_tray/`. That endpoint exists on the MOBILE API only; the
        web host answers it with the 600KB HTML app shell and a 200, which the
        caller could only read as "session dead". Measured: the same cookies
        that got a login page from the tray answered `reels_media` correctly
        seconds later, and this one bad probe disabled 8 of 11 live sessions.

        So the tray is assembled instead: list what the session follows, ask
        `reels_media` who among them has a story, and return that in the shape
        the callers already expect.
        """
        return self.tray_from_following(self.following())

    def tray_from_following(self, following: list[tuple[int, str]]) -> TrayResponse:
        """The tray for an already-known following list.

        Split out so a caller holding a cached list can skip the graph call
        entirely - it is throttled separately from the feeds.
        """
        if not following:
            return TrayResponse(entries=[], raw={})

        names = dict(following)
        reels = self.reels_media([uid for uid, _ in following])

        entries = [
            TrayEntry(
                id=str(uid),
                user={"pk": uid, "username": names.get(uid, str(uid))},
                items=[i.raw for i in items],
                raw={"id": uid},
            )
            for uid, items in reels.items()
            if items
        ]
        log.info("web_reels_tray", entry_count=len(entries), following=len(following))
        return TrayResponse(entries=entries, raw={})

    def reels_media(self, user_ids: list[int]) -> dict[int, list[StoryItem]]:
        """Batch story fetch, in chunks.

        The whole id list used to go into one URL. Instagram answers 400 once it
        is too long, and `_get` maps a 400 on a feed to LoginRequiredError - so
        a session with many followings was reported as expired and disabled.
        Measured: 74 ids -> 400, the same cookies at 20 ids -> 142 story items.
        """
        if not user_ids:
            return {}

        result: dict[int, list[StoryItem]] = {}
        raw_reels: dict[str, Any] = {}
        for start in range(0, len(user_ids), _REELS_CHUNK):
            chunk = user_ids[start : start + _REELS_CHUNK]
            data = self._get(
                "feed/reels_media/", params=[("reel_ids", str(u)) for u in chunk]
            )
            raw_reels.update(data.get("reels") or {})
            if start + _REELS_CHUNK < len(user_ids):
                self._pause()

        for reel_id, reel in raw_reels.items():
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
        """Отпустить транспорт, НЕ закрывая соединение.

        Соединение переиспользуется между циклами - в этом весь смысл. Явно
        закрыть пул можно через `close_all_connections()` при остановке.
        """
        return


def story_age_hours(item: StoryItem, now: dt.datetime | None = None) -> float:
    """Hours since the story was posted - the detection-latency input."""
    reference = now or dt.datetime.now(dt.UTC)
    taken = dt.datetime.fromtimestamp(item.taken_at, tz=dt.UTC)
    return (reference - taken).total_seconds() / 3600
