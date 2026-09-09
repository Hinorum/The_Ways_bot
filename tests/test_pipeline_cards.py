"""Слой 4: единый конвейер — карты рождаются в той же генерации, что и глава.

Промпт по умолчанию не содержит блока карт (обратная совместимость строк),
с флагом with_choices — содержит; _parse_chapter нормализует карты под схему
Card; конвейер дня достраивает нехватку офлайн-пулом.
"""

import json

from app.story import _build_story_prompt, _normalize_cards, _parse_chapter


def test_prompt_default_keeps_no_cards_block() -> None:
    from app.models import WinRule

    prompt = _build_story_prompt(7, [], WinRule.MINORITY)
    assert '"cards"' not in prompt
    assert "РОВНО 3 карты" not in prompt


def test_prompt_with_choices_adds_cards_schema() -> None:
    from app.models import WinRule

    prompt = _build_story_prompt(
        7, ["Костёр стаи: появился общий костёр"], WinRule.MAJORITY, with_choices=True
    )
    assert '"cards"' in prompt
    assert "РОВНО 3 карты" in prompt
    assert "risk | care | cunning" in prompt
    assert "npc_reactions" in prompt


def test_prompt_with_choices_still_builds_chapter() -> None:
    prompt = _build_story_prompt(1, [], None, with_choices=True)
    assert "Ответь только JSON" in prompt
    assert "Формат:" in prompt


def test_normalize_cards_whitelists_tags_and_coerces_costs() -> None:
    raw = [
        {
            "title": "Ворваться",
            "description": "Короткий путь через гараж.",
            "consequence": "Шум разбудит зиму.",
            "tag": "risk",
            "food_cost": "2",
            "water_cost": 1,
            "health_risk": "4",
            "trust_change": "-1",
            "npc_reactions": [{"name": "Лайнер", "reaction": "Фыркает."}],
        },
        {"title": "", "description": "Пустышка", "consequence": "c"},
        {
            "description": "Без имени не карта",
            "consequence": "c",
            "tag": "risk",
        },
        {
            "title": "Крадучись",
            "description": "Обойти по крышам.",
            "consequence": "Долго, но тихо.",
            "tag": "cunning",
        },
        {
            "title": 12,
            "description": 34,
            "consequence": "x",
            "tag": "invalid",
            "npc_reactions": "nope",
        },
    ]
    normalized = _normalize_cards(raw)
    assert len(normalized) == 3  # две пустые записи отброшены
    assert {card["title"] for card in normalized} == {"Ворваться", "Крадучись", "12"}
    by_title = {card["title"]: card for card in normalized}
    first = by_title["Ворваться"]
    assert first["tag"] == "risk"
    assert first["food_cost"] == 2
    assert first["water_cost"] == 1
    assert first["health_risk"] == 4
    assert first["trust_change"] == -1
    assert first["npc_reactions"] == [{"name": "Лайнер", "reaction": "Фыркает."}]
    assert by_title["Крадучись"]["tag"] == "cunning"
    assert by_title["12"]["tag"] == "care"  # не из белого списка
    assert by_title["12"]["npc_reactions"] == []


def test_parse_chapter_normalizes_cards_inline() -> None:
    chapter_json = json.dumps(
        {
            "title": "День 9. Гараж",
            "place": "Гараж",
            "text": "а" * 300,
            "lore_summary": "л",
            "cards": [
                {
                    "title": "Вскрыть гараж",
                    "description": "Ржавый замок поддаётся.",
                    "consequence": "Внутри шум и запах зимней еды.",
                    "tag": "risk",
                    "food_cost": 2,
                    "location": "Гараж",
                    "npc_reactions": [{"name": "Лайнер", "reaction": "Кружит у ворот."}],
                },
                {
                    "title": "Обойти",
                    "description": "Тропа вдоль стены.",
                    "consequence": "Дольше, но тише.",
                    "tag": "care",
                    "characters_involved": ["Лайнер"],
                },
                {
                    "title": "Ждать",
                    "description": "Прижаться к теплу.",
                    "consequence": "Утро решит само.",
                    "tag": "cunning",
                    "health_risk": 0,
                },
            ],
        },
        ensure_ascii=False,
    )
    data = _parse_chapter({"choices": [{"message": {"content": chapter_json}}]}, 9)
    assert data is not None
    cards = data["cards"]
    assert len(cards) == 3
    assert {card["tag"] for card in cards} == {"risk", "care", "cunning"}
    rich = next(card for card in cards if card["tag"] == "risk")
    assert rich["food_cost"] == 2
    assert rich["npc_reactions"][0]["name"] == "Лайнер"
    assert rich["location"] == "Гараж"


def test_assemble_cards_uses_chapter_then_fills_offline() -> None:
    from app.rounds import _assemble_cards

    chapter = {
        "cards": [
            {
                "title": "Дельта",
                "description": "Описание первой.",
                "consequence": "Итог первой.",
                "tag": "risk",
                "food_cost": 1,
                "npc_reactions": [{"name": "Лайнер", "reaction": "Следит."}],
            },
            {
                "title": "Гамма",
                "description": "Описание второй.",
                "consequence": "Итог второй.",
                "tag": "care",
            },
        ]
    }
    cards = _assemble_cards(chapter, 42)
    assert len(cards) == 3
    assert [card["position"] for card in cards] == [0, 1, 2]
    assert cards[0]["title"] == "Дельта"
    assert cards[0]["food_cost"] == 1
    assert json.loads(cards[0]["npc_reactions_json"])[0]["name"] == "Лайнер"
    third = cards[2]
    assert third["title"] and third["description"]
    assert third["tag"] in {"risk", "care", "cunning"}


def test_assemble_cards_empty_chapter_uses_offline_pool() -> None:
    from app.rounds import _assemble_cards

    cards = _assemble_cards({}, 43)
    assert len(cards) == 3
    for card in cards:
        assert card["position"] == cards.index(card)
        assert card["title"] and card["description"]
        assert card["tag"] in {"risk", "care", "cunning"}
        assert card["npc_reactions_json"] == "[]"