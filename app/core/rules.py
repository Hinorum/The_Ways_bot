"""Единый реестр правил мира: сдвиги по тегу победившей карты.

Раньше четыре изолированные числовые таблицы (потребности стаи, эмоции,
отношения с NPC, нрав мира) жили в своих модулях и не имели единой точки
правды — теги care/risk/cunning связывались только вручную. Теперь все
числовые сдвиги определены здесь одним словарём TAG_SHIFTS; модули-
потребители импортируют свой срез под прежними именами, поведение не
меняется. RULE_PHRASES/RULE_MASKS в этот реестр не входят — они уже
единственны (app/models.py) и переиспользуются импортом.

Значение сдвига — int либо callable(rng) -> int для детерминированного
хаоса (см. alignment.risk.moral_axis: знак зависит от сида дня).
"""

from __future__ import annotations

TAG_SHIFTS: dict[str, dict[str, dict[str, object]]] = {
    # Потребности стаи (app/pack_state.py): hunger/thirst 0-10, health 0-10.
    "needs": {
        "risk": {"hunger": 1, "thirst": 1, "health": -1},
        "care": {"hunger": -2, "thirst": -1, "health": 1},
        "cunning": {"hunger": 0, "thirst": 1, "health": 0},
    },
    # Эмоциональный профиль (app/emotional_state.py): fatigue/hope/paranoia 0-10.
    "emotions": {
        "risk": {"fatigue": 1, "hope": -1, "paranoia": 0},
        "care": {"fatigue": -1, "hope": 1, "paranoia": 0},
        "cunning": {"fatigue": 0, "hope": -1, "paranoia": 1},
    },
    # Отношения с лицами мира (app/relations.py): liner/journal/master/heretic -3..3.
    "relations": {
        "care": {"liner": 1, "journal": 1, "master": -1, "heretic": 0},
        "cunning": {"liner": 1, "journal": 1, "master": 1, "heretic": 1},
        "risk": {"liner": -1, "journal": -1, "master": 1, "heretic": 1},
    },
    # Нрав стаи (app/season.py): order_axis/moral_axis -5..5.
    # risk.moral_axis — лямбда: знак выбирается от сида дня (хаос ≠ зло).
    "alignment": {
        "care": {"moral_axis": 1, "order_axis": 1},
        "risk": {"order_axis": -1, "moral_axis": lambda rng: rng.choice((1, -1))},
        "cunning": {"moral_axis": -1, "order_axis": 1},
    },
}

NEED_SHIFTS = TAG_SHIFTS["needs"]
EMOTION_SHIFTS = TAG_SHIFTS["emotions"]
RELATION_SHIFTS = TAG_SHIFTS["relations"]
ALIGNMENT_DRIFT = TAG_SHIFTS["alignment"]


def shifts_for(dimension: str, tag: str) -> dict[str, object]:
    """Срез сдвигов для параметра мира по тегу; пусто при неизвестном."""
    table = TAG_SHIFTS.get(dimension)
    if not table:
        return {}
    return table.get(tag, {})