"""Правила промпта, за которые заплачено ложными лидами.

Реальный случай: `astana.hub` репостил анонс AI-конференции, а классификатор
трижды пометил это лидом с оценкой 8 - увидел "software development" + "Astana"
и решил, что кто-то ищет подрядчика.

Тесты проверяют текст промпта, а не модель: сетевого вызова нет, но правило
нельзя удалить незаметно.
"""

from __future__ import annotations

import pathlib

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


def test_cheap_prompt_lists_the_allowed_categories():
    """ТЗ §7: клиент покупает B2B-услуги. Ложные лиды были florist/hookah/retail."""
    text = CHEAP_SYSTEM_PROMPT
    for term in ("Meta Ads", "SEO", "HubSpot", "co-packing", "immigration", "CPA"):
        assert term in text, f"нет приоритетной категории ТЗ §7: {term}"


def test_cheap_prompt_names_out_of_scope_categories():
    """Верная заявка в чужой категории - всё равно не наш лид."""
    text = CHEAP_SYSTEM_PROMPT.lower()
    for term in ("plumbing", "florist", "restaurant", "beauty"):
        assert term in text, f"не назван как вне охвата: {term}"


def test_cheap_prompt_covers_spec_auto_rejects():
    """ТЗ §8: barter, backlinks, partnership-питчи, поиск работы."""
    text = CHEAP_SYSTEM_PROMPT.lower()
    for term in ("barter", "backlink", "guest post", "partnership", "resume"):
        assert term in text, f"нет автоотклонения ТЗ §8: {term}"


def test_smart_prompt_applies_the_category_gate_first():
    """Умная модель тоже обязана отсекать чужие категории."""
    assert "CATEGORY GATE" in SMART_SYSTEM_PROMPT


def test_placeholder_image_url_is_rejected():
    """Instagram отдаёт rsrc.php/null.jpg вместо картинки - скачивание даёт 400.

    Реальный случай: @astana_it_university, HTTPStatusError 400 в логах.
    """
    from stories_monitor.transport.base import StoryItem

    def item(versions):
        return StoryItem(
            story_id="x", user_id=1, taken_at=1, media_type=1, image_versions=versions
        )

    placeholder = "https://static.cdninstagram.com/rsrc.php/null.jpg"
    assert item([{"url": placeholder, "width": 1080, "height": 1920}]).best_image_url() is None
    assert item([{"url": "", "width": 1, "height": 1}]).best_image_url() is None
    assert item([{"width": 1, "height": 1}]).best_image_url() is None
    assert item([{"url": "not-a-url", "width": 1, "height": 1}]).best_image_url() is None

    # Заглушка не должна вытеснять годный кандидат, даже будучи "больше".
    real = "https://scontent.cdninstagram.com/real.jpg"
    chosen = item(
        [
            {"url": placeholder, "width": 9999, "height": 9999},
            {"url": real, "width": 100, "height": 100},
        ]
    ).best_image_url()
    assert chosen == real


def test_smart_model_cannot_invent_a_lead(monkeypatch):
    """Умная модель не превращает "не заявку" в лид.

    Реальный случай: сторис "Скоро поеду в UNIQLO, пишите заказы" - дешёвая
    модель верно дала score=5, seeking_contractor=false, allowed_category=false.
    Умная подняла до 8, и лид ушёл в дашборд. Автор ПРИНИМАЕТ заказы, то есть
    продаёт, а одежда вне списка категорий ТЗ §7.
    """
    from stories_monitor.ai.schemas import CheapResult, SmartResult
    from stories_monitor.workers.analyzer import Analyzer

    cheap = CheapResult(
        score=5,
        explicit_purchase_intent=False,
        seeking_contractor=False,
        allowed_category=True,  # категорию проверяет отдельный тест
        is_spam=False,
        is_offering_services=False,
        asking_for_free=False,
        complaint_only=False,
        service_category=None,
        geography=None,
        email_visible=None,
    )
    smart = SmartResult(
        confirmed=True,
        final_score=8,
        service_category=None,
        intent_type=None,
        explanation="An offer to purchase items for others.",
    )

    class FakeAI:
        def call_cheap(self, *_a, **_k):
            return cheap

        def call_smart(self, *_a, **_k):
            return smart

        def read_text(self, *_a, **_k):
            return ""

    class NullOCR:
        name = "null"

        def extract_text(self, _p):
            return ""

    analyzer = Analyzer(ai_client=FakeAI(), ocr_engine=NullOCR(), queue=object())

    written: dict[str, object] = {}
    monkeypatch.setattr(analyzer, "_write_analysis", lambda **kw: written.update(kw))
    monkeypatch.setattr(analyzer, "_set_state", lambda *_a: None)
    monkeypatch.setattr(analyzer, "bizcheck_queue", type("Q", (), {"push": lambda *_: None})())

    result = analyzer._analyze("story-uniqlo", "/dev/null")

    assert result["final_score"] < 7, (
        "умная модель подняла оценку до лида, хотя нет ни seeking_contractor, "
        "ни explicit_purchase_intent"
    )
    assert written.get("final_score", 99) < 7


def test_category_gate_runs_before_the_smart_model():
    """Ворота по категории должны стоять ДО умной модели.

    Иначе сторис с оценкой 5 и allowed_category=false уходит в умную модель,
    та поднимает до 8, и ворота уже не применяются - ровно так UNIQLO-сторис
    и стала лидом.
    """
    source = (
        pathlib.Path(__file__).resolve().parents[1]
        / "scripts/web_classify.py"
    ).read_text()

    gate = source.index("not cheap.allowed_category")
    smart_call = source.index("client.call_smart")
    assert gate < smart_call, "проверка категории стоит после вызова умной модели"
