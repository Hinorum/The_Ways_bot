"""Личная память собак стаи: подавленный слой до лабиринта.

У каждой собаки стаи была жизнь до лабиринта, и она не стёрта — она
подавлена. Шрам мира (WorldScar) и память связаны: когда страх/хитрость
оставляет на мире след, у собаки-героя дня всплывает следующий слой
памяти. Тёплые шрамы (очаг, святилище) исцеляют — память принимается.

Жизненный цикл записи: suppressed (подавлена) → recalled (всплыла)
→ healed (принята). Это не механика наказания, а дорога к правде о стае.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DogMemory

logger = logging.getLogger(__name__)

# Собаки стаи — те же ключи, что у pack focus (story._PACK_CHARS).
_DOG_KEYS = ("баркод", "стежка", "вектор", "пиксель", "безымянная")

_DOG_DISPLAY = {
    "баркод": "Баркод",
    "стежка": "Стежка",
    "вектор": "Вектор",
    "пиксель": "Пиксель",
    "безымянная": "Безымянная",
}

# Жизнь до лабиринта: один подавленный слой на собаку.
_BIRTH_MEMORIES: dict[str, str] = {
    "баркод": (
        "до лабиринта Баркод считал миски за тремя дворами и ни разу не сбился — "
        "но каждый вечер одна кость была лишней, и это его тревожило больше всего"
    ),
    "стежка": (
        "до лабиринта Стежка жила у старого забора и знала запах каждого, кто "
        "проходил мимо, — никого не остановила, и простить себе этого не может"
    ),
    "вектор": (
        "до лабиринта Вектор стоял на пустом мосту и ждал стаю, которая шла другой "
        "дорогой, — он всё ещё ждёт, просто теперь ждут вместе с ним"
    ),
    "пиксель": (
        "до лабиринта Пиксель ловил лапой огоньки фонарей, думая, что это искры, "
        "— и однажды поймал одну, и она не погасла, и именно она привела его сюда"
    ),
    "безымянная": (
        "до лабиринта у Безымянной было имя, но она отдала его тому, кто боялся "
        "темноты, — теперь у неё нет имени, а у той девочки есть свет"
    ),
}

# Исцеляющие шрамы: снимают по одной подавленной памяти со стаи.
_HEALING_SCAR_KEYS = {
    "warm_hearth",
    "sanctuary",
    "gentle_breath",
    "warm_hearth_2",
}


def dog_display_name(dog_key: str) -> str:
    """Человеческое имя собаки для текстов."""
    return _DOG_DISPLAY.get(dog_key, dog_key)


async def seed_dog_memories(session: AsyncSession, season: int = 1) -> int:
    """Одна подавленная память на собаку. Идемпотентно по (dog_key, kind)."""
    inserted = 0
    for dog_key, summary in _BIRTH_MEMORIES.items():
        exists = (
            await session.execute(
                select(DogMemory.id)
                .where(DogMemory.dog_key == dog_key, DogMemory.kind == "birth")
                .limit(1)
            )
        ).first()
        if exists:
            continue
        session.add(
            DogMemory(
                dog_key=dog_key,
                kind="birth",
                summary=summary,
                scar_key="",
                created_day=0,
                state="suppressed",
            )
        )
        inserted += 1
    if inserted:
        await session.commit()
    return inserted


async def surface_dog_memory(
    session: AsyncSession,
    dog_key: str,
    scar_key: str,
    day_index: int,
) -> DogMemory | None:
    """Всплывает подавленная память собаки-героя дня от шрама мира.

    Если подавленной памяти ещё нет (старт до сидинга) — создаёт её из
    шаблона, чтобы слой жизни до лабиринта всегда был чем поднять.
    """
    if dog_key not in _DOG_KEYS:
        return None
    row = (
        await session.execute(
            select(DogMemory)
            .where(DogMemory.dog_key == dog_key, DogMemory.state == "suppressed")
            .order_by(DogMemory.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        row = DogMemory(
            dog_key=dog_key,
            kind="birth",
            summary=_BIRTH_MEMORIES.get(dog_key, f"до лабиринта {dog_key} жил тихо и ждал стаю"),
            scar_key=scar_key or "",
            created_day=day_index,
            state="suppressed",
        )
        session.add(row)
        await session.flush()
    row.state = "recalled"
    row.surfaced_day = day_index
    if scar_key:
        row.scar_key = scar_key
    await session.flush()
    return row


async def heal_dog_memory(
    session: AsyncSession,
    scar_key: str,
    day_index: int,
) -> DogMemory | None:
    """Исцеляющий шрам принимает одну всплывшую память стаи."""
    row = (
        await session.execute(
            select(DogMemory)
            .where(DogMemory.state == "recalled")
            .order_by(DogMemory.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    row.state = "healed"
    row.healed_day = day_index
    if scar_key:
        row.scar_key = f"{row.scar_key}+{scar_key}".strip("+")
    await session.flush()
    return row


async def day_dog_memory_sync(
    session: AsyncSession,
    dog_key: str | None,
    yesterday_tag: str | None,
    day_index: int,
) -> str | None:
    """Синхронизирует память с обычным днём (без новых шрамов мира).

    Заботливый день (care) — как тёплый шрам: принимает одну всплывшую память.
    Риск/хитрость дня — как боль: поднимают подавленный слой героя дня.
    Возвращает строку-заметку для дневника стаи или None.
    """
    if dog_key is None:
        return None
    if yesterday_tag == "care":
        healed = await heal_dog_memory(session, "care", day_index)
        if healed is None:
            return None
        return (
            f"💚 День прошёл по-домашнему, и {_DOG_DISPLAY.get(dog_key, dog_key)} "
            "приняла осколок своей памяти. Часть её прошлого перестала болеть."
        )
    if yesterday_tag in ("risk", "cunning"):
        surfaced = await surface_dog_memory(session, dog_key, yesterday_tag, day_index)
        if surfaced is None:
            return None
        return (
            f"🌑 {_DOG_DISPLAY.get(dog_key, dog_key)} притихла: слой её жизни до "
            "лабиринта всплыл и не хочет прятаться. Память пугает — но она своя."
        )
    return None


async def dog_memory_block_for(
    session: AsyncSession,
    dog_key: str,
) -> str | None:
    """Блок для промпта главы: личная память собаки-героя дня.

    Подавленную память Ведущий может проявить одной сценой-обрывком.
    None — памяти у собаки нет или она ещё подавлена без шрама.
    """
    row = (
        await session.execute(
            select(DogMemory)
            .where(DogMemory.dog_key == dog_key, DogMemory.state.in_(("recalled", "healed")))
            .order_by(DogMemory.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    phase = "она принята" if row.state == "healed" else "она только всплыла и пугает"
    return (
        f"ЛИЧНАЯ ПАМЯТЬ СТАИ ({_DOG_DISPLAY.get(dog_key, dog_key)}): {row.summary}; "
        f"{phase}. Одна состоявшаяся сцена-обрывок этой памяти в главе — не более."
    )


async def icon_memories(
    session: AsyncSession,
    limit: int = 3,
) -> list[DogMemory]:
    """Всплывшие или принятые памяти для хроники /lore: от свежих к старым."""
    result = await session.execute(
        select(DogMemory)
        .where(DogMemory.state.in_(("recalled", "healed")))
        .order_by(DogMemory.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def apply_scar_to_memory(
    session: AsyncSession,
    scar_key: str,
    dog_key: str,
    day_index: int,
) -> str | None:
    """Связывает новый шрам мира с личной памятью стаи.

    Исцеляющий шрам принимает всплывшую память; любой другой — поднимает
    подавленный слой у собаки-героя дня. Возвращает строку-заметку для
    дневника стаи или None, если связывать память не с чем.
    """
    if scar_key in _HEALING_SCAR_KEYS:
        healed = await heal_dog_memory(session, scar_key, day_index)
        if healed is None:
            return None
        return (
            f"💚 {_DOG_DISPLAY.get(dog_key, dog_key)} рассказала осколок памяти, "
            "который наконец-то отпустил. Часть её прошлого принята с миром."
        )
    surfaced = await surface_dog_memory(session, dog_key, scar_key, day_index)
    if surfaced is None:
        return None
    return (
        f"🌑 {_DOG_DISPLAY.get(dog_key, dog_key)} притихла: от нового шрама мира "
        "всплыл слой её жизни до лабиринта. Память подавлена давно и не хочет прятаться."
    )