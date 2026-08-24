"""Отношения NPC к стае: иллюзия непрерывности становится каноном.

Промпт обещает, что «доброта запоминается», но раньше это состояние нигде
не жило — Ведущий выдумывал отношение заново каждый день. Теперь у трёх
главных лиц есть счётчики -3..+3 в watcher_state; шаги делаются по тегу
победившего пути дня и вплетаются в промпт главы одной строкой тона.

Деньги и механику голосования это не трогает — только реплики и поведение.
"""

from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WatcherState

RELATION_KEY = "npc_relations"

# Лица мира: ключи стабильны для промптов, титулы — человеческие.
NPC_TITLES = {
    "liner": "Лайнер",
    "archivist": "Архивариус",
    "master": "Хозяин Ошибки",
}

_MIN, _MAX = -3, 3

# Шаг за победивший путь дня. Канон лиц:
#   care    — тёплый мир труднее «чинить» ошибками: Архивариус доволен,
#             Лайнеру приятно, Хозяин Ошибки недоволен;
#   cunning — язык Лайнера, корм Хозяина Ошибки, головная боль Архива;
#   risk    — мир трещит: Хозяин доволен, Лайнер настораживается.
_SHIFTS: dict[str, dict[str, int]] = {
    "care": {"liner": 1, "archivist": 1, "master": -1},
    "cunning": {"liner": 1, "archivist": -1, "master": 1},
    "risk": {"liner": -1, "archivist": 0, "master": 1},
}

_TONES = {
    3: ("предан стае", "ходит за стаей хвостом"),
    2: ("расположен", "делает скидки без спроса"),
    1: ("приветлив", "здоровается первым"),
    0: ("ничей", "наблюдает"),
    -1: ("насторожен", "отвечает уклончиво"),
    -2: ("враждебен", "не выходит из тумана"),
    -3: ("охотится на стаю", "его приметы видны в каждом дне"),
}


def default_relations() -> dict[str, int]:
    return {npc: 0 for npc in NPC_TITLES}


def clamp_relation(value: int) -> int:
    return max(_MIN, min(_MAX, value))


def apply_winner_shift(relations: dict[str, int], winner_tag: str | None) -> dict[str, int]:
    """Шаг отношений по тегу победившего пути. Мутирует и возвращает словарь."""
    if winner_tag not in _SHIFTS:
        return relations
    for npc, shift in _SHIFTS[winner_tag].items():
        relations[npc] = clamp_relation(relations.get(npc, 0) + shift)
    return relations


def tone_word(value: int) -> str:
    return _TONES[clamp_relation(value)][0]


def relations_prompt_block(relations: dict[str, int]) -> str | None:
    """Строка для промпта главы. None — все отношения нейтральны."""
    parts = [
        f"{NPC_TITLES[npc]} — {tone_line(value)}"
        for npc, value in relations.items()
        if npc in NPC_TITLES and value != 0
    ]
    if not parts:
        return None
    return (
        "ОТНОШЕНИЯ К СТАЕ (канон последних дней, учитывай в репликах): "
        + "; ".join(parts)
        + "."
    )


def tone_line(value: int) -> str:
    clamped = clamp_relation(value)
    word, behaviour = _TONES[clamped]
    sign = "+" if clamped > 0 else ""
    return f"{word} ({sign}{clamped})"


async def load_relations(session: AsyncSession) -> dict[str, int]:
    row = await session.get(WatcherState, RELATION_KEY)
    if row is None or not row.value:
        return default_relations()
    try:
        data = json.loads(row.value)
    except ValueError:
        return default_relations()
    relations = default_relations()
    for npc in NPC_TITLES:
        if isinstance(data.get(npc), int):
            relations[npc] = clamp_relation(data[npc])
    return relations


async def save_relations(session: AsyncSession, relations: dict[str, int]) -> None:
    payload = json.dumps(
        {npc: clamp_relation(relations.get(npc, 0)) for npc in NPC_TITLES},
        ensure_ascii=False,
    )
    row = await session.get(WatcherState, RELATION_KEY)
    if row is None:
        session.add(WatcherState(key=RELATION_KEY, value=payload))
    else:
        row.value = payload


async def apply_round_result(session: AsyncSession, winner_tag: str | None) -> bool:
    """Обновить отношения после итогов дня. True — было изменение."""
    if winner_tag not in _SHIFTS:
        return False
    relations = await load_relations(session)
    before = dict(relations)
    apply_winner_shift(relations, winner_tag)
    if relations == before:
        return False
    await save_relations(session, relations)
    await session.commit()
    return True


async def relations_block_for_session(session: AsyncSession) -> str | None:
    return relations_prompt_block(await load_relations(session))


__all__ = [
    "RELATION_KEY",
    "NPC_TITLES",
    "apply_winner_shift",
    "apply_round_result",
    "clamp_relation",
    "default_relations",
    "load_relations",
    "relations_block_for_session",
    "relations_prompt_block",
    "save_relations",
    "tone_word",
]
