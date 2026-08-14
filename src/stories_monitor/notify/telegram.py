"""Телеграм-алерты о состоянии системы.

Отдельно от Slack: Slack получает найденные лиды, Telegram - сообщения о том,
что система перестала работать. Разные адресаты и разная срочность.

Главный случай: веб-куки истекли. Их нельзя продлить из кода, поэтому без
уведомления вы узнаете о простое по пропавшим лидам - то есть через часы.

Алерты дедуплицируются: одно и то же состояние не шлётся чаще, чем раз в
`alert_cooldown_sec`, иначе цикл раз в минуту превратится в спам.
"""

from __future__ import annotations

import datetime as dt

import httpx

from ..config import get_settings
from ..logging_setup import get_logger

log = get_logger(__name__)

_API = "https://api.telegram.org/bot{token}/sendMessage"

# Когда какой алерт отправляли в последний раз (в пределах процесса).
_last_sent: dict[str, dt.datetime] = {}


def send_alert(kind: str, text: str, *, force: bool = False) -> bool:
    """Отправить алерт. `kind` - ключ дедупликации.

    Возвращает True, если сообщение ушло. False означает "не настроено" или
    "недавно уже слали" - и то, и другое нормально, а не ошибка.
    """
    settings = get_settings()
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id

    if not token or not chat_id:
        log.debug("telegram_not_configured", kind=kind)
        return False

    now = dt.datetime.now(dt.UTC)
    if not force:
        last = _last_sent.get(kind)
        if last and (now - last).total_seconds() < settings.alert_cooldown_sec:
            return False

    try:
        response = httpx.post(
            _API.format(token=token),
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        # Сбой алерта не должен ронять цикл - иначе мониторинг сам себе враг.
        log.warning("telegram_send_failed", kind=kind, error=str(exc)[:150])
        return False

    _last_sent[kind] = now
    log.info("telegram_alert_sent", kind=kind)
    return True


def alert_cookies_expired(usernames: list[str]) -> bool:
    """Куки истекли - нужен человек с браузером."""
    accounts = ", ".join(f"@{u}" for u in usernames) or "(нет аккаунтов)"
    return send_alert(
        "cookies_expired",
        "<b>Instagram: куки истекли</b>\n\n"
        f"Аккаунты: {accounts}\n\n"
        "Сбор сторис остановлен. Продлить сессию из кода нельзя.\n\n"
        "Что сделать:\n"
        "1. Зайти в instagram.com в браузере\n"
        "2. F12 → Application → Cookies\n"
        "3. Обновить строку в таблице <code>cookies</code> в Supabase "
        "и поставить <code>is_active = true</code>",
    )


def alert_no_accounts() -> bool:
    """В таблице cookies нет ни одной активной строки."""
    return send_alert(
        "no_accounts",
        "<b>Instagram: нет рабочих аккаунтов</b>\n\n"
        "В таблице <code>cookies</code> нет активных строк — "
        "цикл ничего не собирает.",
    )


def alert_cycle_failing(consecutive: int, error: str) -> bool:
    """Цикл падает подряд несколько раз - что-то сломано, а не моргнуло."""
    return send_alert(
        "cycle_failing",
        f"<b>Instagram: цикл падает</b>\n\n"
        f"Неудачных подряд: {consecutive}\n"
        f"Ошибка: <code>{error[:200]}</code>",
    )


def alert_lead(username: str, score: int, category: str, explanation: str) -> bool:
    """Найден лид. Отдельный ключ на сторис - каждый лид приходит один раз."""
    text = (
        "<b>Новый лид</b>\n\n"
        f"Аккаунт: @{username}\n"
        f"Ссылка: https://instagram.com/{username}\n"
        f"Категория: {category or '-'}\n"
        f"Оценка: {score}/10"
    )
    if explanation:
        text += f"\n\n{explanation[:300]}"
    # force=True: лиды не дедуплицируются между собой, их защищает
    # slack_deliveries.story_id на стороне вызывающего.
    return send_alert(f"lead:{username}:{score}", text, force=True)


def reset_cooldowns() -> None:
    """Тестовый хук - сбросить дедупликацию."""
    _last_sent.clear()
