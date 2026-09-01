"""Динамические правила — переопределения голосования на основе выборов.

Правила меняются в зависимости от:
- Шрамов мира (linger_debuff)
- Эмоционального профиля (fatigue >= 7)
- Деревьев последствий (активные ветви)
- Комбинаций (шрам + эмоция)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models import WorldScar, EmotionalState
from app.consequence_trees import ConsequenceBranch


@dataclass
class RuleOverride:
    """Переопределение правила голосования."""
    rule_id: str
    description: str
    effects: dict = field(default_factory=dict)
    duration_days: int = 0  # 0 = permanent while active
    priority: int = 0  # Чем выше, тем важнее


# Словарь всех возможных переопределений
RULE_OVERRIDES: dict[str, RuleOverride] = {
    "scar_reduce_weight": RuleOverride(
        rule_id="scar_reduce_weight",
        description="Шрам мира снижает вес голоса за связанные карты",
        effects={"card_weight_modifier": -0.2},
        priority=1,
    ),
    "fatigue_increase_care": RuleOverride(
        rule_id="fatigue_increase_care",
        description="Усталость стаи усиливает care-карты",
        effects={"care_weight_bonus": 0.15},
        priority=2,
    ),
    "paranoia_increase_cunning": RuleOverride(
        rule_id="paranoia_increase_cunning",
        description="Паранойя стаи усиливает cunning-карты",
        effects={"cunning_weight_bonus": 0.15},
        priority=2,
    ),
    "debt_threat_override": RuleOverride(
        rule_id="debt_threat_override",
        description="Долг чужой стаи — голос за risk снижается, cunning растёт",
        effects={"risk_weight_penalty": 0.1, "cunning_weight_bonus": 0.2},
        priority=3,
        duration_days=5,
    ),
    "bridge_gone_override": RuleOverride(
        rule_id="bridge_gone_override",
        description="Сожжённый мост — увеличивает вес risk-карт",
        effects={"risk_weight_bonus": 0.15},
        priority=2,
        duration_days=7,
    ),
    "sanctuary_protection": RuleOverride(
        rule_id="sanctuary_protection",
        description="Убежище защищает — снижает вес risk-карт",
        effects={"risk_weight_penalty": 0.2},
        priority=2,
        duration_days=3,
    ),
    "labyrinth_doubt_override": RuleOverride(
        rule_id="labyrinth_doubt_override",
        description="Лабиринт сомневается — усиливает cunning, ослабляет risk",
        effects={"cunning_weight_bonus": 0.2, "risk_weight_penalty": 0.15},
        priority=3,
        duration_days=6,
    ),
    "exhaustion_combined": RuleOverride(
        rule_id="exhaustion_combined",
        description="Усталость + шрам — сильный дебафф на risk",
        effects={"risk_weight_penalty": 0.25},
        priority=4,
    ),
}


def get_active_overrides(
    scars: list[WorldScar],
    emotions: EmotionalState,
    branches: list[ConsequenceBranch],
    current_day: int,
) -> list[RuleOverride]:
    """Определяет активные переопределения правил."""
    overrides = []

    # Шрамы мира: снижают вес связанных карт
    for scar in scars:
        # Шрамы с effect_type "block_place" или "modify_tone" снижают вес
        if scar.metadata_json:
            overrides.append(RULE_OVERRIDES["scar_reduce_weight"])
            break  # Достаточно одного шрама для дебаффа

    # Эмоции: fatigue >= 7
    if emotions.fatigue >= 7:
        overrides.append(RULE_OVERRIDES["fatigue_increase_care"])

    # Эмоции: paranoia >= 7
    if emotions.paranoia >= 7:
        overrides.append(RULE_OVERRIDES["paranoia_increase_cunning"])

    # Деревья последствий: проверяем активные ветви
    for branch in branches:
        if branch.branch_key == "foreign_pack_debt" and not branch.resolved:
            override = RULE_OVERRIDES["debt_threat_override"]
            if branch.created_day + override.duration_days >= current_day:
                overrides.append(override)
        elif branch.branch_key == "burned_bridge" and not branch.resolved:
            override = RULE_OVERRIDES["bridge_gone_override"]
            if branch.created_day + override.duration_days >= current_day:
                overrides.append(override)
        elif branch.branch_key == "warm_hearth" and not branch.resolved:
            override = RULE_OVERRIDES["sanctuary_protection"]
            if branch.created_day + override.duration_days >= current_day:
                overrides.append(override)
        elif branch.branch_key == "labyrinth_doubt" and not branch.resolved:
            override = RULE_OVERRIDES["labyrinth_doubt_override"]
            if branch.created_day + override.duration_days >= current_day:
                overrides.append(override)

    # Комбинации: fatigue + любой шрам
    if emotions.fatigue >= 5 and any(s.metadata_json for s in scars):
        overrides.append(RULE_OVERRIDES["exhaustion_combined"])

    # Сортируем по приоритету
    overrides.sort(key=lambda o: o.priority, reverse=True)

    # Убираем дубликаты
    seen = set()
    unique = []
    for o in overrides:
        if o.rule_id not in seen:
            seen.add(o.rule_id)
            unique.append(o)

    return unique


def apply_overrides(
    card_weights: dict[str, float],
    overrides: list[RuleOverride],
) -> dict[str, float]:
    """Применяет переопределения к весам карточек."""
    modified = card_weights.copy()

    for override in overrides:
        effects = override.effects

        if "risk_weight_bonus" in effects:
            modified["risk"] = modified.get("risk", 0.33) + effects["risk_weight_bonus"]
        if "risk_weight_penalty" in effects:
            modified["risk"] = modified.get("risk", 0.33) - effects["risk_weight_penalty"]
        if "care_weight_bonus" in effects:
            modified["care"] = modified.get("care", 0.33) + effects["care_weight_bonus"]
        if "cunning_weight_bonus" in effects:
            modified["cunning"] = modified.get("cunning", 0.34) + effects["cunning_weight_bonus"]

    # Нормализация
    total = sum(modified.values())
    if total > 0:
        return {k: v / total for k, v in modified.items()}
    return card_weights


def get_dynamic_rule_text(overrides: list[RuleOverride]) -> str:
    """Форматирует активные правила для промпта."""
    if not overrides:
        return ""

    lines = ["ДИНАМИЧЕСКИЕ ПРАВИЛА:"]
    for o in overrides:
        lines.append(f"- {o.description}")

    return "\n".join(lines) if len(lines) > 1 else ""


def get_streak_bonus(streak: int) -> float:
    """Бонус за серию побед (стрик)."""
    if streak >= 5:
        return 0.3
    elif streak >= 3:
        return 0.2
    elif streak >= 2:
        return 0.1
    return 0.0


def get_streak_penalty(streak: int) -> float:
    """Штраф за серию поражений."""
    if streak >= 5:
        return -0.3
    elif streak >= 3:
        return -0.2
    elif streak >= 2:
        return -0.1
    return 0.0
