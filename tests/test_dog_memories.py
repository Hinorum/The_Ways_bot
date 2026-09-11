"""Личная память собак стаи: подавление/всплытие/принятие и блок для промпта.

Контракты жизни памяти: suppressed → recalled → healed; сидинг идемпотентен;
заботливые/исцеляющие события принимают память, риск/хитрость всплывают.
"""

from sqlalchemy import select

from app.dog_memories import (
    apply_scar_to_memory,
    day_dog_memory_sync,
    dog_memory_block_for,
    heal_dog_memory,
    healed_memories_count,
    seed_dog_memories,
    surface_dog_memory,
)
from app.models import DogMemory


async def test_seed_is_idempotent_and_creates_five(session) -> None:
    await seed_dog_memories(session)
    await seed_dog_memories(session)  # повтор — ничего нового
    rows = (
        await session.execute(select(DogMemory))
    ).scalars().all()
    assert len(rows) == 5
    assert all(row.state == "suppressed" for row in rows)
    assert {row.dog_key for row in rows} == {
        "баркод", "стежка", "вектор", "пиксель", "безымянная",
    }


async def test_surface_marks_recalled_and_keeps_scar(session) -> None:
    await seed_dog_memories(session)
    row = await surface_dog_memory(session, "баркод", "burned_path", 5)
    assert row is not None
    assert row.state == "recalled"
    assert row.surfaced_day == 5
    assert row.scar_key == "burned_path"
    combined = (
        await session.execute(select(DogMemory).where(DogMemory.dog_key == "баркод"))
    ).scalars().all()
    assert all(r.state == "recalled" for r in combined)


async def test_surface_creates_from_template_without_seed(session) -> None:
    # Старт до сидинга: память создаётся из шаблона, не падает.
    row = await surface_dog_memory(session, "вектор", "", 1)
    assert row is not None
    assert row.state == "recalled"
    block = await dog_memory_block_for(session, "вектор")
    assert block is not None and "лабиринта" in block


async def test_heal_takes_oldest_recalled(session) -> None:
    await seed_dog_memories(session)
    await surface_dog_memory(session, "баркод", "risk", 2)
    await surface_dog_memory(session, "стежка", "risk", 3)
    healed = await heal_dog_memory(session, "warm_hearth", 4)
    assert healed is not None and healed.dog_key == "баркод"
    assert healed.state == "healed"
    assert healed.healed_day == 4
    assert healed.scar_key == "risk+warm_hearth"
    # Пока recalled не пусто, heal берёт следующую самую старую память.
    second = await heal_dog_memory(session, "warm_hearth", 5)
    assert second is not None and second.dog_key == "стежка"
    # Больше recalled нет — heal больше нечего принять.
    assert await heal_dog_memory(session, "warm_hearth", 6) is None


async def test_day_sync_maps_care_and_risk(session) -> None:
    await seed_dog_memories(session)
    # Забота принимает уже всплывшую память, а не создаёт её.
    assert await day_dog_memory_sync(session, "баркод", "care", 1) is None
    await surface_dog_memory(session, "баркод", "risk", 2)
    care_note = await day_dog_memory_sync(session, "баркод", "care", 6)
    assert care_note is not None and "Баркод" in care_note
    assert await healed_memories_count(session) == 1
    risk_note = await day_dog_memory_sync(session, "стежка", "risk", 7)
    assert risk_note is not None and "Стежка" in risk_note
    cunning_note = await day_dog_memory_sync(session, "вектор", "cunning", 8)
    assert cunning_note is not None
    assert await day_dog_memory_sync(session, "вектор", None, 9) is None
    assert await day_dog_memory_sync(session, None, "care", 10) is None


async def test_memory_block_shows_phase_phrase(session) -> None:
    await seed_dog_memories(session)
    await surface_dog_memory(session, "пиксель", "risk", 3)
    recalled_block = await dog_memory_block_for(session, "пиксель")
    assert recalled_block is not None
    assert "всплыла и пугает" in recalled_block
    assert "Пиксель" in recalled_block
    await heal_dog_memory(session, "warm_hearth", 4)
    healed_block = await dog_memory_block_for(session, "пиксель")
    assert healed_block is not None and "она принята" in healed_block
    # Подавленная без сцен память в промпт не попадает.
    assert await dog_memory_block_for(session, "безымянная") is None


async def test_apply_scar_heals_or_surfaces(session) -> None:
    await seed_dog_memories(session)
    await surface_dog_memory(session, "баркод", "risk", 2)
    heal_note = await apply_scar_to_memory(session, "warm_hearth", "баркод", 5)
    assert heal_note is not None and "принята" in heal_note
    assert await healed_memories_count(session) == 1
    scar_note = await apply_scar_to_memory(session, "burned_path", "стежка", 6)
    assert scar_note is not None and "всплыл" in scar_note