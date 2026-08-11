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

from .db.models import WorkerAccount
from .db.session import session_scope
from .logging_setup import get_logger
from .transport.web import COOKIE_NAMES, WebTransport, parse_cookie_header

log = get_logger(__name__)

# Ключ внутри session_json, чтобы веб-куки не затирали мобильную сессию.
_WEB_KEY = "web_cookies"
_SAVED_AT = "web_cookies_saved_at"


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
    """Сохранить веб-куки для аккаунта. Мобильную сессию не трогает."""
    jar = parse_cookie_header(raw) if isinstance(raw, str) else dict(raw)
    if not jar.get("sessionid"):
        raise ValueError("в куки нет sessionid")

    with session_scope() as session:
        account = session.scalars(
            select(WorkerAccount).where(WorkerAccount.username == username)
        ).first()
        if account is None:
            raise ValueError(
                f"{username} не заведён - сначала `stories seed-worker`"
            )

        # Мержим, а не заменяем: мобильная сессия должна пережить это.
        blob = dict(account.session_json or {})
        blob[_WEB_KEY] = jar
        blob[_SAVED_AT] = dt.datetime.now(dt.UTC).isoformat()
        account.session_json = blob
        session.flush()

        result = WebAccount(
            id=account.id,
            username=account.username,
            shard_id=account.shard_id,
            cookies=jar,
            saved_at=blob[_SAVED_AT],
        )

    log.info(
        "web_cookies_saved",
        username=username,
        cookies=len(jar),
        missing=result.missing_cookies,
    )
    return result


def load_accounts(username: str | None = None) -> list[WebAccount]:
    """Аккаунты с сохранёнными веб-куки. Без аргумента - все."""
    accounts: list[WebAccount] = []
    with session_scope() as session:
        stmt = select(WorkerAccount).order_by(WorkerAccount.id)
        if username:
            stmt = stmt.where(WorkerAccount.username == username)
        for account in session.scalars(stmt).all():
            blob = account.session_json or {}
            jar = blob.get(_WEB_KEY)
            if not isinstance(jar, dict) or not jar.get("sessionid"):
                continue
            accounts.append(
                WebAccount(
                    id=account.id,
                    username=account.username,
                    shard_id=account.shard_id,
                    cookies=dict(jar),
                    saved_at=blob.get(_SAVED_AT),
                )
            )
    return accounts


def clear_cookies(username: str) -> bool:
    """Удалить веб-куки, оставив мобильную сессию нетронутой."""
    with session_scope() as session:
        account = session.scalars(
            select(WorkerAccount).where(WorkerAccount.username == username)
        ).first()
        if account is None or not account.session_json:
            return False
        blob = dict(account.session_json)
        had = blob.pop(_WEB_KEY, None) is not None
        blob.pop(_SAVED_AT, None)
        account.session_json = blob
        return had


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
        return True, f"{tray.entry_count} записей, {len(users)} с активными сторис"
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
                f"OK - {len(users)} подписок со сторис, {new_users} новых для пула"
            )
            log.info(
                "web_account_polled",
                username=account.username,
                tray_entries=tray.entry_count,
                users_with_stories=len(users),
            )
        except Exception as exc:  # noqa: BLE001 - один мёртвый аккаунт не рушит сбор
            status[account.username] = f"НЕ РАБОТАЕТ - {type(exc).__name__}"
            log.warning(
                "web_account_failed", username=account.username, error=str(exc)[:120]
            )
        finally:
            transport.close()

    return merged, names, status
