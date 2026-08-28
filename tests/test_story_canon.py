"""Канон «Еретика»: новая сюжетная рамка The Way's.

Рамка: ветеран старой Стаи (LostDogs-подобный мир одного сна на всех)
заскучал, свернул с Пути и построил свою игру. Каждая механика бота
получает внутримировое обоснование как его изобретение.
"""

from __future__ import annotations

import pytest

from app.bestiary import BEASTIES
from app.callings import CALLINGS, echo_tail, unlocked_callings
from app.council import _COUNCIL_PAGES as council_pages, page_for_run_day
from app.prologue import PROLOGUE_BEATS, prologue_block, prologue_title
from app.relations import NPC_TITLES, NPC_WANTS, apply_winner_shift, default_relations
from app.rounds import _GUEST_POOL, guest_blocks_for
from app.season import heretic_event, heretic_prompt_block


# ---------- Ротация гостов: Еретик занял место книги обещаний ----------


def test_guest_pool_has_heretic_instead_of_promises() -> None:
    assert "promises" not in _GUEST_POOL
    assert "heretic" in _GUEST_POOL
    # Пары по-прежнему сходятся без самопересечений на всём цикле пула.
    for day in range(1, 61):
        guests = guest_blocks_for(day)
        assert len(guests) == 2
        assert all(name in _GUEST_POOL for name in guests)


# ---------- Сюжет-машина «Правил Еретика» ----------


def test_heretic_event_stable_within_slot_and_varies() -> None:
    """Событие канонично внутри ~4-дневного окна, детерминировано и
    ротируется между окнами и ступенями."""
    # Дни 28-31 — один слот (slot = day // 4 == 7).
    window = {heretic_event("2026-08", 2, day) for day in range(28, 32)}
    assert len(window) == 1
    assert heretic_event("2026-08", 2, 31) == heretic_event("2026-08", 2, 31)
    across_slots = {
        heretic_event("2026-08", s, slot * 4 + 1)
        for s in range(4)
        for slot in range(5)
    }
    assert len(across_slots) >= 3  # между окнами и ступенями линия живёт


@pytest.mark.parametrize("stage", [0, 1, 2, 3])
def test_heretic_prompt_block_shape(stage: int) -> None:
    block = heretic_prompt_block("2026-08", stage, run_day=10)
    assert block is not None and "ПРАВИЛА ЕРЕТИКА" in block
    assert "одним касанием" in block


# ---------- Живые системы: лицо, бестиарий, призвание ----------


def test_heretic_is_fourth_npc_face_with_wants() -> None:
    assert set(default_relations()) == set(NPC_TITLES)
    assert "heretic" in NPC_TITLES and NPC_TITLES["heretic"] == "Еретик"
    # Хотелки есть у всех лиц — фокус-дни не падают на пустом пуле.
    for npc in NPC_TITLES:
        assert NPC_WANTS.get(npc)


def test_heretic_approves_chaos_not_cozy() -> None:
    relations = default_relations()
    apply_winner_shift(relations, "risk")
    apply_winner_shift(relations, "cunning")
    apply_winner_shift(relations, "care")
    assert relations["heretic"] == 2  # риск и хитрость — его язык; тепло — нет


def test_bestiary_contains_heretic_and_cat() -> None:
    assert "heretic" in BEASTIES and "Свернувший с Пути" in BEASTIES["heretic"][0]
    assert "cat" in BEASTIES and BEASTIES["cat"][0] == "Кошачий след"


def test_heretic_calling_unlocks_on_double_sealed() -> None:
    keys = {c.key for c in CALLINGS}
    assert "heretic" in keys
    # Одно попадание — только Мутант; Раскольник требует двойного.
    assert "heretic" not in {c.key for c in unlocked_callings({"sealed_correct": 1})}
    unlocked = unlocked_callings({"sealed_correct": 2})
    assert any(c.key == "heretic" for c in unlocked)
    assert echo_tail("heretic")


# ---------- Пролог и ARG ----------


def test_prologue_day5_introduces_heretic() -> None:
    titles = [beat["title"] for beat in PROLOGUE_BEATS.values()]
    assert len(titles) == len(set(titles))
    assert prologue_title(5) == "Свернувший с Пути"
    block = prologue_block(5)
    assert block is not None and "Еретик" in block
    # Старый мир присутствует намёком, но без имён.
    assert "старой Стаи" in block


def test_council_pages_cadence_and_rotation() -> None:
    from app import council

    assert page_for_run_day(0) is None
    assert page_for_run_day(3) is None  # обычный привал
    first = page_for_run_day(4)
    second = page_for_run_day(8)
    third = page_for_run_day(12)
    assert first and "НАЙДЕНА СТРАНИЦА" in first
    assert first != second != third  # страницы не повторяются подряд
    total_pages = len(council._COUNCIL_PAGES)
    assert page_for_run_day(council.COUNCIL_DAY_MODULO * (total_pages + 1)) == first  # цикл замкнулся


def test_council_all_pages_reachable_in_short_run() -> None:
    """Регресс-ловушка: раньше страницы №5 и №6 (манифест Еретика) не
    успевали попасться в месячном забеге (29 дней при 7-дневном шаге).
    Теперь каждая из 6 страниц встретится уже к 24-му дню забега."""
    from app import council

    total_pages = len(council._COUNCIL_PAGES)
    seen = set()
    for day in range(1, 25):
        page = page_for_run_day(day)
        if page is not None:
            seen.add(page)
    # Все страницы, включая манифест №6, доступны до финала короткого сезона.
    assert len(seen) == total_pages


def test_council_manifesto_is_page_six() -> None:
    """Манифест Еретика отдан Совету: страница №6, единственная не от Совета."""
    pages = [page for page in council_pages if "№6" in page]
    assert pages and "Апостроф — мой" in pages[0]
    assert "The Way’s" in pages[0]
    assert "Старая Стая выбирала один сон на миллион" in pages[0]


def test_cat_teases_season_two_soon() -> None:
    """Кошачье ружьё не снято со стены: сезон второй — после техработ. SOON."""
    description = BEASTIES["cat"][1]
    assert "SOON" in description
    assert "после технических работ" in description


def test_heretic_has_neural_makeup_in_art_conveyor() -> None:
    """НейроГримёр: силуэт Еретика стабилен в кадрах по обоим именам."""
    from app.art_director import character_motifs_for

    by_short = character_motifs_for("Еретик выцарапал новое правило на стене")
    by_full = character_motifs_for("Свернувший с Пути молча показал тропу")
    assert by_short == by_full and len(by_short) == 1
    descriptor = by_short[0]
    assert "Heretic the Way-Leaver" in descriptor
    # Костюм-биография и знак присвоения читаются в кадре.
    assert "old maps" in descriptor and "apostrophe-shaped" in descriptor
