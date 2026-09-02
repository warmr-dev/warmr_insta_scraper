"""Хранение веб-куки для нескольких рабочих аккаунтов.

Куки лежат в `worker_accounts.session_json` под ключом `web_cookies`, рядом с
мобильной сессией - у аккаунта может быть и то, и другое одновременно, и они
не мешают друг другу.

Ключевое отличие от мобильной сессии: веб-куки **нельзя обновить из кода**.
Истекли - человек идёт в браузер. Поэтому здесь есть `is_expired` и понятные
сообщения, а не попытки восстановиться самостоятельно.
"""

from __future__ import annotations

import datetime as dt
import os
import random
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from . import activity
from .db.models import Cookie
from .db.session import session_scope
from .logging_setup import get_logger
from .transport.base import LoginRequiredError, RateLimitedError, TransportError
from .transport.web import COOKIE_NAMES, WebTransport, parse_cookie_header

log = get_logger(__name__)

# Куки живут в отдельной таблице `cookies` - её удобно править руками в
# интерфейсе Supabase, когда они истекли. Продлить их из кода нельзя, поэтому
# ручное обновление входит в штатную эксплуатацию.


@dataclass(slots=True)
class WebAccount:
    """Аккаунт с сохранёнными веб-куки."""

    id: int
    username: str
    shard_id: int
    cookies: dict[str, str]
    saved_at: str | None = None
    user_agent: str | None = None
    following: list[Any] | None = None
    following_at: dt.datetime | None = None

    @property
    def following_is_stale(self) -> bool:
        """Пора ли обновить список подписок.

        Два разных срока. После УСПЕХА список верен и живёт TTL. После
        НЕУДАЧИ ждать столько же нельзя: список уже мог устареть, а
        устаревший список делает аккаунт слепым к новым подпискам. Замерено:
        401 на графе перемежается с 200, так что повтор через полчаса
        осмысленен, а раз в сутки - нет.
        """
        if self.following_at is None:
            return True
        age = (dt.datetime.now(dt.UTC) - self.following_at).total_seconds()
        window = FOLLOWING_TTL_SEC if self.following else FOLLOWING_RETRY_SEC
        return age >= window

    @property
    def user_id(self) -> str:
        return self.cookies.get("ds_user_id", "")

    @property
    def missing_cookies(self) -> list[str]:
        return [n for n in COOKIE_NAMES if n not in self.cookies]

    def transport(self) -> WebTransport:
        return WebTransport(self.cookies, user_agent=self.user_agent)


def save_cookies(
    username: str, raw: str | dict[str, str], user_agent: str | None = None
) -> WebAccount:
    """Сохранить веб-куки аккаунта в таблицу `cookies` (upsert по username)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    jar = parse_cookie_header(raw) if isinstance(raw, str) else dict(raw)
    if not jar.get("sessionid"):
        raise ValueError("в куки нет sessionid")

    values = {n: jar.get(n) for n in COOKIE_NAMES}
    values["username"] = username
    values["is_active"] = True
    if user_agent:
        values["user_agent"] = user_agent
    values["updated_at"] = dt.datetime.now(dt.UTC)
    values["last_error"] = None

    with session_scope() as session:
        session.execute(
            pg_insert(Cookie)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["username"],
                set_={k: v for k, v in values.items() if k != "username"},
            )
        )

    account = WebAccount(
        id=0,
        username=username,
        shard_id=0,
        cookies=jar,
        saved_at=values["updated_at"].isoformat(),
        user_agent=user_agent,
    )
    log.info(
        "web_cookies_saved",
        username=username,
        cookies=len(jar),
        missing=account.missing_cookies,
    )
    return account


def load_accounts(username: str | None = None) -> list[WebAccount]:
    """Активные аккаунты из таблицы `cookies`. Без аргумента - все."""
    accounts: list[WebAccount] = []
    with session_scope() as session:
        stmt = select(Cookie).where(Cookie.is_active.is_(True)).order_by(Cookie.username)
        if username:
            stmt = select(Cookie).where(Cookie.username == username)
        for row in session.scalars(stmt).all():
            jar = row.as_jar()
            if not jar.get("sessionid"):
                continue
            accounts.append(
                WebAccount(
                    id=0,
                    username=row.username,
                    shard_id=0,
                    cookies=jar,
                    saved_at=row.updated_at.isoformat() if row.updated_at else None,
                    user_agent=row.user_agent,
                    following=row.following,
                    following_at=row.following_at,
                )
            )
    return accounts


def clear_cookies(username: str) -> bool:
    """Удалить куки аккаунта из таблицы."""
    from sqlalchemy import delete

    with session_scope() as session:
        result = session.execute(delete(Cookie).where(Cookie.username == username))
        return bool(result.rowcount)


def mark_failed(username: str, error: str) -> None:
    """Пометить куки нерабочими - видно в Supabase, какие пора обновить."""
    from sqlalchemy import update as sa_update

    try:
        with session_scope() as session:
            session.execute(
                sa_update(Cookie)
                .where(Cookie.username == username)
                .values(last_error=error[:500], is_active=False)
            )
    except Exception as exc:  # noqa: BLE001 - тоже вызывается из обработчика
        log.warning("mark_failed_failed", username=username, error=str(exc)[:120])
        return
    log.warning("web_cookies_marked_failed", username=username, error=error[:120])


def save_following(username: str, pairs: list[tuple[int, str]]) -> None:
    """Запомнить список подписок - на случай, когда граф отдаёт 401."""
    from sqlalchemy import update as sa_update

    if not pairs:
        return
    try:
        with session_scope() as session:
            session.execute(
                sa_update(Cookie)
                .where(Cookie.username == username)
                .values(
                    following=[[int(u), n] for u, n in pairs],
                    following_at=dt.datetime.now(dt.UTC),
                )
            )
    except Exception as exc:  # noqa: BLE001 - кэш не стоит цикла
        log.warning("following_cache_write_failed", username=username, error=str(exc)[:120])


def touch_following_attempt(username: str, retry_in: int | None = None) -> None:
    """Отметить попытку обновления подписок, не трогая сам список.

    Без этого сессия с пустым или устаревшим кэшем била в троттлящийся граф
    каждый цикл: `following_is_stale` оставался True, потому что
    `following_at` обновлялся только при успехе. Замерено: 4 сессии делали это
    раз в ~90 секунд без единого шанса на успех.
    """
    from sqlalchemy import update as sa_update

    # retry_in сдвигает отметку в прошлое так, чтобы следующая попытка пришлась
    # через указанное число секунд, а не через полный TTL.
    stamp = dt.datetime.now(dt.UTC)
    if retry_in is not None:
        stamp -= dt.timedelta(seconds=max(0, FOLLOWING_TTL_SEC - retry_in))

    try:
        with session_scope() as session:
            session.execute(
                sa_update(Cookie)
                .where(Cookie.username == username)
                .values(following_at=stamp)
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("following_touch_failed", username=username, error=str(exc)[:120])


def note_error(username: str, error: str) -> None:
    """Записать причину сбоя, НЕ отключая аккаунт.

    Для временных отказов - сеть, 429. Отключать из-за них нельзя: аккаунт
    рабочий, просто сейчас не отвечает.

    Никогда не бросает: вызывается ИЗ обработчика, который существует ровно
    для того, чтобы один плохой аккаунт не рушил цикл. Замерено: оборванное
    соединение с Supabase внутри этого обработчика уронило весь прогон.
    """
    from sqlalchemy import update as sa_update

    try:
        with session_scope() as session:
            session.execute(
                sa_update(Cookie).where(Cookie.username == username).values(last_error=error[:500])
            )
    except Exception as exc:  # noqa: BLE001 - запись причины не стоит цикла
        log.warning("note_error_failed", username=username, error=str(exc)[:120])


def revive(username: str) -> bool:
    """Снова включить аккаунт после обновления куки."""
    from sqlalchemy import update as sa_update

    with session_scope() as session:
        result = session.execute(
            sa_update(Cookie)
            .where(Cookie.username == username)
            .values(is_active=True, last_error=None)
        )
        return bool(result.rowcount)


def check_alive(account: WebAccount) -> tuple[bool, str]:
    """Живы ли куки. Возвращает (жива, пояснение).

    Проверяем через `reels_tray` - это то, что нам реально нужно.
    `accounts/current_user/` отвечает 400 на веб-куки даже при рабочих лентах,
    поэтому как индикатор он не годится.
    """
    transport = account.transport()
    try:
        tray = transport.reels_tray()
        users = [e for e in tray.entries if e.is_user_entry]
        return True, f"{tray.entry_count} entries, {len(users)} with active stories"
    except Exception as exc:  # noqa: BLE001 - любая ошибка означает "непригодна"
        return False, f"{type(exc).__name__}: {str(exc)[:80]}"
    finally:
        transport.close()


# Как часто обновлять список подписок. Сутки: подписки меняются редко, а
# граф - самый троттлимый эндпоинт из тех, что мы трогаем.
FOLLOWING_TTL_SEC = int(os.environ.get("FOLLOWING_TTL_SEC", str(6 * 3600)))

# Отдельный, КОРОТКИЙ интервал для повтора после неудачи. Сутки здесь неверны:
# протухший список делает аккаунт слепым к новым подпискам - замерено, сессия
# сообщала "0 entries", когда у двух её новых подписок былиживые сторис. Ждать
# сутки, чтобы это исправить, дороже, чем изредка попробовать граф.
FOLLOWING_RETRY_SEC = int(os.environ.get("FOLLOWING_RETRY_SEC", str(30 * 60)))

# Пауза между аккаунтами внутри цикла, секунды.
_ACCOUNT_GAP_MIN = 3.0
_ACCOUNT_GAP_MAX = 12.0

# Кто из воркеров принёс сторис конкретной цели. Заполняется collect_stories и
# читается фазой AI, чтобы в дашборде оценка была привязана к сессии.
SOURCE: dict[int, str] = {}


def collect_stories(
    accounts: list[WebAccount] | None = None,
) -> tuple[dict[int, list[Any]], dict[int, str], dict[str, str]]:
    """Собрать сторис со ВСЕХ аккаунтов сразу.

    Это и есть шардирование из §1, только на веб-куки: каждый аккаунт отдаёт
    сторис всех своих подписок одним запросом, а объединение покрывает больше
    целей, чем любой из них поодиночке.

    Возвращает (сторис по user_id, имена, статусы аккаунтов).
    """
    pool = accounts if accounts is not None else load_accounts()
    merged: dict[int, list[Any]] = {}
    names: dict[int, str] = {}
    status: dict[str, str] = {}
    SOURCE.clear()

    for index, account in enumerate(pool):
        # Пауза между аккаунтами. 11 сессий подряд с одного IP - это всплеск,
        # которого у живого человека быть не может; вразброс он выглядит как
        # несколько разных людей, а не как один скрипт.
        if index:
            time.sleep(random.uniform(_ACCOUNT_GAP_MIN, _ACCOUNT_GAP_MAX))

        transport = account.transport()
        started = time.monotonic()
        try:
            # Список подписок берём из кэша, а обновляем раз в сутки.
            #
            # `reels_media` умеет только "есть ли сторис у ВОТ ЭТИХ id" - сам
            # список он не отдаёт, а трея на веб-API нет. Значит id откуда-то
            # нужны, и единственный источник - граф. Но подписки меняются
            # медленно (на аккаунт подписались один раз и всё), а
            # `friendships/.../following/` троттлится жёстче лент: замерено, 4
            # из 11 сессий отдавали там 401, продолжая нормально отвечать на
            # reels_media. Дёргать его каждый цикл значило платить самым
            # рискованным запросом за данные, которые не менялись.
            pairs = [(int(u), n) for u, n in (account.following or [])]
            # `not pairs` НЕ входит в условие намеренно. Раньше входило - и
            # сводило на нет весь смысл TTL: у сессии без кэша заполнить его
            # можно только тем запросом, который троттлится, поэтому она била
            # в граф каждый цикл, сколько бы раз он ни ответил 401. Теперь
            # окно одно для всех: пустой кэш ждёт следующего TTL так же, как
            # устаревший, потому что `following_at` ставится и при неудаче.
            if account.following_is_stale:
                try:
                    fresh = transport.following()
                    if fresh:
                        save_following(account.username, fresh)
                        pairs = fresh
                except (RateLimitedError, TransportError) as exc:
                    if not pairs:
                        # Кэша нет и граф не отвечает - тупик: заполнить кэш
                        # можно только тем самым запросом, который троттлится.
                        # Долбить его каждые полторы минуты бессмысленно и
                        # вредно, поэтому запоминаем неудачу и не повторяем её
                        # до следующего окна (following_at ставится сейчас,
                        # так что следующая попытка - через TTL).
                        note_error(account.username, f"following unavailable: {exc}")
                        touch_following_attempt(account.username)
                        log.warning(
                            "following_bootstrap_failed",
                            username=account.username,
                            detail="no cache and the graph is throttled; "
                            "retry deferred to the next TTL window",
                            reason=str(exc)[:80],
                        )
                        raise
                    # Кэш есть, но он уже просрочен: обновление не удалось.
                    # Отматываем following_at так, чтобы следующая попытка
                    # была через FOLLOWING_RETRY_SEC, а не через полный TTL -
                    # иначе аккаунт остаётся слепым к новым подпискам на сутки
                    # из-за одного 401.
                    touch_following_attempt(
                        account.username, retry_in=FOLLOWING_RETRY_SEC
                    )
                    log.warning(
                        "following_refresh_failed",
                        username=account.username,
                        cached=len(pairs),
                        reason=str(exc)[:80],
                    )

            if not pairs:
                # Ни кэша, ни права попробовать в этом окне. Отдельная ветка:
                # без неё tray_from_following([]) вернул бы пустой трей и цикл
                # отчитался бы "OK - 0 followings", что неотличимо от "у всех
                # подписок нет сторис".
                raise RateLimitedError(
                    "no cached following list and the graph is throttled; "
                    "next attempt after the TTL window"
                )

            tray = transport.tray_from_following(pairs)
            users = [e for e in tray.entries if e.is_user_entry]
            for entry in users:
                names.setdefault(entry.user_id, entry.user.get("username", str(entry.id)))

            # Кого именно опрашиваем - это и есть "аккаунт 1 берёт сторис у 1..2..3"
            # в дашборде.
            handles = [names.get(e.user_id, str(e.user_id)) for e in users if e.user_id]
            activity.record(
                account.username,
                "poll",
                message=f"Polled tray: {tray.entry_count} entries, {len(users)} with active stories",
                targets=handles,
                item_count=len(users),
                duration_ms=int((time.monotonic() - started) * 1000),
            )

            reels = transport.reels_media([e.user_id for e in users if e.user_id])
            new_users = 0
            for user_id, items in reels.items():
                if user_id not in merged:
                    merged[user_id] = items
                    SOURCE[user_id] = account.username
                    new_users += 1
                # Тот же аккаунт виден с двух воркеров - берём непустой набор.
                elif not merged[user_id] and items:
                    merged[user_id] = items
                    SOURCE[user_id] = account.username

            fetched = sum(len(v) for v in reels.values())
            with_stories = [
                names.get(uid, str(uid)) for uid, items in reels.items() if items
            ]
            activity.record(
                account.username,
                "stories_found",
                message=(
                    f"Fetched {fetched} story items from {len(with_stories)} accounts "
                    f"({new_users} new for the pool)"
                ),
                targets=with_stories,
                item_count=fetched,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

            status[account.username] = (
                f"OK - {len(users)} followings with stories, {new_users} new for the pool"
            )
            log.info(
                "web_account_polled",
                username=account.username,
                tray_entries=tray.entry_count,
                users_with_stories=len(users),
            )
        except LoginRequiredError as exc:
            # Куки протухли. Продлить их из кода нельзя, поэтому аккаунт
            # выключается: следующие циклы его пропустят, и мы не будем зря
            # долбить Instagram мёртвой сессией.
            # Записи в БД - только по-английски: их читают в Supabase, где
            # кириллица в CSV-выгрузках и алертах часто ломается.
            mark_failed(account.username, f"cookies expired: {exc}")
            activity.record(
                account.username,
                "error",
                status="expired",
                message=f"Session expired and was disabled: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            status[account.username] = "COOKIES EXPIRED - disabled, refresh in Supabase"
            log.error(
                "web_cookies_expired",
                username=account.username,
                detail="is_active=false; refresh the row in the cookies table",
            )
        except RateLimitedError as exc:
            # Временно, аккаунт не трогаем - отключать его было бы ошибкой.
            activity.record(
                account.username,
                "error",
                status="rate_limited",
                message=f"Rate limited, skipping this cycle: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            status[account.username] = "RATE LIMITED - skipping this cycle"
            log.warning("web_account_throttled", username=account.username, error=str(exc)[:80])
        except Exception as exc:  # noqa: BLE001 - один мёртвый аккаунт не рушит сбор
            # Сетевой сбой и прочее - тоже временное. Не выключаем, но
            # записываем причину, чтобы её было видно в Supabase.
            note_error(account.username, f"{type(exc).__name__}: {exc}")
            activity.record(
                account.username,
                "error",
                status="error",
                message=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            status[account.username] = f"ERROR - {type(exc).__name__}"
            log.warning(
                "web_account_failed", username=account.username, error=str(exc)[:120]
            )
        finally:
            transport.close()

    return merged, names, status
