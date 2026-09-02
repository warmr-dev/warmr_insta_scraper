"""Как часто опрашивать каждую цель.

До сих пор все подписки опрашивались одинаково часто. Данные говорят, что это
неправильно в обе стороны: из 123 подписок 103 не выложили НИ ОДНОЙ сторис, а
29 целей дали 835 сторис, из которых ни одна не набрала больше 1 балла. То есть
основная часть запросов уходит туда, где ничего не найдётся, и ровно эти
запросы стоят нам лимитов - тех самых, из-за которых мы не успеваем к
действительно интересным аккаунтам.

Логика простая: чем ценнее цель, тем чаще её проверяем.

- Цель, давшая лид, проверяется каждый цикл. Пропустить её сторис дороже всего:
  сторис живёт 24 часа, и «первыми увидеть» - это про них.
- Цель с оценками 2-6 пишет о заказах, просто пока не дотянула. Тоже часто.
- Цель, которую анализировали много раз и всегда получали 0-1, - не наша
  аудитория. Проверяем изредка: люди меняются, но платить за них каждый цикл
  незачем.
- Цель, которая вообще ничего не публикует, - проверяем совсем редко.

Ни одна цель не выключается насовсем: у каждой есть свой интервал, а не запрет.
Иначе однажды замолчавший аккаунт исчез бы из поля зрения навсегда.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass

from sqlalchemy import text

from .db.session import session_scope
from .logging_setup import get_logger

log = get_logger(__name__)

# Каждый уровень - "проверять не чаще, чем раз в N секунд". 0 - каждый цикл.
TIER_HOT = 0                                                    # давала лиды
TIER_WARM = int(os.environ.get("CADENCE_WARM_SEC", "300"))      # оценки 2-6
TIER_COLD = int(os.environ.get("CADENCE_COLD_SEC", str(30 * 60)))    # только 0-1
TIER_SILENT = int(os.environ.get("CADENCE_SILENT_SEC", str(2 * 3600)))  # ни одной сторис

# Ниже этого порога цель считается «пишет о заказах» (ТЗ §5 использует ту же
# границу для приоритизации AI - держим их согласованными).
PROMISING_SCORE = int(os.environ.get("CADENCE_PROMISING_SCORE", "2"))
LEAD_SCORE = int(os.environ.get("APPROVAL_SCORE_MIN", "7"))

# Сколько раз нужно проанализировать цель без результата, прежде чем понижать
# её. Одна нулевая сторис ничего не доказывает.
MIN_SAMPLES = int(os.environ.get("CADENCE_MIN_SAMPLES", "5"))

CADENCE_ENABLED = os.environ.get("CADENCE_ENABLED", "true").lower() != "false"


@dataclass(slots=True)
class TargetCadence:
    """Решение по одной цели."""

    user_id: int
    tier: str
    interval_sec: int
    last_seen: dt.datetime | None

    def is_due(self, now: dt.datetime) -> bool:
        if self.interval_sec <= 0 or self.last_seen is None:
            return True
        return (now - self.last_seen).total_seconds() >= self.interval_sec


def load_cadences() -> dict[int, TargetCadence]:
    """Уровень каждой цели - одним запросом.

    `last_seen` - когда мы в последний раз ВИДЕЛИ у неё сторис, а не когда
    опрашивали: опрос без результата не должен отодвигать следующую проверку.
    """
    sql = """
        SELECT st.target_user_id AS uid,
               count(a.story_id)                                   AS analysed,
               coalesce(max(a.final_score), 0)                     AS best,
               count(*) FILTER (WHERE a.final_score >= :lead)      AS leads,
               max(st.taken_at)                                    AS last_story
          FROM stories st
          LEFT JOIN story_analysis a ON a.story_id = st.story_id
         GROUP BY st.target_user_id
    """
    out: dict[int, TargetCadence] = {}
    try:
        with session_scope() as session:
            for row in session.execute(text(sql), {"lead": LEAD_SCORE}).mappings():
                out[int(row["uid"])] = _classify(
                    int(row["uid"]),
                    analysed=int(row["analysed"] or 0),
                    best=int(row["best"] or 0),
                    leads=int(row["leads"] or 0),
                    last_story=row["last_story"],
                )
    except Exception as exc:  # noqa: BLE001 - без градаций лучше опросить всех
        log.warning("cadence_load_failed", error=str(exc)[:120])
    return out


def _classify(
    user_id: int,
    *,
    analysed: int,
    best: int,
    leads: int,
    last_story: dt.datetime | None,
) -> TargetCadence:
    if leads > 0:
        return TargetCadence(user_id, "hot", TIER_HOT, last_story)
    if best >= PROMISING_SCORE:
        return TargetCadence(user_id, "warm", TIER_WARM, last_story)
    if analysed >= MIN_SAMPLES:
        # Много раз смотрели, всегда 0-1. Не наша аудитория.
        return TargetCadence(user_id, "cold", TIER_COLD, last_story)
    # Историей не богата: либо совсем новая, либо ничего не публикует.
    if last_story is None:
        return TargetCadence(user_id, "silent", TIER_SILENT, None)
    return TargetCadence(user_id, "warm", TIER_WARM, last_story)


def select_due(
    following: list[tuple[int, str]],
    cadences: dict[int, TargetCadence],
    now: dt.datetime | None = None,
) -> tuple[list[tuple[int, str]], dict[str, int]]:
    """Кого опрашивать в этом цикле.

    Возвращает (подписки к опросу, счётчик по уровням).

    Подписка, о которой нет вообще никакой истории, - самый частый случай: 103
    из 123. Опрашивать её каждый цикл наравне с целью, дающей лиды, значит
    тратить лимиты на тишину. Но и выключить её нельзя: новая подписка обязана
    получить шанс, иначе приоритизация становится самосбывающейся.

    Компромисс: такие цели опрашиваются по расписанию TIER_SILENT, но со
    сдвигом по user_id, чтобы они не приходились все на один цикл. Пик
    запросов - это ровно то, чего мы избегаем.
    """
    if not CADENCE_ENABLED:
        return following, {}

    moment = now or dt.datetime.now(dt.UTC)
    due: list[tuple[int, str]] = []
    skipped: dict[str, int] = {}

    for uid, name in following:
        cadence = cadences.get(uid)
        if cadence is not None:
            if cadence.is_due(moment):
                due.append((uid, name))
            else:
                skipped[cadence.tier] = skipped.get(cadence.tier, 0) + 1
            continue

        # Истории нет. Раскладываем такие цели по окну TIER_SILENT: каждая
        # попадает в свой слот, вычисленный из её id, - равномерно и без всплеска.
        if TIER_SILENT <= 0:
            due.append((uid, name))
            continue
        slot = uid % max(1, TIER_SILENT)
        elapsed = int(moment.timestamp()) % TIER_SILENT
        # Окно шириной в один цикл (60с) вокруг слота цели.
        if abs(elapsed - slot) < 60 or TIER_SILENT - abs(elapsed - slot) < 60:
            due.append((uid, name))
        else:
            skipped["unseen"] = skipped.get("unseen", 0) + 1

    return due, skipped
