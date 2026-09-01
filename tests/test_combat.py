"""Тесты бой как текст."""

from __future__ import annotations

import random
import pytest

from app.combat import (
    COMBAT_OUTCOMES,
    OUTCOME_DESCRIPTIONS,
    resolve_combat,
    apply_combat_effects,
    get_combat_block,
    check_surprise_encounter,
    CombatResult,
)
from app.pack_state import PackNeeds


def test_all_tags_have_outcomes():
    for tag in ("risk", "care", "cunning"):
        assert tag in COMBAT_OUTCOMES
        assert len(COMBAT_OUTCOMES[tag]) > 0


def test_all_outcomes_have_descriptions():
    for outcome in ("victory", "defeat", "retreat", "draw"):
        assert outcome in OUTCOME_DESCRIPTIONS


def test_resolve_combat_risk():
    rng = random.Random(42)
    needs = PackNeeds(hunger=5, thirst=5, health=10)
    result = resolve_combat(rng, "risk", needs)
    assert result.outcome in ("victory", "defeat", "retreat", "draw")
    assert isinstance(result.health_loss, int)
    assert isinstance(result.hunger_change, int)


def test_resolve_combat_care():
    rng = random.Random(42)
    needs = PackNeeds(hunger=5, thirst=5, health=10)
    result = resolve_combat(rng, "care", needs)
    assert result.outcome in ("victory", "defeat", "retreat", "draw")


def test_resolve_combat_cunning():
    rng = random.Random(42)
    needs = PackNeeds(hunger=5, thirst=5, health=10)
    result = resolve_combat(rng, "cunning", needs)
    assert result.outcome in ("victory", "defeat", "retreat", "draw")


def test_resolve_combat_unknown_tag():
    rng = random.Random(42)
    needs = PackNeeds(hunger=5, thirst=5, health=10)
    result = resolve_combat(rng, "unknown", needs)
    assert result.outcome in ("victory", "defeat", "retreat", "draw")


def test_apply_combat_effects_victory():
    needs = PackNeeds(hunger=5, thirst=5, health=10)
    result = CombatResult(outcome="victory", health_loss=0, hunger_change=-1)
    effects = apply_combat_effects(result, needs)
    assert needs.hunger == 4
    assert needs.health == 10
    assert effects["outcome"] == "victory"


def test_apply_combat_effects_defeat():
    needs = PackNeeds(hunger=5, thirst=5, health=10)
    result = CombatResult(outcome="defeat", health_loss=-2, hunger_change=+1)
    effects = apply_combat_effects(result, needs)
    assert needs.health == 8
    assert needs.hunger == 6


def test_apply_combat_effects_clamp():
    needs = PackNeeds(hunger=5, thirst=5, health=1)
    result = CombatResult(outcome="defeat", health_loss=-2, hunger_change=0)
    effects = apply_combat_effects(result, needs)
    assert needs.health == 0


def test_get_combat_block_victory():
    result = CombatResult(outcome="victory", health_loss=0, hunger_change=-1)
    block = get_combat_block(result)
    assert block is not None
    assert "ИСХОД БОЯ" in block


def test_get_combat_block_draw():
    result = CombatResult(outcome="draw", health_loss=-1, hunger_change=0)
    block = get_combat_block(result)
    assert block is None  # Ничья — ничего не пишем


def test_check_surprise_encounter():
    rng = random.Random(42)
    # С фиксированным seed проверяем, что функция работает
    result = check_surprise_encounter(rng)
    assert isinstance(result, bool)


def test_health_loss_always_negative_or_zero():
    rng = random.Random(42)
    needs = PackNeeds(hunger=5, thirst=5, health=10)
    for _ in range(100):
        result = resolve_combat(rng, "risk", needs)
        assert result.health_loss <= 0
