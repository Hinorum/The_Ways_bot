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
    assert relations == {"liner": 1, "journal": 1, "master": -1, "heretic": 0}
    for _ in range(5):
        apply_winner_shift(relations, "cunning")
    assert relations["liner"] == 3  # +6 → кламп до 3
    # Хитрость = новые расхождения для дневника: +1 за ход → +6 → кламп до 3.
    assert relations["journal"] == 3
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
    assert "Лайнер — расположен" in block
    assert "(+2)" not in block  # наружу — только слово-тон, без чисел
    assert "Хозяин Ошибки" not in block  # нулевой — не упоминается
    assert tone_word(-3) == "охотится на стаю"
    # Все нули — блока нет.
    assert relations_prompt_block(default_relations()) is None


async def test_persist_and_load(session) -> None:
    await save_relations(session, {"liner": 2, "journal": -1, "master": 0})
    await session.commit()
    loaded = await load_relations(session)
    # Еретик — четвёртое лицо канона: отсутствующий ключ сохраняется нулём.
    assert loaded == {"liner": 2, "journal": -1, "master": 0, "heretic": 0}
    row = await session.get(WatcherState, RELATION_KEY)
    assert row is not None


async def test_apply_round_result_commits_step(session) -> None:
    changed = await apply_round_result(session, "risk")
    assert changed is True
    loaded = await load_relations(session)
    assert loaded["master"] == 1 and loaded["liner"] == -1 and loaded["journal"] == -1
    # Трещина мира — Еретик доволен.
    assert loaded["heretic"] == 1
    # Неизвестный тег — шага нет.
    assert await apply_round_result(session, "dragon") is False


def test_every_npc_has_title() -> None:
    relations = default_relations()
    assert set(relations) == set(NPC_TITLES)


# ── Парные связи NPC↔NPC ──


def test_pair_key_normalized() -> None:
    from app.relations import pair_key

    assert pair_key("liner", "journal") == "journal-liner"
    assert pair_key("journal", "liner") == "journal-liner"


def test_default_pairs_have_all_face_pairs() -> None:
    from app.relations import default_pair_relations, pair_key

    pairs = default_pair_relations()
    keys = list(NPC_TITLES)
    expected = {
        pair_key(keys[i], keys[j])
        for i in range(len(keys))
        for j in range(i + 1, len(keys))
    }
    assert set(pairs) == expected
    assert all(v == 0 for v in pairs.values())


def test_pair_shift_by_tag_and_clamp() -> None:
    from app.relations import apply_pair_shift, default_pair_relations

    pairs = default_pair_relations()
    apply_pair_shift(pairs, "care")
    assert pairs["journal-liner"] == 1
    assert pairs["heretic-master"] == -1
    for _ in range(5):
        apply_pair_shift(pairs, "care")
    assert pairs["heretic-master"] == -3  # кламп до -3
    before = dict(pairs)
    apply_pair_shift(pairs, "dragon")
    assert pairs == before


def test_pair_prompt_block_words_only() -> None:
    from app.relations import pair_prompt_block

    pairs = {"journal-liner": 2, "heretic-master": -2, "master-liner": 0}
    block = pair_prompt_block(pairs)
    assert block is not None
    assert "Дневник и Лайнер — близки" in block
    assert "Еретик и Администратор — против" in block
    assert "0" not in block  # нет чисел
    assert "(-2)" not in block
    # Все нули — блока нет.
    from app.relations import default_pair_relations

    assert pair_prompt_block(default_pair_relations()) is None


async def test_pair_round_result_commits_step(session) -> None:
    from app.relations import (
        PAIR_RELATION_KEY,
        apply_pair_round_result,
        load_pair_relations,
    )

    changed = await apply_pair_round_result(session, "risk")
    assert changed is True
    loaded = await load_pair_relations(session)
    assert loaded["heretic-master"] == 1
    assert loaded["journal-liner"] == 1
    row = await session.get(WatcherState, PAIR_RELATION_KEY)
    assert row is not None
    # Неизвестный тег — шага нет.
    assert await apply_pair_round_result(session, "dragon") is False
