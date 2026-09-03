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
    "journal": "Дневник",
    "master": "Администратор",
    "heretic": "Еретик",
}

_MIN, _MAX = -3, 3

# Шаг за победивший путь дня. Канон лиц:
#   care    — тёплый мир труднее «чинить» ошибками: дневник доволен,
#             Лайнеру приятно, Хозяин Ошибки недоволен; Еретику всё равно
#             (тепло — не его дело, но и не враг);
#   cunning — язык Лайнера И язык Еретика (хитрость = его ремесло),
#             корм Хозяина Ошибки, головная боль дневника;
#   risk    — мир трещит: Хозяин доволен, Лайнер настораживается,
#             Еретик одобряет — трещина значит, что мир ещё живой.
_SHIFTS: dict[str, dict[str, int]] = {
    "care": {"liner": 1, "journal": 1, "master": -1, "heretic": 0},
    "cunning": {"liner": 1, "journal": 1, "master": 1, "heretic": 1},
    "risk": {"liner": -1, "journal": -1, "master": 1, "heretic": 1},
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


# Хотелки NPC: личная цель, которая ротируется фокус-днями. Не влияет на
# экономику — даёт глубину персонажу без нового состояния.
NPC_WANTS = {
    "liner": (
        "хочет вернуть долг с одной давней сделки и потому сегодня щедрее обычного",
        "ищет в стае нос, чующий подделку памяти",
        "мечтает о покупателе, который заплатит воспоминанием, а не проводом",
        "радио на его бедре молчит годами — но он ищет тот, кто услышит в нём первый Лай",
    ),
    "journal": (
        "ищет расхождение между сегодняшним днём и записью в папке — где-то они разошлись",
        "торгует одной версией за другую, но ни одну не показывает до конца",
        "проверяет, не помнит ли стая то, чего нет ни в одной папке",
        "его страницы сегодня показывают одну и ту же запись — и он не может понять, почему",
    ),
    "master": (
        "пересчитывает стаю заново: прошлый счёт снова не сошёлся",
        "оставляет приметы так, чтобы стая сама пришла к развилке",
        "чинит один мир слишком аккуратно — как будто репетирует что-то большое",
        "тоскует по ровному сну, который стая бросила, и чинит мир так бережно, словно боится его разбудить",
    ),
    "heretic": (
        "вырезает из старых папок правило, которого стая ещё не знает, — и "
        "примеряет его к сегодняшнему дню",
        "ищет во стае ту, кто помнит старый сон так же ясно, как он сам",
        "готовит четвёртый путь к Первому Лаю — тот, которого нет ни на "
        "одной карте",
        "показывает одной-единственной собаке выцветший ошейник старой Стаи под пальто — и ждёт, узнает ли она его",
    ),
}


_FOCUS_PHASES = ("завязка", "развитие", "ход")


def npc_focus_line(run_day: int, npc_titles: dict[str, str] | None = None) -> str | None:
    """Микро-линия NPC: одна хотелка развивается три дня подряд
    (завязка → развитие → ход), затем линия переходит к следующему
    персонажу. Детерминировано по дню забега; None — день ≤ 0."""
    if run_day <= 0:
        return None
    arc, phase = divmod(max(1, run_day) - 1, 3)
    keys = list(NPC_TITLES)
    npc = keys[arc % len(keys)]
    wants = NPC_WANTS[npc]
    want = wants[(arc // len(keys)) % len(wants)]
    titles = npc_titles or NPC_TITLES
    return (
        f"ФОКУС ДНЯ [{_FOCUS_PHASES[phase]}] — {titles.get(npc, npc)}: {want}. "
        "Дай этому реплику или жест в сцене."
    )


async def get_npc_titles(session: AsyncSession | None = None) -> dict[str, str]:
    """Возвращает словарь {npc_key: display_name} из БД или хардкода."""
    if session is None:
        return dict(NPC_TITLES)
    try:
        from app.npc_cog import get_npc_names
        db_names = await get_npc_names(session)
        if db_names:
            # Объединяем: БД > хардкод
            result = dict(NPC_TITLES)
            result.update(db_names)
            return result
    except Exception:
        pass
    return dict(NPC_TITLES)


def relations_prompt_block(relations: dict[str, int], npc_titles: dict[str, str] | None = None) -> str | None:
    """Строка для промпта главы. None — все отношения нейтральны."""
    titles = npc_titles or NPC_TITLES
    parts = [
        f"{titles[npc]} — {tone_line(value)}"
        for npc, value in relations.items()
        if npc in titles and value != 0
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
    "get_npc_titles",
    "load_relations",
    "relations_block_for_session",
    "relations_prompt_block",
    "save_relations",
    "tone_word",
]
