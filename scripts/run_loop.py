"""Постоянный процесс: цикл каждые N секунд.

Для платформ без cron (Out Plane и подобные) - там контейнер должен работать
непрерывно, а не запускаться по расписанию.

    python scripts/run_loop.py                 # каждые 60с
    LOOP_INTERVAL_SEC=120 python scripts/run_loop.py

Отличия от `run_once.py`:

- не завершается, спит между циклами
- переживает ошибку в цикле: залогирует и пойдёт дальше
- корректно реагирует на SIGTERM, чтобы платформа не убивала его силой

Всё состояние в Postgres, поэтому перезапуск ничего не теряет.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import signal
import sys
import time
from types import FrameType

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from stories_monitor import activity  # noqa: E402
from stories_monitor.logging_setup import configure_logging, get_logger  # noqa: E402
from stories_monitor.notify.telegram import (  # noqa: E402
    alert_cookies_expired,
    alert_cycle_failing,
    alert_no_accounts,
)
from stories_monitor.webaccounts import collect_stories, load_accounts  # noqa: E402

log = get_logger("run_loop")

_stopping = False


def _handle_stop(signum: int, _frame: FrameType | None) -> None:
    """Досматриваем текущий цикл и выходим - иначе платформа убьёт по таймауту."""
    global _stopping
    _stopping = True
    log.info("shutdown_requested", signal=signum)


def _cycle(limit: int) -> bool:
    """Один цикл. True - хотя бы один аккаунт ответил."""
    from web_classify import _classify

    accounts = load_accounts()
    if not accounts:
        log.error("no_accounts", detail="в таблице cookies нет активных строк")
        alert_no_accounts()
        return False

    activity.record(
        "system", "cycle",
        message=f"Cycle started across {len(accounts)} active sessions",
        targets=[a.username for a in accounts],
        item_count=len(accounts),
    )

    reels, names, status = collect_stories(accounts)
    working = [u for u, s in status.items() if s.startswith("OK")]
    if not working:
        # Куки протухли. Продлить их из кода нельзя - нужен человек.
        log.error(
            "all_accounts_failed",
            accounts=list(status),
            detail="обновите куки в таблице cookies",
        )
        # Куки продлить из кода нельзя - без уведомления простой заметят
        # только по пропавшим лидам.
        expired = [u for u, s in status.items() if "ИСТЕКЛИ" in s]
        alert_cookies_expired(expired or list(status))
        return False

    _classify(reels, names, limit)
    activity.record(
        "system", "cycle",
        message=f"Cycle finished — {len(working)}/{len(accounts)} sessions OK",
        item_count=len(working),
    )
    # Иначе таблица растёт без границ: 11 аккаунтов в минуту.
    activity.prune()
    return True


def main() -> int:
    configure_logging(json_output=True)
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    # SCRAPER_OFFLINE=true - выйти, не сделав ни одного запроса к Instagram.
    # Проверка стоит до всего остального: смысл флага в том, чтобы НИЧЕГО не
    # произошло, включая чтение аккаунтов из базы.
    from stories_monitor.config import get_settings

    if get_settings().scraper_offline:
        log.info(
            "offline_mode",
            detail="SCRAPER_OFFLINE=true - сбор выключен, запросов к Instagram нет",
        )
        return 0

    interval = int(os.environ.get("LOOP_INTERVAL_SEC", "120"))
    limit = int(os.environ.get("LOOP_PHOTO_LIMIT", "25"))
    # Сколько джиттера добавлять к интервалу, в долях. 0.5 при интервале 120с
    # даёт разброс 60-180с. Ноль возвращает старое поведение.
    jitter = float(os.environ.get("LOOP_JITTER", "0.5"))
    log.info("loop_started", interval_sec=interval, photo_limit=limit, jitter=jitter)

    consecutive_failures = 0

    while not _stopping:
        started = time.monotonic()
        try:
            ok = _cycle(limit)
            consecutive_failures = 0 if ok else consecutive_failures + 1
        except Exception as exc:  # noqa: BLE001 - цикл не должен умирать
            consecutive_failures += 1
            log.exception("cycle_failed", error=str(exc)[:200])
            if consecutive_failures >= 3:
                alert_cycle_failing(consecutive_failures, str(exc))

        elapsed = time.monotonic() - started

        # При стойких отказах разрежаем попытки: если куки мертвы, долбить
        # Instagram раз в минуту бессмысленно и вредно.
        backoff = min(consecutive_failures, 5) * interval

        # Живой человек не открывает Instagram ровно раз в 60 секунд. Ровный
        # интервал - самый дешёвый признак автоматизации, какой только можно
        # подарить: он виден даже без анализа содержимого запросов.
        #
        # Множитель считает webaccounts.pace_multiplier: случайный разброс
        # 0.25x-1.5x, а ночью дополнительно умноженный на NIGHT_SLOWDOWN, когда
        # цели почти не публикуют. LOOP_JITTER оставлен для совместимости: 0
        # возвращает ровный интервал.
        from stories_monitor.webaccounts import in_quiet_hours, pace_multiplier

        target = interval * (pace_multiplier() if jitter else 1.0)
        sleep_for = max(1.0, target - elapsed) + backoff

        log.info(
            "cycle_done",
            duration_sec=round(elapsed, 1),
            sleep_sec=round(sleep_for, 1),
            next_interval_sec=round(target, 1),
            night=in_quiet_hours(),
            consecutive_failures=consecutive_failures,
            timestamp=dt.datetime.now(dt.UTC).isoformat(),
        )

        # Спим короткими отрезками, чтобы SIGTERM не ждал полный интервал.
        deadline = time.monotonic() + sleep_for
        while time.monotonic() < deadline and not _stopping:
            time.sleep(min(2.0, deadline - time.monotonic()))

    log.info("loop_stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
