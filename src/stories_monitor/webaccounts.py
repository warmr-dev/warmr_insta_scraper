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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from . import activity
from .cadence import load_cadences, load_session_cadences, select_due
from .db.models import Cookie
from .db.session import get_sessionmaker, session_scope
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
        raise ValueError("cookies do not contain sessionid")

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
    """Mark the cookies as broken - Supabase then shows which need refreshing.

    Also hands this session's follow assignments back to the pool. A session is
    declared dead here no matter which worker noticed (the collector on a read,
    the follower on a write), so this is the one chokepoint where the targets it
    was holding become available to the surviving sessions - which is what keeps
    collection uninterrupted rather than quietly losing those accounts.
    """
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

    try:
        from .follow_assign import reap_session

        freed = reap_session(username, reason=f"session marked failed: {error}"[:200])
        if freed:
            log.info("dead_session_follows_freed", username=username, freed=freed)
    except Exception as exc:  # noqa: BLE001 - freeing must never mask the failure
        log.warning("reap_on_mark_failed_failed", username=username, error=str(exc)[:120])


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

# Потолок запросов на аккаунт в сутки. Существует не ради экономии трафика, а
# чтобы ни один аккаунт не мог набрать за день столько обращений, сколько живой
# человек не сделает никогда. Замерено на реальных данных: 8 запросов в цикл
# при 120с - это 5760 обращений в сутки на шесть аккаунтов, ради ~31 найденной
# сторис. Один аккаунт, на котором Instagram показал предупреждение об
# автоматизации, к тому моменту принял на себя сотни запросов подряд.
DAILY_REQUEST_BUDGET = int(os.environ.get("DAILY_REQUEST_BUDGET", "400"))

# Часы (UTC), когда цели почти не публикуют. Замерено за 30 дней: 04:00-17:00
# дают втрое больше сторис, чем 18:00-03:00. Ночью опрашиваем реже - это
# убирает примерно треть суточных запросов, не теряя почти ничего.
QUIET_HOURS_START = int(os.environ.get("QUIET_HOURS_START", "18"))
QUIET_HOURS_END = int(os.environ.get("QUIET_HOURS_END", "4"))

# Во сколько раз растягивать паузу ночью. 1.0 - как днём, 4.0 - вчетверо реже.
NIGHT_SLOWDOWN = float(os.environ.get("NIGHT_SLOWDOWN", "3.0"))

# Разброс множителя интервала. Живой человек не открывает приложение по
# расписанию: между заходами то минута, то полчаса. Диапазон 0.25x-1.5x даёт
# именно такую неровность - иногда быстрее обычного, иногда заметно медленнее.
PACE_MIN = float(os.environ.get("PACE_MIN", "0.25"))
PACE_MAX = float(os.environ.get("PACE_MAX", "1.5"))


def in_quiet_hours(now: dt.datetime | None = None) -> bool:
    """Сейчас тихие часы? Интервал может пересекать полночь."""
    hour = (now or dt.datetime.now(dt.UTC)).hour
    if QUIET_HOURS_START <= QUIET_HOURS_END:
        return QUIET_HOURS_START <= hour < QUIET_HOURS_END
    return hour >= QUIET_HOURS_START or hour < QUIET_HOURS_END


def pace_multiplier(now: dt.datetime | None = None) -> float:
    """Множитель интервала для этого цикла.

    Две составляющие, и обе важны по отдельности:

    - СЛУЧАЙНОСТЬ. Ровный интервал - самый дешёвый признак автоматизации:
      его видно, не читая ни одного запроса. Множитель 0.25x-1.5x означает,
      что два соседних цикла почти никогда не совпадают по длине.
    - СУТКИ. Замерено за 30 дней: 04:00-17:00 UTC дают втрое больше сторис,
      чем 18:00-03:00. Ночью растягиваем интервал, а не пропускаем аккаунты -
      так каждый аккаунт всё равно проверяется, просто реже.
    """
    base = random.uniform(PACE_MIN, PACE_MAX)
    return base * NIGHT_SLOWDOWN if in_quiet_hours(now) else base


def _bump_requests(username: str, count: int) -> int:
    """Прибавить запросы к дневному счётчику и вернуть новое значение.

    Счётчик живёт в `cookies`, а не считается по activity_log: тот чистится
    раз в 48 часов, а бюджет, обнуляющийся вместе с логами, - не бюджет.
    Окно скользит сутками от первого запроса.
    """
    from sqlalchemy import text as sa_text

    try:
        with session_scope() as session:
            row = session.execute(
                sa_text(
                    """
                    UPDATE cookies
                       SET requests_today = CASE
                             WHEN requests_reset_at IS NULL
                               OR requests_reset_at < now() - interval '24 hours'
                             THEN :n ELSE requests_today + :n END,
                           requests_reset_at = CASE
                             WHEN requests_reset_at IS NULL
                               OR requests_reset_at < now() - interval '24 hours'
                             THEN now() ELSE requests_reset_at END
                     WHERE username = :u
                 RETURNING requests_today
                    """
                ),
                {"u": username, "n": count},
            ).scalar()
            return int(row or 0)
    except Exception:  # noqa: BLE001 - бюджет не стоит цикла
        return 0


def _requests_today(username: str) -> int:
    """Текущий расход по дневному бюджету."""
    return _bump_requests(username, 0)

# Пауза между аккаунтами внутри цикла, секунды.
_ACCOUNT_GAP_MIN = 3.0
_ACCOUNT_GAP_MAX = 12.0

# Кто из воркеров принёс сторис конкретной цели. Заполняется collect_stories и
# читается фазой AI, чтобы в дашборде оценка была привязана к сессии.
SOURCE: dict[int, str] = {}

# Ключ advisory-блокировки Postgres. Произвольное, но постоянное число.
_COLLECT_LOCK_KEY = 728_411_003


@contextmanager
def collection_lock() -> Iterator[bool]:
    """Взять глобальную блокировку сбора. Отдаёт False, если уже занята.

    Зачем: при передеплое Railway старый контейнер ещё жив, когда новый уже
    стартовал, и оба идут по одним и тем же аккаунтам. Замерено в логах - два
    цикла с разницей в 7 секунд, аккаунт опрошен дважды за 8 секунд, и второй
    запрос получил 401. То есть часть троттлинга мы устраивали себе сами.

    `pg_try_advisory_lock` не ждёт: второй процесс просто пропускает цикл.
    Блокировка снимается вместе с соединением, поэтому упавший контейнер её не
    удерживает.
    """
    from sqlalchemy import text as sa_text

    session = None
    acquired = False
    try:
        session = get_sessionmaker()()
        acquired = bool(
            session.execute(
                sa_text("SELECT pg_try_advisory_lock(:k)"), {"k": _COLLECT_LOCK_KEY}
            ).scalar()
        )
        yield acquired
    except Exception as exc:  # noqa: BLE001 - без блокировки лучше работать, чем стоять
        log.warning("collection_lock_failed", error=str(exc)[:120])
        yield True
    finally:
        if session is not None:
            try:
                if acquired:
                    session.execute(
                        sa_text("SELECT pg_advisory_unlock(:k)"),
                        {"k": _COLLECT_LOCK_KEY},
                    )
                    session.commit()
            except Exception:  # noqa: BLE001
                pass
            session.close()

# Счётчик циклов: в тихие часы опрашиваем не всех, а по очереди, иначе одни и
# те же аккаунты не проверялись бы всю ночь.
_CYCLES = 0


def _cycle_counter() -> int:
    return _CYCLES


# Отдых после отказа: username -> момент, до которого аккаунт не трогаем.
# Когда сессию в последний раз опрашивали - для её собственного интервала.
_LAST_POLLED: dict[str, float] = {}
_RESTING: dict[str, float] = {}
_STRIKES: dict[str, int] = {}
# То же самое, но в настенном времени - монотонные часы нельзя показать человеку.
_REST_UNTIL_WALL: dict[str, dt.datetime] = {}

REST_BASE_SEC = int(os.environ.get("REST_BASE_SEC", "600"))
REST_MAX_SEC = int(os.environ.get("REST_MAX_SEC", str(6 * 3600)))


def _rest(username: str) -> None:
    """Отправить аккаунт отдыхать с нарастающей паузой."""
    strikes = _STRIKES.get(username, 0) + 1
    _STRIKES[username] = strikes
    # Экспоненциальный рост, но с разбросом: ровно 10/20/40 минут - это тоже
    # узнаваемый почерк, просто более медленный.
    delay = min(REST_BASE_SEC * (2 ** (strikes - 1)), REST_MAX_SEC)
    delay *= random.uniform(PACE_MIN + 0.5, PACE_MAX)
    delay = min(delay, REST_MAX_SEC)
    _RESTING[username] = time.monotonic() + delay
    until = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=delay)
    _REST_UNTIL_WALL[username] = until
    _persist_rest(username, until, strikes)
    log.warning(
        "account_resting", username=username, strikes=strikes, minutes=round(delay / 60)
    )


def _persist_rest(username: str, until: dt.datetime | None, strikes: int) -> None:
    """Записать отдых в базу - дашборд читает оттуда. Никогда не бросает."""
    from sqlalchemy import update as sa_update

    try:
        with session_scope() as session:
            session.execute(
                sa_update(Cookie)
                .where(Cookie.username == username)
                .values(rest_until=until, rest_strikes=strikes)
            )
    except Exception as exc:  # noqa: BLE001 - статус не стоит цикла
        log.warning("rest_persist_failed", username=username, error=str(exc)[:120])


def _is_resting(username: str) -> bool:
    until = _RESTING.get(username)
    if until is None:
        return False
    if time.monotonic() >= until:
        _RESTING.pop(username, None)
        return False
    return True


def _clear_strikes(username: str) -> None:
    """Успешный опрос обнуляет счётчик - иначе пауза росла бы вечно."""
    _STRIKES.pop(username, None)
    _RESTING.pop(username, None)
    _REST_UNTIL_WALL.pop(username, None)
    _persist_rest(username, None, 0)


def rest_state() -> dict[str, dict[str, Any]]:
    """Кто сейчас отдыхает и до какого времени - для дашборда."""
    now = dt.datetime.now(dt.UTC)
    out: dict[str, dict[str, Any]] = {}
    for username, until in list(_REST_UNTIL_WALL.items()):
        if until <= now:
            continue
        out[username] = {
            "until": until.isoformat(),
            "minutes_left": round((until - now).total_seconds() / 60),
            "strikes": _STRIKES.get(username, 0),
        }
    return out


def collect_stories(
    accounts: list[WebAccount] | None = None,
) -> tuple[dict[int, list[Any]], dict[int, str], dict[str, str]]:
    """Собрать сторис со ВСЕХ аккаунтов сразу.

    Это и есть шардирование из §1, только на веб-куки: каждый аккаунт отдаёт
    сторис всех своих подписок одним запросом, а объединение покрывает больше
    целей, чем любой из них поодиночке.

    Возвращает (сторис по user_id, имена, статусы аккаунтов).
    """
    global _CYCLES
    _CYCLES += 1

    pool = accounts if accounts is not None else load_accounts()
    # Один запрос на цикл: уровни нужны всем аккаунтам, а считаются по общей
    # истории целей.
    cadences = load_cadences()
    session_cadences = load_session_cadences()
    merged: dict[int, list[Any]] = {}
    names: dict[int, str] = {}
    status: dict[str, str] = {}
    SOURCE.clear()

    for index, account in enumerate(pool):
        # Дневной потолок. Аккаунт, упёршийся в него, пропускаем целиком:
        # предупреждение об автоматизации прилетает не за один запрос, а за
        # сотни подряд по одной сессии.
        # Реже опрашиваем сессии, которые ничего не приносят. Замерено за 48
        # часов: две сессии дали 682 сторис из 746, а три другие - ни одной за
        # 54 опроса, накопив при этом ошибок. Лимиты конечны, и тратить их надо
        # на те сессии, ради которых мы торопимся.
        pace = session_cadences.get(account.username)
        if pace is not None and pace.interval_sec > 0:
            last = _LAST_POLLED.get(account.username)
            if last is not None and (time.monotonic() - last) < pace.interval_sec:
                status[account.username] = (
                    f"{pace.tier.upper()} - polled every {pace.interval_sec // 60} min"
                )
                continue

        if _is_resting(account.username):
            status[account.username] = "RESTING after a rate-limit - skipping"
            continue

        used = _requests_today(account.username)
        if used >= DAILY_REQUEST_BUDGET:
            status[account.username] = f"BUDGET REACHED - {used} requests in 24h, resting"
            activity.record(
                account.username,
                "skipped",
                status="budget",
                message=(
                    f"Rested: {used} requests in the last 24h, over the "
                    f"{DAILY_REQUEST_BUDGET} budget"
                ),
            )
            log.info("account_budget_reached", username=account.username, used=used)
            continue

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

            # Опрашиваем не всех подряд, а по ценности цели. 103 из 123
            # подписок не выложили ни одной сторис, а 29 целей дали 835 сторис
            # без единой оценки выше 1: именно эти запросы и съедают лимиты,
            # из-за которых мы опаздываем к интересным аккаунтам.
            due, tier_skips = select_due(pairs, cadences)
            if tier_skips:
                log.info(
                    "cadence_filtered",
                    username=account.username,
                    polled=len(due),
                    skipped=sum(tier_skips.values()),
                    by_tier=tier_skips,
                )

            tray = transport.tray_from_following(due)
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

            # Учитываем реальную стоимость цикла: один запрос графа (если был)
            # плюс по одному на каждые 20 подписок.
            _LAST_POLLED[account.username] = time.monotonic()
            _bump_requests(account.username, max(1, -(-len(pairs) // 20)))
            _clear_strikes(account.username)
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
            # 401/429 - это предупредительный выстрел. Продолжать опрашивать
            # аккаунт через полторы минуты после него - ровно то поведение,
            # которое доводит до видимого пользователю предупреждения об
            # автоматизации. Отдыхаем, и тем дольше, чем чаще прилетает.
            _rest(account.username)
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
