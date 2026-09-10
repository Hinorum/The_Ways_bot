"""Слой 6 — контент-данные: офлайн-карты выравнены под единый конвейер.

Трата ресурсов и урон отключены: food_cost/water_cost/health_risk всегда 0.
Офлайн-тропы лора и карты главы выравниваются по архетипу и названию
(детерминированно на день) по trust_change/emotional_consequence/npc_reactions,
уважая явные значения модели.
"""

from __future__ import annotations

import json

from app.card_payload import _assemble_cards, _card_payload
from app.lore import _CARE_PATHS, _CUNNING_PATHS, _RISK_PATHS, card_rich_payload

POOL = [
    (*t, "risk") for t in _RISK_PATHS
] + [
    (*t, "care") for t in _CARE_PATHS
] + [
    (*t, "cunning") for t in _CUNNING_PATHS
]


# ── Деривация card_rich_payload ────────────────────────────────────────────


def test_rich_profile_is_deterministic_per_day() -> None:
    a = card_rich_payload("Прыжок в огонь", "risk", 7, salt="s")
    b = card_rich_payload("Прыжок в огонь", "risk", 7, salt="s")
    assert a == b


def test_rich_profile_breathes_across_days() -> None:
    seen = {
        (r["trust_change"], r["emotional_consequence"])
        for r in (card_rich_payload("Прыжок в огонь", "risk", d) for d in range(1, 21))
    }
    assert len(seen) > 1  # один и тот же троп в разные дни звучит по-разному


def test_rich_profile_costs_are_zero_and_trust_matches_archetype() -> None:
    for day in range(1, 9):
        risk = card_rich_payload("Риск", "risk", day)
        care = card_rich_payload("Дом", "care", day)
        cunning = card_rich_payload("Хитрость", "cunning", day)
        for rich in (risk, care, cunning):
            assert rich["food_cost"] == 0
            assert rich["water_cost"] == 0
            assert rich["health_risk"] == 0
            assert rich["emotional_consequence"]
            assert 1 <= len(rich["npc_reactions"]) <= 2
            for entry in rich["npc_reactions"]:
                assert entry["name"] and entry["reaction"]
        assert care["trust_change"] >= 1
        assert cunning["trust_change"] <= 0


def test_rich_profile_falls_back_to_care_on_unknown_tag() -> None:
    rich = card_rich_payload("Что-то", "not-a-tag", 1)
    assert rich["food_cost"] == 0
    assert rich["trust_change"] >= 1


def test_every_offline_trope_enriches_validly() -> None:
    for day in range(1, 8):
        for title, _desc, _conseq, tag in POOL:
            rich = card_rich_payload(title, tag, day)
            assert rich["food_cost"] == 0
            assert rich["health_risk"] == 0
            if tag != "care":
                pass  # доверие risk/cunning может быть и нулевым — это ок


# ── Payload-конвейер ───────────────────────────────────────────────────────


def test_offline_cards_carried_through_assemble_carry_rich_fields() -> None:
    cards = _assemble_cards({}, 5)
    assert len(cards) == 3
    for card in cards:
        tag = card["tag"]
        assert card["food_cost"] == 0
        assert card["water_cost"] == 0
        assert card["health_risk"] == 0
        assert card["emotional_consequence"]
        assert json.loads(card["npc_reactions_json"])
        if tag == "care":
            assert card["trust_change"] >= 1


def test_assemble_keeps_explicit_trust_and_drops_cost_fields() -> None:
    chapter = {
        "cards": [
            {
                "title": "Ждать",
                "description": "Прижаться к теплу.",
                "consequence": "Утро решит само.",
                "tag": "care",
                "trust_change": 3,
            },
            {
                "title": "Идти",
                "description": "Прямо по тротуару.",
                "consequence": "Кто-то заметит.",
                "tag": "risk",
            },
        ]
    }
    cards = _assemble_cards(chapter, 9)
    assert len(cards) == 3
    waiting = cards[0]
    assert waiting["food_cost"] == 0  # ресурсы/урон всегда 0
    assert waiting["health_risk"] == 0
    assert waiting["trust_change"] == 3  # явное значение модели уважаем
    for card in cards:
        assert card["food_cost"] == 0
        assert card["health_risk"] == 0


def test_card_payload_costs_zero_and_rich_fields_derived() -> None:
    payload = _card_payload(
        {"title": "Сломать цепь", "description": "д", "consequence": "c", "tag": "risk"},
        0,
        12,
    )
    assert payload["food_cost"] == 0
    assert payload["water_cost"] == 0
    assert payload["health_risk"] == 0
    assert payload["npc_reactions_json"] != "[]"