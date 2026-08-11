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
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from stories_monitor.ai.client import get_ai_client  # noqa: E402
from stories_monitor.ai.ocr import get_ocr_engine  # noqa: E402
from sqlalchemy import update  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from stories_monitor.config import get_settings  # noqa: E402
from stories_monitor.db.models import Story, StoryAnalysis, Target  # noqa: E402
from stories_monitor.db.session import session_scope  # noqa: E402
from stories_monitor.logging_setup import configure_logging  # noqa: E402
from stories_monitor.transport.web import WebTransport, story_age_hours  # noqa: E402


def _remember(item: Any, user_id: int, username: str) -> bool:
    """Записать сторис в БД. True - новая, False - уже видели.

    Дедупликация та же, что и в фетчере: INSERT ... ON CONFLICT (story_id)
    DO NOTHING. Конфликт = уже обрабатывали, и это единственный механизм
    (§7.3). Видео сразу получают `skipped_video` и в AI не попадают (§1).
    """
    taken = dt.datetime.fromtimestamp(item.taken_at, tz=dt.UTC)
    state = "skipped_video" if not item.is_photo else "new"

    with session_scope() as session:
        # Цель должна существовать - FK на targets.
        session.execute(
            pg_insert(Target)
            .values(
                user_id=user_id,
                username=username,
                instagram_url=f"https://instagram.com/{username}",
                shard_id=0,
                status="active",
            )
            .on_conflict_do_nothing(index_elements=["user_id"])
        )

    with session_scope() as session:
        inserted = session.execute(
            pg_insert(Story)
            .values(
                story_id=item.story_id,
                target_user_id=user_id,
                taken_at=taken,
                expiring_at=(
                    dt.datetime.fromtimestamp(item.expiring_at, tz=dt.UTC)
                    if item.expiring_at
                    else None
                ),
                media_type=item.media_type,
                pipeline_state=state,
            )
            .on_conflict_do_nothing(index_elements=["story_id"])
            .returning(Story.story_id)
        ).scalar_one_or_none()

    return inserted is not None


def _save_analysis(
    story_id: str,
    ocr_text: str,
    cheap: Any,
    final_score: int,
    explanation: str,
    smart_score: int | None = None,
) -> None:
    """Сохранить результат анализа. Upsert по story_id - повторный прогон
    перезаписывает, а не дублирует."""
    values = {
        "story_id": story_id,
        "ocr_text": ocr_text or None,
        "cheap_score": cheap.score,
        "cheap_result": cheap.model_dump(mode="json"),
        "smart_score": smart_score,
        "final_score": final_score,
        "service_category": cheap.service_category,
        "intent_type": (
            "seeking_contractor" if cheap.seeking_contractor
            else "purchase_intent" if cheap.explicit_purchase_intent
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


def _already_analysed(story_id: str) -> bool:
    """Уже прогоняли через AI? Проверяем story_analysis, не stories."""
    with session_scope() as session:
        return session.get(StoryAnalysis, story_id) is not None


def _mark(story_id: str, state: str) -> None:
    """Продвинуть состояние сторис после анализа."""
    with session_scope() as session:
        session.execute(
            update(Story).where(Story.story_id == story_id).values(pipeline_state=state)
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookies", default=None)
    ap.add_argument("--cookies-file", default=None)
    ap.add_argument("--limit", type=int, default=10, help="Сколько фото классифицировать")
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

    # Дедупликация: та же гарантия, что и в фетчере - INSERT ... ON CONFLICT
    # (story_id) DO NOTHING. Конфликт означает "уже видели", и сторис больше
    # никогда не скачивается и не уходит в AI (§7.3).
    fresh: list[tuple[int, Any]] = []
    seen_before = 0
    for uid, item in photos:
        _remember(item, uid, names.get(uid, str(uid)))
        # Критерий "уже платили" - наличие строки в story_analysis, а не в
        # stories. Сторис попадает в stories при первом же обнаружении, задолго
        # до того, как её увидит AI.
        if _already_analysed(item.story_id):
            seen_before += 1
        else:
            fresh.append((uid, item))

    for uid, items in reels.items():
        for item in items:
            if not item.is_photo:
                _remember(item, uid, names.get(uid, str(uid)))

    print(
        f"найдено: {len(photos)} фото | {videos} видео пропущено (§1)\n"
        f"уже анализировали ранее: {seen_before} | новых к анализу: {len(fresh)}"
    )
    if not fresh:
        print("\nНовых фото нет - все уже проходили через AI. Платить второй раз не за что.")
        transport.close()
        return 0

    print(f"классифицирую {min(args.limit, len(fresh))} самых свежих\n")
    print("=" * 78)
    photos = fresh

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
            _save_analysis(
                item.story_id, text, cheap, final, explanation,
                smart_score=(final if route == "smart" else None),
            )
            _mark(item.story_id, "analyzed")
            results.append((name, final, cheap.service_category or "-", route))
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
    print(f"\nобработано {len(results)} фото | лидов (score >= {settings.approval_score_min}): {len(leads)}")
    for name, score, category, _ in leads:
        print(f"  ЛИД  @{name} — {score}/10 — {category}")

    if not leads:
        print("  (это ожидаемо: обычные личные сторис — не заявки на услуги)")

    transport.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
