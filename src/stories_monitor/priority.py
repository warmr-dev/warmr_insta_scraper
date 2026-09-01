"""Приоритизация целей: кого анализировать, а кого пропускать.

ТЗ §5: «проверять неактивные аккаунты реже, а ранее активные чаще».

Экономить нужно на AI-анализе, а не на чтении трея: трей отдаётся одним
запросом сразу по всем подпискам и стоит копейки, тогда как каждое фото - это
вызов модели. Поэтому фильтр стоит между «нашли сторис» и «отправили в AI».

Замерено на первых данных: из 15 аккаунтов с проанализированными фото лид дал
ровно ОДИН. 51 фото ушло на аккаунты, которые не дали ничего.

Оценка аккаунта складывается из трёх сигналов:

1. Были ли лиды раньше - самый сильный признак. Аккаунт, однажды давший лид,
   даёт их снова.
2. Оценки прошлых сторис - даже 4-6 означают, что человек пишет о заказах.
   Стабильные нули означают, что это не наша аудитория.
3. Сколько уже потрачено впустую - аккаунт с 20 нулями подряд не заслуживает
   двадцать первого вызова.

Ни один аккаунт не блокируется навсегда: у каждого есть шанс раз в
`recheck_after_hours`, потому что люди меняют поведение, а выборка мала.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select

from .config import get_settings
from .db.models import Story, StoryAnalysis
from .db.session import session_scope
from .logging_setup import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class TargetStats:
    """История одной цели, на которой строится решение."""

    user_id: int
    analysed: int = 0
    leads: int = 0
    best_score: int = 0
    avg_score: float = 0.0
    last_analysed_at: dt.datetime | None = None

    @property
    def is_proven(self) -> bool:
        """Уже давал лид - анализируем всегда."""
        return self.leads > 0

    @property
    def is_promising(self) -> bool:
        """Пишет о заказах, хоть и не дотягивал до лида."""
        return self.best_score >= 4

    @property
    def is_exhausted(self) -> bool:
        """Достаточно нулей подряд, чтобы перестать платить за него."""
        settings = get_settings()
        return (
            self.analysed >= settings.priority_min_samples
            and self.best_score == 0
            and self.avg_score < 0.5
        )

    def hours_since_last(self, now: dt.datetime | None = None) -> float:
        if self.last_analysed_at is None:
            return float("inf")
        reference = now or dt.datetime.now(dt.UTC)
        return (reference - self.last_analysed_at).total_seconds() / 3600


def load_stats(user_ids: list[int]) -> dict[int, TargetStats]:
    """История по целям - одним запросом, а не по одному на аккаунт."""
    if not user_ids:
        return {}

    with session_scope() as session:
        rows = session.execute(
            select(
                Story.target_user_id,
                func.count(StoryAnalysis.story_id),
                func.count(StoryAnalysis.story_id).filter(
                    StoryAnalysis.final_score >= get_settings().approval_score_min
                ),
                func.coalesce(func.max(StoryAnalysis.final_score), 0),
                func.coalesce(func.avg(StoryAnalysis.final_score), 0.0),
                func.max(StoryAnalysis.analyzed_at),
            )
            .join(StoryAnalysis, StoryAnalysis.story_id == Story.story_id)
            .where(Story.target_user_id.in_(user_ids))
            .group_by(Story.target_user_id)
        ).all()

    return {
        int(uid): TargetStats(
            user_id=int(uid),
            analysed=int(analysed or 0),
            leads=int(leads or 0),
            best_score=int(best or 0),
            avg_score=float(avg or 0.0),
            last_analysed_at=last,
        )
        for uid, analysed, leads, best, avg, last in rows
    }


def should_analyse(stats: TargetStats | None, now: dt.datetime | None = None) -> tuple[bool, str]:
    """Анализировать фото этой цели? Возвращает (да/нет, причина).

    Причина попадает в лог, чтобы решение можно было объяснить, а не гадать.
    """
    settings = get_settings()

    # Нет истории - обязательно анализируем. Иначе новая цель никогда не
    # получит шанс и приоритизация станет самоисполняющейся.
    if stats is None or stats.analysed == 0:
        return True, "новая цель"

    if stats.is_proven:
        return True, f"давал лиды ({stats.leads})"

    if stats.is_promising:
        return True, f"перспективный (лучшая оценка {stats.best_score})"

    if stats.is_exhausted:
        # Периодически всё равно проверяем: люди меняют поведение, а выборка
        # маленькая. Полная блокировка навсегда закрыла бы дорогу назад.
        hours = stats.hours_since_last(now)
        if hours >= settings.priority_recheck_hours:
            return True, f"плановая перепроверка через {hours:.0f}ч"
        return False, f"{stats.analysed} фото без сигнала, перепроверка через {settings.priority_recheck_hours - hours:.0f}ч"

    return True, "истории недостаточно"


@dataclass(slots=True)
class SkipDecision:
    """Почему конкретную цель пропустили - для дашборда и логов.

    Раньше причина сворачивалась в счётчик и терялась: было видно «пропущено
    12», но не КОГО и почему. Именно это и нужно объяснить в интерфейсе.
    """

    user_id: int
    username: str
    reason: str
    photos_skipped: int
    analysed: int
    leads: int
    best_score: int
    avg_score: float
    hours_since_last: float

    @property
    def detail(self) -> str:
        """Человекочитаемое объяснение - идёт прямо в дашборд."""
        return (
            f"{self.analysed} photos analysed, best score {self.best_score}, "
            f"avg {self.avg_score:.1f}, no leads — {self.reason}"
        )


def filter_photos(
    photos: list[tuple[int, object]], names: dict[int, str] | None = None
) -> tuple[list[tuple[int, object]], dict[str, int], list[SkipDecision]]:
    """Отсеять фото целей, которые стабильно не дают лидов.

    Возвращает (что анализировать, статистика пропусков, решения по целям).
    Третий элемент - подробности по каждой пропущенной цели: без него в
    интерфейсе видно только «пропущено N», а не за что именно.
    """
    if not photos:
        return [], {}, []

    if not get_settings().priority_enabled:
        return photos, {}, []

    stats = load_stats(list({uid for uid, _ in photos}))
    now = dt.datetime.now(dt.UTC)
    names = names or {}

    keep: list[tuple[int, object]] = []
    skipped: dict[str, int] = {}
    decisions: dict[int, str] = {}
    per_target: dict[int, int] = {}

    for user_id, item in photos:
        ok, reason = should_analyse(stats.get(user_id), now)
        if ok:
            keep.append((user_id, item))
        else:
            skipped[reason.split(",")[0]] = skipped.get(reason.split(",")[0], 0) + 1
            decisions[user_id] = reason
            per_target[user_id] = per_target.get(user_id, 0) + 1

    detailed = []
    for user_id, reason in decisions.items():
        st = stats.get(user_id)
        detailed.append(
            SkipDecision(
                user_id=user_id,
                username=names.get(user_id, str(user_id)),
                reason=reason,
                photos_skipped=per_target.get(user_id, 0),
                analysed=st.analysed if st else 0,
                leads=st.leads if st else 0,
                best_score=st.best_score if st else 0,
                avg_score=st.avg_score if st else 0.0,
                hours_since_last=st.hours_since_last(now) if st else float("inf"),
            )
        )
    # Худшие сверху: кто дороже всего обошёлся впустую.
    detailed.sort(key=lambda d: (-d.analysed, -d.photos_skipped))

    if skipped:
        log.info(
            "priority_filtered",
            kept=len(keep),
            skipped=len(photos) - len(keep),
            accounts_skipped=len(decisions),
        )
    return keep, skipped, detailed
