"""Тесты шрамов мира: проверка правил, создания, фильтрации локаций."""

from __future__ import annotations

import pytest

from app.scar_rules import (
    ScarRule,
    check_streak_for_scar,
    get_active_scars_for_day,
    get_blocked_places,
    get_tone_modifiers,
    SCAR_RULES,
)
from app.lore import _get_dynamic_places, _PLACES


def test_scar_rule_triggered_by_streak():
    rule = ScarRule(trigger_tag="risk", min_streak=2, scar_key="burned_path")
    # 2 risk подряд → срабатывает
    assert check_streak_for_scar(["risk", "risk"], rule, 10) is not None
    # 1 risk → не срабатывает
    assert check_streak_for_scar(["risk"], rule, 10) is None
    # risk + care → не срабатывает
    assert check_streak_for_scar(["risk", "care"], rule, 10) is None


def test_scar_rule_min_streak():
    rule = ScarRule(trigger_tag="care", min_streak=5, scar_key="sanctuary")
    tags = ["care", "care", "care", "care"]
    assert check_streak_for_scar(tags, rule, 10) is None
    tags.append("care")
    assert check_streak_for_scar(tags, rule, 10) is not None


def test_active_scars_for_day():
    from unittest.mock import MagicMock

    scar1 = MagicMock(expires_day=10, scar_key="burned_path")
    scar2 = MagicMock(expires_day=5, scar_key="warm_hearth")
    scar3 = MagicMock(expires_day=None, scar_key="sanctuary")

    active = get_active_scars_for_day([scar1, scar2, scar3], day_index=7)
    assert len(active) == 2  # scar1 и scar3 (scar2 истёк)
    assert scar1 in active
    assert scar3 in active


def test_blocked_places():
    from unittest.mock import MagicMock

    scar = MagicMock(scar_key="burned_path", metadata_json="мост")
    blocked = get_blocked_places([scar])
    assert "мост" in blocked


def test_tone_modifiers():
    from unittest.mock import MagicMock

    scar1 = MagicMock(scar_key="fresh_wound")
    scar2 = MagicMock(scar_key="gentle_breath")
    modifiers = get_tone_modifiers([scar1, scar2])
    assert "wound" in modifiers
    assert "warmth" in modifiers


def test_dynamic_places_no_scars():
    places = _get_dynamic_places(scar_keys=None)
    assert len(places) == len(_PLACES)


def test_dynamic_places_with_blocked():
    places = _get_dynamic_places(scar_keys={"burned_path"})
    # Мост с scar_key="burned_path" должен быть исключён
    for p in places:
        assert p.get("scar_key") != "burned_path"


def test_dynamic_places_with_unlocked():
    places = _get_dynamic_places(scar_keys={"warm_hearth"})
    # Должна добавиться разблокированная локация
    to_texts = [p["to"] for p in places]
    assert any("тёплый очаг" in t for t in to_texts)


def test_all_scar_rules_have_required_fields():
    for rule in SCAR_RULES:
        assert rule.trigger_tag in ("risk", "care", "cunning")
        assert rule.scar_key
        assert rule.min_streak >= 1
        assert rule.effect_type in ("block_place", "unlock_place", "modify_tone")
