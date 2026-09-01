"""Тесты потребностей стаи."""

from __future__ import annotations

import pytest

from app.pack_state import (
    PackNeeds,
    NEED_SHIFTS,
    apply_needs_shift,
    get_needs_phase,
    get_needs_debuffs,
    get_needs_block,
    check_death,
)


def test_pack_needs_clamp():
    needs = PackNeeds(hunger=15, thirst=-5, health=12, alive_count=7)
    needs.clamp()
    assert needs.hunger == 10
    assert needs.thirst == 0
    assert needs.health == 10
    assert needs.alive_count == 5


def test_apply_risk_shift():
    needs = PackNeeds(hunger=5, thirst=5, health=10)
    apply_needs_shift(needs, "risk")
    assert needs.hunger == 6
    assert needs.thirst == 6
    assert needs.health == 9


def test_apply_care_shift():
    needs = PackNeeds(hunger=5, thirst=5, health=10)
    apply_needs_shift(needs, "care")
    assert needs.hunger == 3
    assert needs.thirst == 4
    assert needs.health == 10


def test_apply_cunning_shift():
    needs = PackNeeds(hunger=5, thirst=5, health=10)
    apply_needs_shift(needs, "cunning")
    assert needs.hunger == 5
    assert needs.thirst == 6
    assert needs.health == 10


def test_apply_unknown_tag():
    needs = PackNeeds(hunger=5, thirst=5, health=10)
    apply_needs_shift(needs, "unknown")
    assert needs.hunger == 5
    assert needs.thirst == 5
    assert needs.health == 10


def test_needs_phase_dead():
    needs = PackNeeds(hunger=5, thirst=5, health=0)
    assert get_needs_phase(needs) == "dead"


def test_needs_phase_dying():
    needs = PackNeeds(hunger=5, thirst=5, health=3)
    assert get_needs_phase(needs) == "dying"


def test_needs_phase_desperate():
    needs = PackNeeds(hunger=8, thirst=8, health=10)
    assert get_needs_phase(needs) == "desperate"


def test_needs_phase_struggling():
    needs = PackNeeds(hunger=7, thirst=5, health=10)
    assert get_needs_phase(needs) == "struggling"


def test_needs_phase_hungry():
    needs = PackNeeds(hunger=5, thirst=5, health=10)
    assert get_needs_phase(needs) == "hungry"


def test_needs_phase_sated():
    needs = PackNeeds(hunger=2, thirst=2, health=10)
    assert get_needs_phase(needs) == "sated"


def test_needs_debuffs_hunger():
    needs = PackNeeds(hunger=8, thirst=2, health=10)
    debuffs = get_needs_debuffs(needs)
    assert debuffs.get("risk_penalty", 0) < 0


def test_needs_debuffs_thirst():
    needs = PackNeeds(hunger=2, thirst=8, health=10)
    debuffs = get_needs_debuffs(needs)
    assert debuffs.get("cunning_penalty", 0) < 0


def test_needs_debuffs_low_health():
    needs = PackNeeds(hunger=2, thirst=2, health=3)
    debuffs = get_needs_debuffs(needs)
    assert debuffs.get("risk_penalty", 0) < 0


def test_needs_block_dead():
    needs = PackNeeds(hunger=5, thirst=5, health=0)
    block = get_needs_block(needs)
    assert block is not None
    assert "ПОГИБЛИ" in block


def test_needs_block_dying():
    needs = PackNeeds(hunger=5, thirst=5, health=3)
    block = get_needs_block(needs)
    assert block is not None
    assert "КРИТИЧЕСКОЕ" in block


def test_needs_block_none_when_sated():
    needs = PackNeeds(hunger=2, thirst=2, health=10)
    block = get_needs_block(needs)
    assert block is None


def test_check_death():
    needs = PackNeeds(hunger=5, thirst=5, health=0)
    assert check_death(needs) == True
    needs = PackNeeds(hunger=5, thirst=5, health=1)
    assert check_death(needs) == False


def test_all_shifts_defined():
    for tag in ("risk", "care", "cunning"):
        assert tag in NEED_SHIFTS
        assert "hunger" in NEED_SHIFTS[tag]
        assert "thirst" in NEED_SHIFTS[tag]
        assert "health" in NEED_SHIFTS[tag]
