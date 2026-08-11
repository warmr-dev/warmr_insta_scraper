"""Скачать фото-сторис через веб-API и прогнать через AI-классификатор.

    python scripts/web_classify.py --cookies '<полный набор куки>' --limit 10

Полный путь: reels_tray -> reels_media -> только ФОТО -> OCR -> дешёвая модель
-> (5-6) умная модель -> оценка.

Видео пропускаются до скачивания и до любого обращения к AI (§1), поэтому денег
не стоят.

Временный файл удаляется в `finally` - медиа не хранится никогда (§7.4, §11).
Только чтение: `media/seen/` не вызывается.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from stories_monitor.ai.client import get_ai_client  # noqa: E402
from stories_monitor.ai.ocr import get_ocr_engine  # noqa: E402
from stories_monitor.config import get_settings  # noqa: E402
from stories_monitor.logging_setup import configure_logging  # noqa: E402
from stories_monitor.transport.web import WebTransport, story_age_hours  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookies", default=None)
    ap.add_argument("--cookies-file", default=None)
    ap.add_argument("--limit", type=int, default=10, help="Сколько фото классифицировать")
    args = ap.parse_args()
    configure_logging(json_output=False)

    raw = args.cookies or os.environ.get("IG_WEB_COOKIES", "")
    if args.cookies_file:
        raw = pathlib.Path(args.cookies_file).read_text()
    if not raw:
        print("Куки не переданы (--cookies / --cookies-file / IG_WEB_COOKIES)")
        return 2

    settings = get_settings()
    transport = WebTransport(raw)
    client = get_ai_client()
    ocr = get_ocr_engine(client=client)

    print(f"AI: {type(client).__name__} | cheap={settings.cheap_model}")
    print(f"OCR: {getattr(ocr, 'name', '?')}\n")

    tray = transport.reels_tray()
    users = [e for e in tray.entries if e.is_user_entry]
    reels = transport.reels_media([e.user_id for e in users if e.user_id])
    names = {e.user_id: e.user.get("username", str(e.id)) for e in users}

    # Только фото. Видео отсекаются здесь - до скачивания, до AI (§1).
    photos = [
        (uid, item)
        for uid, items in reels.items()
        for item in items
        if item.is_photo
    ]
    videos = sum(1 for items in reels.values() for i in items if not i.is_photo)
    photos.sort(key=lambda p: p[1].taken_at, reverse=True)  # свежие первыми

    print(f"найдено: {len(photos)} фото | {videos} видео пропущено (§1)")
    print(f"классифицирую {min(args.limit, len(photos))} самых свежих\n")
    print("=" * 78)

    now = dt.datetime.now(dt.UTC)
    results: list[tuple[str, int, str, str]] = []

    for uid, item in photos[: args.limit]:
        name = names.get(uid, str(uid))
        url = item.best_image_url()
        if not url:
            continue

        fd, path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        try:
            transport.download_media(url, path)
            text = ""
            try:
                text = ocr.extract_text(path)
            except Exception:  # noqa: BLE001 - OCR потерять сторис не должен
                pass

            cheap = client.call_cheap(path, text)
            route = (
                "reject"
                if cheap.score < settings.smart_model_score_min
                else "smart"
                if cheap.score <= settings.smart_model_score_max
                else "accept"
            )
            final = cheap.score
            explanation = ""
            if route == "smart":
                smart = client.call_smart(path, text, cheap)
                final = smart.final_score
                explanation = smart.explanation

            flag = "ЛИД" if final >= settings.approval_score_min else "   "
            print(
                f"{flag} @{name:20} score={cheap.score:>2} → {final:>2}  [{route}]"
                f"  {story_age_hours(item, now):.1f}ч"
            )
            if text.strip():
                print(f"      текст: {text.strip()[:70]}")
            if cheap.service_category:
                print(
                    f"      категория: {cheap.service_category}"
                    f"{' | гео: ' + cheap.geography if cheap.geography else ''}"
                )
            if explanation:
                print(f"      {explanation[:100]}")
            results.append((name, final, cheap.service_category or "-", route))
        except Exception as exc:  # noqa: BLE001 - одна плохая сторис не рушит прогон
            print(f"    @{name:20} ошибка: {type(exc).__name__}: {str(exc)[:60]}")
        finally:
            # Медиа не переживает анализ (§7.4, §11).
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    print("=" * 78)
    leads = [r for r in results if r[1] >= settings.approval_score_min]
    print(f"\nобработано {len(results)} фото | лидов (score >= {settings.approval_score_min}): {len(leads)}")
    for name, score, category, _ in leads:
        print(f"  ЛИД  @{name} — {score}/10 — {category}")

    if not leads:
        print("  (это ожидаемо: обычные личные сторис — не заявки на услуги)")

    transport.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
