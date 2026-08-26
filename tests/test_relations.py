"""Отношения NPC: шаги по тегам, клампы, тона, промпт-блок, персист в БД."""

from app.models import WatcherState
from app.relations import (
    RELATION_KEY,
    NPC_TITLES,
    apply_round_result,
    apply_winner_shift,
    default_relations,
    load_relations,
    relations_prompt_block,
    save_relations,
    tone_word,
)


def test_shift_mapping_and_clamp() -> None:
    relations = default_relations()
    apply_winner_shift(relations, "care")
    # Еретику тепло — не враг, но и не его дело: ноль.
    assert relations == {"liner": 1, "archivist": 1, "master": -1, "heretic": 0}
    for _ in range(5):
        apply_winner_shift(relations, "cunning")
    assert relations["liner"] == 3  # +6 → кламп до 3
    assert relations["archivist"] == -3
    assert relations["master"] == 3
    # Хитрость — ремесло Еретика: +5 → кламп до 3.
    assert relations["heretic"] == 3
    # Неизвестный тег — пустой шаг.
    before = dict(relations)
    apply_winner_shift(relations, "dragon")
    assert relations == before


def test_tones_and_prompt_block() -> None:
    relations = {"liner": 2, "archivist": -3, "master": 0}
    block = relations_prompt_block(relations)
    assert block is not None
    assert "Лайнер — расположен (+2)" in block
    assert "Хозяин Ошибки" not in block  # нулевой — не упоминается
    assert tone_word(-3) == "охотится на стаю"
    # Все нули — блока нет.
    assert relations_prompt_block(default_relations()) is None


async def test_persist_and_load(session) -> None:
    await save_relations(session, {"liner": 2, "archivist": -1, "master": 0})
    await session.commit()
    loaded = await load_relations(session)
    # Еретик — четвёртое лицо канона: отсутствующий ключ сохраняется нулём.
    assert loaded == {"liner": 2, "archivist": -1, "master": 0, "heretic": 0}
    row = await session.get(WatcherState, RELATION_KEY)
    assert row is not None


async def test_apply_round_result_commits_step(session) -> None:
    changed = await apply_round_result(session, "risk")
    assert changed is True
    loaded = await load_relations(session)
    assert loaded["master"] == 1 and loaded["liner"] == -1 and loaded["archivist"] == 0
    # Трещина мира — Еретик доволен.
    assert loaded["heretic"] == 1
    # Неизвестный тег — шага нет.
    assert await apply_round_result(session, "dragon") is False


def test_every_npc_has_title() -> None:
    relations = default_relations()
    assert set(relations) == set(NPC_TITLES)
