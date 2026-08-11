"""Классификация фото-сторис через веб-API.

Один аккаунт:
    python scripts/web_classify.py --cookies-file cookies.txt --limit 20

Все аккаунты с сохранёнными веб-куки (см. `stories web-add`):
    python scripts/web_classify.py --all-accounts --limit 20
    python scripts/web_classify.py --account yrsayl7

Путь: reels_tray -> reels_media -> только ФОТО -> OCR -> дешёвая модель
-> (5-6) умная модель -> оценка.

Видео отсекаются до скачивания и до любого обращения к AI (§1) - денег не стоят.
Временный файл удаляется в `finally` (§7.4, §11). Только чтение: `media/seen/`
не вызывается.

Повторно ничего не оплачивается: критерий "уже платили" - строка в
`story_analysis`, а не в `stories` (туда сторис попадает при первом появлении
в трее, задолго до AI).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import sys
import tempfile
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402
from sqlalchemy import select, update  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from stories_monitor.ai.client import get_ai_client  # noqa: E402
from stories_monitor.ai.ocr import get_ocr_engine  # noqa: E402
from stories_monitor.config import get_settings  # noqa: E402
from stories_monitor.db.models import Story, StoryAnalysis, Target  # noqa: E402
from stories_monitor.db.session import session_scope  # noqa: E402
from stories_monitor.logging_setup import configure_logging  # noqa: E402
from stories_monitor.transport.web import WebTransport, story_age_hours  # noqa: E402


# --- сохранение и дедупликация ------------------------------------------------


def _remember_all(reels: dict[int, list[Any]], names: dict[int, str]) -> set[str]:
    """Записать ВСЕ сторис двумя запросами и вернуть уже проанализированные.

    Раньше это были три round-trip на каждую сторис. На удалённой БД (Supabase
    в Сиднее, ~2.3с на запрос) 145 сторис превращались в ~17 минут - при
    запуске раз в минуту это неприемлемо. Теперь весь пул уходит пачкой.
    """
    targets: list[dict[str, Any]] = []
    stories: list[dict[str, Any]] = []
    seen_ids: list[str] = []

    for user_id, items in reels.items():
        username = names.get(user_id, str(user_id))
        targets.append(
            {
                "user_id": user_id,
                "username": username,
                "instagram_url": f"https://instagram.com/{username}",
                "shard_id": 0,
                "status": "active",
            }
        )
        for item in items:
            seen_ids.append(item.story_id)
            stories.append(
                {
                    "story_id": item.story_id,
                    "target_user_id": user_id,
                    "taken_at": dt.datetime.fromtimestamp(item.taken_at, tz=dt.UTC),
                    "expiring_at": (
                        dt.datetime.fromtimestamp(item.expiring_at, tz=dt.UTC)
                        if item.expiring_at
                        else None
                    ),
                    "media_type": item.media_type,
                    # Видео оседают здесь и до AI не доходят (§1).
                    "pipeline_state": "skipped_video" if not item.is_photo else "new",
                }
            )

    with session_scope() as session:
        if targets:
            session.execute(
                pg_insert(Target).on_conflict_do_nothing(index_elements=["user_id"]),
                targets,
            )
        if stories:
            session.execute(
                pg_insert(Story).on_conflict_do_nothing(index_elements=["story_id"]),
                stories,
            )

    # Одним запросом: что из этого пула уже прогонялось через AI.
    if not seen_ids:
        return set()
    with session_scope() as session:
        rows = session.scalars(
            select(StoryAnalysis.story_id).where(StoryAnalysis.story_id.in_(seen_ids))
        ).all()
    return set(rows)


def _save_analysis(
    story_id: str,
    ocr_text: str,
    cheap: Any,
    final_score: int,
    explanation: str,
    smart_score: int | None = None,
) -> None:
    """Upsert по story_id - повторный прогон перезапишет, а не задублирует."""
    values = {
        "story_id": story_id,
        "ocr_text": ocr_text or None,
        "cheap_score": cheap.score,
        "cheap_result": cheap.model_dump(mode="json"),
        "smart_score": smart_score,
        "final_score": final_score,
        "service_category": cheap.service_category,
        "intent_type": (
            "seeking_contractor"
            if cheap.seeking_contractor
            else "purchase_intent"
            if cheap.explicit_purchase_intent
            else None
        ),
        "ai_explanation": explanation or None,
        "analyzed_at": dt.datetime.now(dt.UTC),
    }
    with session_scope() as session:
        session.execute(
            pg_insert(StoryAnalysis)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["story_id"],
                set_={k: v for k, v in values.items() if k != "story_id"},
            )
        )


def _mark(story_id: str, state: str) -> None:
    with session_scope() as session:
        session.execute(
            update(Story).where(Story.story_id == story_id).values(pipeline_state=state)
        )


def _download(url: str, dest: str) -> str:
    """Скачать медиа. URL живут недолго - одна повторная попытка (§7.3)."""
    for attempt in (1, 2):
        try:
            response = httpx.get(
                url,
                timeout=30,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
            with open(dest, "wb") as handle:
                handle.write(response.content)
            return dest
        except Exception:  # noqa: BLE001
            if attempt == 2:
                raise
    return dest


# --- классификация ------------------------------------------------------------


def _classify(reels: dict[int, list[Any]], names: dict[int, str], limit: int) -> int:
    """Общая часть для обоих режимов: пул сторис -> оценки."""
    settings = get_settings()
    client = get_ai_client()
    ocr = get_ocr_engine(client=client)
    print(f"AI: {type(client).__name__} | cheap={settings.cheap_model}\n")

    photos = [
        (uid, item) for uid, items in reels.items() for item in items if item.is_photo
    ]
    videos = sum(1 for items in reels.values() for i in items if not i.is_photo)
    photos.sort(key=lambda p: p[1].taken_at, reverse=True)

    analysed = _remember_all(reels, names)
    fresh = [(uid, item) for uid, item in photos if item.story_id not in analysed]
    seen_before = len(photos) - len(fresh)

    print(
        f"найдено: {len(photos)} фото | {videos} видео пропущено (§1)\n"
        f"уже анализировали ранее: {seen_before} | новых к анализу: {len(fresh)}"
    )
    if not fresh:
        print("\nНовых фото нет - все уже проходили через AI. Платить второй раз не за что.")
        return 0

    print(f"классифицирую {min(limit, len(fresh))} самых свежих\n")
    print("=" * 78)

    now = dt.datetime.now(dt.UTC)
    results: list[tuple[str, int, str]] = []

    for uid, item in fresh[:limit]:
        name = names.get(uid, str(uid))
        url = item.best_image_url()
        if not url:
            continue

        fd, path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        try:
            _download(url, path)
            text = ""
            try:
                text = ocr.extract_text(path)
            except Exception:  # noqa: BLE001 - OCR не должен терять сторис
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
                geo = f" | гео: {cheap.geography}" if cheap.geography else ""
                print(f"      категория: {cheap.service_category}{geo}")
            if explanation:
                print(f"      {explanation[:100]}")

            _save_analysis(
                item.story_id,
                text,
                cheap,
                final,
                explanation,
                smart_score=(final if route == "smart" else None),
            )
            _mark(item.story_id, "analyzed")
            results.append((name, final, cheap.service_category or "-"))
        except Exception as exc:  # noqa: BLE001 - одна плохая сторис не рушит прогон
            print(f"    @{name:20} ошибка: {type(exc).__name__}: {str(exc)[:60]}")
            _mark(item.story_id, "failed")
        finally:
            # Медиа не переживает анализ (§7.4, §11).
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    print("=" * 78)
    leads = [r for r in results if r[1] >= settings.approval_score_min]
    print(
        f"\nобработано {len(results)} фото | "
        f"лидов (score >= {settings.approval_score_min}): {len(leads)}"
    )
    for name, score, category in leads:
        print(f"  ЛИД  @{name} — {score}/10 — {category}")
    if not leads:
        print("  (это ожидаемо: обычные личные сторис — не заявки на услуги)")
    return 0


# --- точка входа --------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookies", default=None)
    ap.add_argument("--cookies-file", default=None)
    ap.add_argument("--limit", type=int, default=10, help="Сколько фото классифицировать")
    ap.add_argument(
        "--all-accounts",
        action="store_true",
        help="Собрать сторис со ВСЕХ аккаунтов с сохранёнными веб-куки",
    )
    ap.add_argument("--account", default=None, help="Только этот аккаунт (из web-add)")
    args = ap.parse_args()
    configure_logging(json_output=False)

    # Пул аккаунтов: объединение подписок покрывает больше целей, чем любой
    # аккаунт поодиночке - это §1 применительно к веб-куки.
    if args.all_accounts or args.account:
        from stories_monitor.webaccounts import collect_stories, load_accounts

        pool = load_accounts(args.account)
        if not pool:
            print("Нет аккаунтов с веб-куки.")
            print("Добавить: stories web-add <username> --cookies-file cookies.txt")
            return 2
        print(
            f"аккаунтов в пуле: {len(pool)} — "
            f"{', '.join('@' + a.username for a in pool)}\n"
        )
        reels, names, status = collect_stories(pool)
        for who, state in status.items():
            print(f"  @{who:22} {state}")
        print()
        return _classify(reels, names, args.limit)

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
        print("Куки не переданы (--cookies / --cookies-file / IG_WEB_COOKIES)")
        print("Либо используйте --all-accounts после `stories web-add`.")
        return 2

    transport = WebTransport(raw)
    try:
        tray = transport.reels_tray()
        users = [e for e in tray.entries if e.is_user_entry]
        reels = transport.reels_media([e.user_id for e in users if e.user_id])
        names = {e.user_id: e.user.get("username", str(e.id)) for e in users}
    finally:
        transport.close()

    return _classify(reels, names, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
