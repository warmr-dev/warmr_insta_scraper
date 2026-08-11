"""Прочитать сторис через ВЕБ-API, используя куки из браузера.

    python scripts/web_stories.py --cookies 'sessionid=...; csrftoken=...; ig_did=...'
    python scripts/web_stories.py --cookies-file cookies.txt
    IG_WEB_COOKIES='...' python scripts/web_stories.py

Одного `sessionid` НЕ достаточно: ленты отвечают 302 без сопутствующих куки.
Скопируйте весь набор:

    instagram.com → F12 → Application → Cookies → https://www.instagram.com

Нужны: sessionid, csrftoken, ds_user_id, ig_did, mid, datr, rur

Быстрый способ — в консоли браузера (F12 → Console) выполнить:

    copy(document.cookie)

и вставить сюда. Обратите внимание: `document.cookie` не отдаёт HttpOnly-куки
(в том числе sessionid), поэтому его всё равно придётся взять из вкладки
Application и дописать вручную.

Только чтение: `media/seen/` и любые write-эндпоинты не вызываются (§11).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from stories_monitor.logging_setup import configure_logging  # noqa: E402
from stories_monitor.transport.web import (  # noqa: E402
    COOKIE_NAMES,
    WebTransport,
    parse_cookie_header,
    story_age_hours,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookies", default=None, help="Строка куки из браузера")
    ap.add_argument("--cookies-file", default=None, help="Файл со строкой куки")
    ap.add_argument("--limit", type=int, default=50, help="Максимум аккаунтов за раз")
    args = ap.parse_args()
    configure_logging(json_output=False)

    raw = args.cookies or os.environ.get("IG_WEB_COOKIES", "")
    if args.cookies_file:
        path = pathlib.Path(args.cookies_file)
        if not path.is_file():
            print(f"Файл не найден: {path}")
            print("\nСоздайте его так (куки: F12 → Application → Cookies → instagram.com):")
            print("  cat > cookies.txt <<'EOF'")
            print("  sessionid=...; csrftoken=...; ds_user_id=...; ig_did=...; mid=...; datr=...; rur=...")
            print("  EOF")
            return 2
        raw = path.read_text()
    if not raw:
        print("Куки не переданы. Укажите --cookies, --cookies-file или IG_WEB_COOKIES.")
        print("Нужны:", ", ".join(COOKIE_NAMES))
        return 2

    jar = parse_cookie_header(raw)
    present = [n for n in COOKIE_NAMES if n in jar]
    missing = [n for n in COOKIE_NAMES if n not in jar]
    print(f"куки: есть {len(present)}/{len(COOKIE_NAMES)} — {', '.join(present)}")
    if missing:
        print(f"      отсутствуют: {', '.join(missing)}")

    transport = WebTransport(jar)

    # `accounts/current_user/` answers 400 to web cookies even when the feeds
    # work, so it is a nice-to-have, never a gate.
    try:
        who = transport.whoami()
        print(f"\nаккаунт: @{who or '?'}")
    except Exception:  # noqa: BLE001
        print(f"\nаккаунт: id={jar.get('ds_user_id', '?')} (current_user недоступен - это нормально)")

    try:
        tray = transport.reels_tray()
    except Exception as exc:  # noqa: BLE001
        print(f"reels_tray не отвечает: {type(exc).__name__}: {exc}")
        return 1

    users = [e for e in tray.entries if e.is_user_entry]
    highlights = tray.entry_count - len(users)
    print(
        f"трей: {tray.entry_count} записей | {len(users)} с активными сторис "
        f"| {highlights} highlights отсеяно"
    )
    if not users:
        print("\nПодписок с активными сторис нет — подпишитесь на кого-нибудь.")
        return 0

    ids = [e.user_id for e in users[: args.limit] if e.user_id]
    names = {e.user_id: e.user.get("username", str(e.id)) for e in users}

    try:
        reels = transport.reels_media(ids)
    except Exception as exc:  # noqa: BLE001
        print(f"\nreels_media не отвечает: {type(exc).__name__}: {exc}")
        return 1

    now = dt.datetime.now(dt.UTC)
    photos = videos = 0
    print()
    for user_id, items in reels.items():
        if not items:
            continue
        print(f"@{names.get(user_id, user_id):20} {len(items)} сторис")
        for item in items:
            kind = "ФОТО " if item.is_photo else "видео"
            if item.is_photo:
                photos += 1
            else:
                videos += 1
            print(f"   [{kind}] {item.story_id}  {story_age_hours(item, now):.1f}ч назад")

    print(f"\nИТОГО: {photos} фото → AI | {videos} видео → пропускаем (§1)")
    transport.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
