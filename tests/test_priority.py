"""Приоритизация целей (ТЗ §5: неактивных проверять реже).

Замерено на первых живых данных: из 15 аккаунтов с проанализированными фото
лид дал ровно ОДИН, а 51 фото ушло на аккаунты без единого сигнала.

Экономия должна быть осторожной: пропустить настоящего клиента дороже, чем
лишний раз проверить пустой аккаунт.
"""

from __future__ import annotations

import datetime as dt

from stories_monitor.priority import TargetStats, should_analyse


def test_new_target_is_always_analysed():
    """Без истории решать не на чем - иначе приоритет самоисполняется."""
    ok, why = should_analyse(None)
    assert ok and "new target" in why

    ok, _ = should_analyse(TargetStats(user_id=1, analysed=0))
    assert ok


def test_target_that_produced_a_lead_is_always_analysed():
    """Кто дал лид однажды, даст снова - это сильнейший сигнал."""
    stats = TargetStats(user_id=1, analysed=50, leads=1, best_score=8, avg_score=0.2)
    ok, why = should_analyse(stats)
    assert ok and "produced leads" in why


def test_promising_target_survives_even_without_a_lead():
    """Оценка 4-6 значит, что человек пишет о заказах. Не отсекаем."""
    stats = TargetStats(user_id=1, analysed=20, leads=0, best_score=5, avg_score=1.0)
    ok, why = should_analyse(stats)
    assert ok and "promising" in why


def test_target_with_enough_zeroes_is_skipped():
    """Двадцать нулей подряд не заслуживают двадцать первого вызова модели."""
    stats = TargetStats(
        user_id=1, analysed=20, leads=0, best_score=0, avg_score=0.0,
        last_analysed_at=dt.datetime.now(dt.UTC),
    )
    ok, _ = should_analyse(stats)
    assert not ok


def test_a_skipped_target_is_never_blocked_forever():
    """Люди меняют поведение, а выборка мала - шанс должен оставаться."""
    stale = dt.datetime.now(dt.UTC) - dt.timedelta(days=3)
    stats = TargetStats(
        user_id=1, analysed=20, leads=0, best_score=0, avg_score=0.0,
        last_analysed_at=stale,
    )
    ok, why = should_analyse(stats)
    assert ok, "цель заблокирована навсегда - дороги назад нет"
    assert "recheck" in why


def test_small_sample_of_zeroes_is_not_enough_to_skip():
    """Три нуля - не доказательство. Отсекаем только по накопленной истории."""
    stats = TargetStats(
        user_id=1, analysed=3, leads=0, best_score=0, avg_score=0.0,
        last_analysed_at=dt.datetime.now(dt.UTC),
    )
    ok, _ = should_analyse(stats)
    assert ok
