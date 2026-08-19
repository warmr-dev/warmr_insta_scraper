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
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from .db.models import Cookie
from .db.session import session_scope
from .logging_setup import get_logger
from .transport.base import LoginRequiredError, RateLimitedError
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

    @property
    def user_id(self) -> str:
        return self.cookies.get("ds_user_id", "")

    @property
    def missing_cookies(self) -> list[str]:
        return [n for n in COOKIE_NAMES if n not in self.cookies]

    def transport(self) -> WebTransport:
        return WebTransport(self.cookies)


def save_cookies(username: str, raw: str | dict[str, str]) -> WebAccount:
    """Сохранить веб-куки аккаунта в таблицу `cookies` (upsert по username)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    jar = parse_cookie_header(raw) if isinstance(raw, str) else dict(raw)
    if not jar.get("sessionid"):
        raise ValueError("в куки нет sessionid")

    values = {n: jar.get(n) for n in COOKIE_NAMES}
    values["username"] = username
    values["is_active"] = True
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

    with session_scope() as session:
        session.execute(
            sa_update(Cookie)
            .where(Cookie.username == username)
            .values(last_error=error[:500], is_active=False)
        )
    log.warning("web_cookies_marked_failed", username=username, error=error[:120])


def note_error(username: str, error: str) -> None:
    """Записать причину сбоя, НЕ отключая аккаунт.

    Для временных отказов - сеть, 429. Отключать из-за них нельзя: аккаунт
    рабочий, просто сейчас не отвечает.
    """
    from sqlalchemy import update as sa_update

    with session_scope() as session:
        session.execute(
            sa_update(Cookie).where(Cookie.username == username).values(last_error=error[:500])
        )


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

    for account in pool:
        transport = account.transport()
        try:
            tray = transport.reels_tray()
            users = [e for e in tray.entries if e.is_user_entry]
            for entry in users:
                names.setdefault(entry.user_id, entry.user.get("username", str(entry.id)))

            reels = transport.reels_media([e.user_id for e in users if e.user_id])
            new_users = 0
            for user_id, items in reels.items():
                if user_id not in merged:
                    merged[user_id] = items
                    new_users += 1
                # Тот же аккаунт виден с двух воркеров - берём непустой набор.
                elif not merged[user_id] and items:
                    merged[user_id] = items

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
            status[account.username] = "COOKIES EXPIRED - disabled, refresh in Supabase"
            log.error(
                "web_cookies_expired",
                username=account.username,
                detail="is_active=false; refresh the row in the cookies table",
            )
        except RateLimitedError as exc:
            # Временно, аккаунт не трогаем - отключать его было бы ошибкой.
            status[account.username] = "RATE LIMITED - skipping this cycle"
            log.warning("web_account_throttled", username=account.username, error=str(exc)[:80])
        except Exception as exc:  # noqa: BLE001 - один мёртвый аккаунт не рушит сбор
            # Сетевой сбой и прочее - тоже временное. Не выключаем, но
            # записываем причину, чтобы её было видно в Supabase.
            note_error(account.username, f"{type(exc).__name__}: {exc}")
            status[account.username] = f"ERROR - {type(exc).__name__}"
            log.warning(
                "web_account_failed", username=account.username, error=str(exc)[:120]
            )
        finally:
            transport.close()

    return merged, names, status
