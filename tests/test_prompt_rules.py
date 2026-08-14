"""Правила промпта, за которые заплачено ложными лидами.

Реальный случай: `astana.hub` репостил анонс AI-конференции, а классификатор
трижды пометил это лидом с оценкой 8 - увидел "software development" + "Astana"
и решил, что кто-то ищет подрядчика.

Тесты проверяют текст промпта, а не модель: сетевого вызова нет, но правило
нельзя удалить незаметно.
"""

from __future__ import annotations

from stories_monitor.ai.prompts import CHEAP_SYSTEM_PROMPT, SMART_SYSTEM_PROMPT


def test_cheap_prompt_rejects_events_and_reposts():
    """Анонс конференции - не заявка на разработку (реальный ложный лид)."""
    text = CHEAP_SYSTEM_PROMPT.lower()
    for term in ("repost", "conference", "meetup", "vacanc", "announcement"):
        assert term in text, f"промпт не защищён от: {term}"


def test_cheap_prompt_ties_score_to_flags():
    """Высокая оценка при seeking_contractor=false - источник ложных лидов."""
    text = CHEAP_SYSTEM_PROMPT
    assert "AT MOST 2" in text, "нет потолка оценки для не-лидов"
    assert "seeking_contractor is false" in text
    assert "explicit_purchase_intent is false" in text


def test_cheap_prompt_defines_direction_of_transaction():
    """«Пишите заказы» - продавец, а не лид. Модель путала это."""
    text = CHEAP_SYSTEM_PROMPT
    assert "пишите заказы" in text.lower(), "нет русских формулировок продавца"
    assert "who sends the next message" in text.lower(), "нет теста направления"


def test_smart_prompt_cannot_be_swayed_by_first_pass():
    """Умная модель поднимала 6 до 9 на сторис, где дешёвая была права."""
    text = SMART_SYSTEM_PROMPT
    assert "Do not be swayed" in text
    assert "DIRECTION OF THE TRANSACTION" in text


def test_smart_prompt_prefers_refusing_when_torn():
    """Ложный лид дороже пропущенного: тратит время вендора и его доверие."""
    assert "refuse" in SMART_SYSTEM_PROMPT.lower()


def test_both_prompts_still_demand_json_only():
    """Ужесточение не должно было сломать контракт вывода (§7.4)."""
    for prompt in (CHEAP_SYSTEM_PROMPT, SMART_SYSTEM_PROMPT):
        assert "ONE JSON object" in prompt
        assert "no markdown code fences" in prompt.lower()
