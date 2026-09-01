"""Бой как текст — нарративные сражения с трекингом потерь.

Бои не имеют механической системы (d20, HP-бары).
Всё решается ИИ в тексте главы. Механика только трекает
потери здоровья и генерирует триггеры для промпта.
"""

from __future__ import annotations

from dataclasses import dataclass
import random

from app.pack_state import PackNeeds


@dataclass
class CombatResult:
    """Результат боя."""
    outcome: str  # "victory", "defeat", "retreat", "draw"
    health_loss: int = 0
    hunger_change: int = 0
    narrative: str = ""  # Краткое описание для промпта


# Возможные исходы боёв по типу карты
COMBAT_OUTCOMES: dict[str, list[dict]] = {
    "risk": [
        {"outcome": "victory", "health_loss": 0, "hunger_change": -1, "weight": 3},
        {"outcome": "draw", "health_loss": -1, "hunger_change": 0, "weight": 2},
        {"outcome": "defeat", "health_loss": -2, "hunger_change": +1, "weight": 1},
    ],
    "care": [
        {"outcome": "retreat", "health_loss": 0, "hunger_change": 0, "weight": 3},
        {"outcome": "draw", "health_loss": -1, "hunger_change": 0, "weight": 2},
        {"outcome": "victory", "health_loss": 0, "hunger_change": -1, "weight": 1},
    ],
    "cunning": [
        {"outcome": "victory", "health_loss": 0, "hunger_change": -1, "weight": 3},
        {"outcome": "retreat", "health_loss": 0, "hunger_change": 0, "weight": 2},
        {"outcome": "defeat", "health_loss": -1, "hunger_change": +1, "weight": 1},
    ],
}


# Описания исходов для промпта
OUTCOME_DESCRIPTIONS = {
    "victory": "Стaya победила в столкновении. Враг отступил.",
    "defeat": "Стaya проиграла столкновение. Потери есть.",
    "retreat": "Стая отступила, избегая ненужного боя.",
    "draw": "Бой завершился ничьей. Обе стороны отступили.",
}


def resolve_combat(
    rng: random.Random,
    card_tag: str,
    pack_needs: PackNeeds,
) -> CombatResult:
    """Разрешает бой на основе тега карты и состояния стаи.

    Args:
        rng: Генератор случайных чисел
        card_tag: Тег выбранной карты (risk/care/cunning)
        pack_needs: Текущее состояние стаи

    Returns:
        CombatResult с исходом и потерями
    """
    if card_tag not in COMBAT_OUTCOMES:
        card_tag = "risk"

    outcomes = COMBAT_OUTCOMES[card_tag]

    # Веса зависят от состояния стаи
    modified_outcomes = []
    for o in outcomes:
        weight = o["weight"]
        # Голодные стаи хуже сражаются
        if pack_needs.hunger >= 7:
            if o["outcome"] == "victory":
                weight = max(1, weight - 1)
            elif o["outcome"] == "defeat":
                weight = weight + 1
        # Больные стаи ещё хуже
        if pack_needs.health <= 3:
            if o["outcome"] == "victory":
                weight = max(1, weight - 1)
            elif o["outcome"] == "defeat":
                weight = weight + 2
        modified_outcomes.append({**o, "weight": weight})

    # Выбираем исход по весам
    total_weight = sum(o["weight"] for o in modified_outcomes)
    r = rng.random() * total_weight
    cumulative = 0
    chosen = modified_outcomes[0]
    for o in modified_outcomes:
        cumulative += o["weight"]
        if r <= cumulative:
            chosen = o
            break

    return CombatResult(
        outcome=chosen["outcome"],
        health_loss=chosen["health_loss"],
        hunger_change=chosen["hunger_change"],
        narrative=OUTCOME_DESCRIPTIONS.get(chosen["outcome"], ""),
    )


def apply_combat_effects(result: CombatResult, pack_needs: PackNeeds) -> dict:
    """Применяет эффекты боя к потребностям стаи.

    Возвращает словарь с описанием произошедшего.
    """
    pack_needs.health = max(0, min(10, pack_needs.health + result.health_loss))
    pack_needs.hunger = max(0, min(10, pack_needs.hunger + result.hunger_change))

    changes = []
    if result.health_loss != 0:
        changes.append(f"здоровье {'+' if result.health_loss > 0 else ''}{result.health_loss}")
    if result.hunger_change != 0:
        changes.append(f"голод {'+' if result.hunger_change > 0 else ''}{result.hunger_change}")

    return {
        "outcome": result.outcome,
        "text": result.narrative,
        "changes": changes,
    }


def get_combat_block(result: CombatResult) -> str | None:
    """Возвращает блок для промпта. None — боя не было."""
    if result.outcome == "draw":
        return None  # Ничья — ничего не пишем

    return (
        f"ИСХОД БОЯ: {OUTCOME_DESCRIPTIONS.get(result.outcome, '')}\n"
        f"Потери: здоровье {result.health_loss:+d}, голод {result.hunger_change:+d}"
    )


def check_surprise_encounter(rng: random.Random) -> bool:
    """Проверяет, произошло ли внезапное столкновение.

    Шанс 10% каждый день.
    """
    return rng.random() < 0.10
