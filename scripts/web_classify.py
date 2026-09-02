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
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402
from sqlalchemy import select, update  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from stories_monitor import activity  # noqa: E402
from stories_monitor.ai.client import get_ai_client  # noqa: E402
from stories_monitor.ai.ocr import get_ocr_engine  # noqa: E402
from stories_monitor.config import get_settings  # noqa: E402

# Отправка в Slack. Идемпотентность даёт первичный ключ slack_deliveries.story_id:
# один лид отправляется РОВНО один раз, даже если цикл перезапустится (§7.6).
from stories_monitor.db.models import (  # noqa: E402
    SlackDelivery,  # noqa: E402
    Story,
    StoryAnalysis,
    Target,
)
from stories_monitor.db.session import session_scope  # noqa: E402
from stories_monitor.logging_setup import configure_logging  # noqa: E402
from stories_monitor.notify.slack import LeadMessage, get_notifier  # noqa: E402
from stories_monitor.priority import filter_photos  # noqa: E402
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


def _notify_lead(
    story_id: str, username: str, score: int, category: str, explanation: str, taken_at: dt.datetime
) -> bool:
    """Отправить лид в Slack ровно один раз.

    Строку в slack_deliveries занимаем ДО отправки: первичный ключ по story_id
    не даст отправить повторно, даже если процесс упадёт между отправкой и
    записью результата (§7.6).
    """
    with session_scope() as session:
        claimed = session.execute(
            pg_insert(SlackDelivery)
            .values(story_id=story_id, status="pending", attempts=0)
            .on_conflict_do_nothing(index_elements=["story_id"])
            .returning(SlackDelivery.story_id)
        ).scalar_one_or_none()
    if claimed is None:
        return False  # уже отправляли

    notifier = get_notifier()
    message = LeadMessage(
        story_id=story_id,
        username=username,
        instagram_url=f"https://instagram.com/{username}",
        service_category=category or None,
        final_score=score,
        ai_explanation=explanation or None,
        taken_at=taken_at,
    )

    try:
        ts = notifier.send(message)
        with session_scope() as session:
            session.execute(
                update(SlackDelivery)
                .where(SlackDelivery.story_id == story_id)
                .values(
                    status="sent", slack_ts=str(ts or ""), attempts=1,
                    sent_at=dt.datetime.now(dt.UTC),
                )
            )
        return True
    except Exception as exc:  # noqa: BLE001 - сбой Slack не должен ронять цикл
        with session_scope() as session:
            session.execute(
                update(SlackDelivery)
                .where(SlackDelivery.story_id == story_id)
                .values(status="failed", attempts=1, last_error=str(exc)[:500])
            )
        return False


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


def _explain_cheap(cheap: Any) -> str:
    """Причина вердикта из флагов дешёвой модели.

    Умную модель зовут только для оценок 5-6, так что на `accept` и `reject`
    объяснение взять неоткуда - а именно `accept` даёт самые сильные лиды.
    Флаги содержат всё нужное, надо лишь произнести это по-человечески.
    """
    reasons: list[str] = []
    if cheap.seeking_contractor:
        reasons.append("looking for a provider")
    if cheap.explicit_purchase_intent:
        reasons.append("states intent to hire or buy")
    if cheap.service_category:
        reasons.append(f"needs {cheap.service_category}")
    if cheap.geography:
        reasons.append(f"in {cheap.geography}")
    if cheap.email_visible:
        reasons.append("contact visible in the story")

    # Отрицательные признаки объясняют низкую оценку - для них причина нужна
    # не меньше: без неё непонятно, за что срезали.
    if cheap.is_offering_services:
        reasons.append("offering their own services, not requesting")
    if cheap.is_spam:
        reasons.append("spam or engagement bait")
    if cheap.asking_for_free:
        reasons.append("asking for a freebie")
    if cheap.complaint_only:
        reasons.append("complaint with no request")
    if not cheap.allowed_category:
        reasons.append("category outside the allowed list")

    if not reasons:
        return f"Scored {cheap.score}/10 by the first-pass model; no strong signal either way."
    return f"Scored {cheap.score}/10: " + ", ".join(reasons) + "."


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

    # Приоритизация (ТЗ §5): цели, стабильно не дающие лидов, пропускаем.
    # Замерено: из 15 аккаунтов с анализом лид дал один, 51 фото - впустую.
    before_priority = len(fresh)
    fresh, skipped_reasons, skip_details = filter_photos(fresh, names)
    deprioritised = before_priority - len(fresh)

    # Всё, что НЕ дошло до AI, объясняем поимённо: иначе в дашборде видно
    # только «обработано 3 фото», а куда делись остальные - непонятно.
    from stories_monitor.webaccounts import SOURCE

    if videos:
        by_owner: dict[str, list[str]] = {}
        for uid, items in reels.items():
            n = sum(1 for i in items if not i.is_photo)
            if n:
                by_owner.setdefault(SOURCE.get(uid, "-"), []).append(
                    f"{names.get(uid, uid)}×{n}"
                )
        for owner, tg in by_owner.items():
            activity.record(
                owner, "skipped", status="video",
                message=f"Skipped {sum(int(t.split('×')[1]) for t in tg)} videos — "
                        "photos only (spec 1), never reaches the AI",
                targets=tg, item_count=len(tg),
            )

    if seen_before:
        activity.record(
            "system", "skipped", status="duplicate",
            message=f"Skipped {seen_before} photos already analysed in an earlier "
                    "cycle — deduplicated on story_id, costs nothing",
            item_count=seen_before,
        )

    for d in skip_details:
        activity.record(
            SOURCE.get(d.user_id, "-"), "skipped", status="irrelevant",
            message=f"@{d.username}: {d.detail}",
            targets=[d.username], item_count=d.photos_skipped,
        )

    if len(fresh) > limit:
        activity.record(
            "system", "skipped", status="over_limit",
            message=f"Deferred {len(fresh) - limit} photos to the next cycle — "
                    f"per-cycle limit is {limit} (spend cap)",
            item_count=len(fresh) - limit,
        )

    print(
        f"найдено: {len(photos)} фото | {videos} видео пропущено (§1)\n"
        f"уже анализировали ранее: {seen_before} | новых к анализу: {len(fresh)}"
    )
    if deprioritised:
        print(f"пропущено по приоритету: {deprioritised} (цели без лидов)")
        for reason, count in sorted(skipped_reasons.items(), key=lambda x: -x[1])[:3]:
            print(f"   {count:>3} × {reason}")
    if not fresh:
        print("\nНовых фото нет - все уже проходили через AI. Платить второй раз не за что.")
        return 0

    print(f"классифицирую {min(limit, len(fresh))} самых свежих\n")
    print("=" * 78)

    now = dt.datetime.now(dt.UTC)
    results: list[tuple[str, int, str]] = []

    from stories_monitor.webaccounts import SOURCE

    for uid, item in fresh[:limit]:
        name = names.get(uid, str(uid))
        # Чья сессия принесла эту сторис - чтобы в дашборде фаза AI была
        # привязана к аккаунту, а не висела в воздухе.
        owner = SOURCE.get(uid, "-")
        ai_started = time.monotonic()
        url = item.best_image_url()
        if not url:
            # Instagram отдал заглушку вместо картинки (rsrc.php/null.jpg) или
            # вовсе не дал ссылку. Помечаем failed, иначе сторис останется в
            # состоянии `new` и будет всплывать в каждом цикле.
            print(f"    @{name:20} без пригодной ссылки на картинку — пропуск")
            activity.record(
                owner, "ai_scoring", status="skipped",
                message=f"@{name}: no usable image URL, skipped before AI",
                targets=[name],
            )
            _mark(item.story_id, "failed")
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

            activity.record(
                owner, "ai_scoring",
                message=f"Sending @{name}'s story to AI ({settings.cheap_model})",
                targets=[name], item_count=1,
            )
            cheap = client.call_cheap(path, text)

            # Категория - жёсткие ворота (ТЗ §7). Проверяем ДО маршрутизации:
            # реальный случай - "поеду в UNIQLO, пишите заказы" получил от
            # дешёвой модели 5 при allowed_category=false, ушёл в умную модель,
            # та подняла до 8, и ворота уже не применялись.
            if not cheap.allowed_category:
                print(
                    f"    @{name:20} категория вне охвата (§7) — оценка {cheap.score} → 0"
                )
                cheap.score = 0
                _save_analysis(item.story_id, text, cheap, 0, "Category outside the allowed list (spec 7)")
                _mark(item.story_id, "analyzed")
                activity.record(
                    owner, "ai_scored", status="ok",
                    message=(
                        f"@{name} scored {cheap.score}/10 → 0 "
                        f"(category outside allowed list)"
                    ),
                    targets=[name], item_count=1,
                    duration_ms=int((time.monotonic() - ai_started) * 1000),
                )
                results.append((name, 0, cheap.service_category or "-"))
                continue

            route = (
                "reject"
                if cheap.score < settings.smart_model_score_min
                else "smart"
                if cheap.score <= settings.smart_model_score_max
                else "accept"
            )
            final = cheap.score
            # На маршруте `accept` умная модель не вызывается, поэтому
            # объяснения не было НИ У ОДНОГО сильного лида: чем очевиднее
            # заявка, тем меньше шансов, что кто-то объяснит вердикт. Дешёвая
            # модель не возвращает текст, но возвращает флаги - из них и
            # собираем причину, не платя за второй вызов.
            explanation = "" if route == "smart" else _explain_cheap(cheap)
            if route == "smart":
                smart = client.call_smart(path, text, cheap)
                final = smart.final_score
                explanation = smart.explanation
                # Умная модель не может превратить "не заявку" в лид. Она уже
                # поднимала 5 до 8 на сторис, где автор ПРИНИМАЛ заказы -
                # дешёвая модель была права, а её вердикт проигнорировали.
                if not (cheap.seeking_contractor or cheap.explicit_purchase_intent):
                    if final >= settings.approval_score_min:
                        print(
                            f"    @{name:20} умная модель дала {final}, но заявки нет "
                            f"(seeking=False, intent=False) → 4"
                        )
                        final = 4

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
            activity.record(
                owner, "ai_scored",
                status="lead" if final >= settings.approval_score_min else "ok",
                message=(
                    f"@{name} scored {final}/10 via {route}"
                    + (f" — {cheap.service_category}" if cheap.service_category else "")
                ),
                targets=[name], item_count=1,
                duration_ms=int((time.monotonic() - ai_started) * 1000),
            )

            if final >= settings.approval_score_min:
                sent = _notify_lead(
                    item.story_id, name, final,
                    cheap.service_category or "", explanation,
                    dt.datetime.fromtimestamp(item.taken_at, tz=dt.UTC),
                )
                if sent:
                    _mark(item.story_id, "sent")
                activity.record(
                    owner, "lead",
                    status="ok" if sent else "error",
                    message=(
                        f"LEAD @{name} ({final}/10) "
                        + ("sent to Slack" if sent else "found but delivery failed")
                    ),
                    targets=[name], item_count=1,
                )
        except Exception as exc:  # noqa: BLE001 - одна плохая сторис не рушит прогон
            print(f"    @{name:20} ошибка: {type(exc).__name__}: {str(exc)[:60]}")
            # Сбой БИЛЛИНГА или сети - не свойство сторис, а состояние сервиса:
            # через час всё то же фото разберётся нормально. `failed` - конечное
            # состояние, его никто не перепроверяет, поэтому пометить им сторис
            # значит выбросить её навсегда из-за пустого счёта. Замерено: 402
            # Payment Required от OpenRouter похоронил две живые сторис, и те же
            # 402/таймауты объясняют 39 записей `failed` без строки анализа.
            text_exc = str(exc)
            transient = (
                "402" in text_exc
                or "429" in text_exc
                or "Insufficient" in text_exc
                or "credits" in text_exc.lower()
                or isinstance(exc, (httpx.TimeoutException, httpx.TransportError))
            )
            activity.record(
                owner, "ai_scoring",
                status="deferred" if transient else "error",
                message=f"@{name}: {type(exc).__name__}: {str(exc)[:120]}",
                targets=[name],
            )
            # Оставляем `new`: следующий цикл возьмёт её снова, пока сторис жива.
            if not transient:
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
