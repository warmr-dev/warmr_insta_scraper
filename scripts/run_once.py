"""Один цикл: собрать сторис со всех аккаунтов и классифицировать новые фото.

Точка входа для хостинга (Railway Cron, systemd timer, k8s CronJob):

    python scripts/run_once.py

Куки читаются из таблицы `cookies` в Supabase - править их можно прямо в
интерфейсе, когда истекут.

Скрипт рассчитан на запуск раз в минуту:

- повторно ничего не оплачивается (дедупликация по `story_analysis`)
- один нерабочий аккаунт не рушит прогон, а помечается в БД
- завершается сам, ничего не ждёт

Выходной код: 0 - успех, 1 - ни один аккаунт не ответил (нужны новые куки).
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from stories_monitor.config import get_settings  # noqa: E402
from stories_monitor.logging_setup import configure_logging, get_logger  # noqa: E402
from stories_monitor.webaccounts import collect_stories, load_accounts  # noqa: E402

log = get_logger("run_once")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Максимум фото за один цикл - защита от всплеска расходов",
    )
    ap.add_argument("--account", default=None, help="Только этот аккаунт")
    args = ap.parse_args()

    # На хостинге логи должны быть машиночитаемыми.
    configure_logging(json_output=True)

    # Тот же выключатель, что и у run_loop.py: платформа может запускать этот
    # скрипт по cron, и пауза должна действовать на оба входа одинаково.
    if get_settings().scraper_offline:
        log.info(
            "offline_mode",
            detail="SCRAPER_OFFLINE=true - сбор выключен, запросов к Instagram нет",
        )
        return 0

    started = dt.datetime.now(dt.UTC)

    accounts = load_accounts(args.account)
    if not accounts:
        log.error(
            "no_accounts",
            detail="в таблице cookies нет активных строк - добавьте куки",
        )
        return 1

    reels, names, status = collect_stories(accounts)
    working = [u for u, s in status.items() if s.startswith("OK")]

    if not working:
        # Все куки протухли. Продлить их из кода нельзя - нужен человек.
        log.error(
            "all_accounts_failed",
            accounts=list(status),
            detail="обновите куки в таблице cookies через Supabase",
        )
        return 1

    # Классификация переиспользует ту же логику, что и ручной запуск.
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from web_classify import _classify

    _classify(reels, names, args.limit)

    settings = get_settings()
    log.info(
        "cycle_done",
        accounts_ok=len(working),
        accounts_failed=len(status) - len(working),
        users_with_stories=len(reels),
        duration_sec=round((dt.datetime.now(dt.UTC) - started).total_seconds(), 1),
        ai_provider=settings.ai_provider,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
