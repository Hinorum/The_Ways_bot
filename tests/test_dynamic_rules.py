"""Тесты динамических правил."""

from __future__ import annotations

import pytest

from app.dynamic_rules import (
    RULE_OVERRIDES,
    get_active_overrides,
    apply_overrides,
    get_dynamic_rule_text,
    get_streak_bonus,
    get_streak_penalty,
)
from app.models import WorldScar, EmotionalState
from app.consequence_trees import ConsequenceBranch


def test_all_overrides_have_ids():
    for key, override in RULE_OVERRIDES.items():
        assert override.rule_id == key
        assert override.description


def test_get_active_overrides_empty():
    scars = []
    emotions = EmotionalState(fatigue=5, hope=5, paranoia=5)
    branches = []
    overrides = get_active_overrides(scars, emotions, branches, 10)
    assert len(overrides) == 0


def test_fatigue_override():
    scars = []
    emotions = EmotionalState(fatigue=8, hope=3, paranoia=3)
    branches = []
    overrides = get_active_overrides(scars, emotions, branches, 10)
    rule_ids = [o.rule_id for o in overrides]
    assert "fatigue_increase_care" in rule_ids


def test_paranoia_override():
    scars = []
    emotions = EmotionalState(fatigue=3, hope=3, paranoia=8)
    branches = []
    overrides = get_active_overrides(scars, emotions, branches, 10)
    rule_ids = [o.rule_id for o in overrides]
    assert "paranoia_increase_cunning" in rule_ids


def test_scar_override():
    scar = WorldScar(
        scar_key="test_scar",
        created_day=5,
        metadata_json="{}",
    )
    scars = [scar]
    emotions = EmotionalState(fatigue=5, hope=5, paranoia=5)
    branches = []
    overrides = get_active_overrides(scars, emotions, branches, 10)
    rule_ids = [o.rule_id for o in overrides]
    assert "scar_reduce_weight" in rule_ids


def test_debt_branch_override():
    scar = WorldScar(
        scar_key="test_scar",
        created_day=5,
        metadata_json="{}",
    )
    scars = [scar]
    emotions = EmotionalState(fatigue=5, hope=5, paranoia=5)
    branch = ConsequenceBranch(
        branch_key="foreign_pack_debt",
        current_stage=0,
        created_day=5,
        resolved=False,
    )
    branches = [branch]
    overrides = get_active_overrides(scars, emotions, branches, 8)
    rule_ids = [o.rule_id for o in overrides]
    assert "debt_threat_override" in rule_ids


def test_debt_branch_expired():
    scar = WorldScar(
        scar_key="test_scar",
        created_day=5,
        metadata_json="{}",
    )
    scars = [scar]
    emotions = EmotionalState(fatigue=5, hope=5, paranoia=5)
    branch = ConsequenceBranch(
        branch_key="foreign_pack_debt",
        current_stage=0,
        created_day=5,
        resolved=False,
    )
    branches = [branch]
    overrides = get_active_overrides(scars, emotions, branches, 15)
    rule_ids = [o.rule_id for o in overrides]
    assert "debt_threat_override" not in rule_ids


def test_apply_overrides():
    weights = {"risk": 0.33, "care": 0.33, "cunning": 0.34}
    override = RULE_OVERRIDES["fatigue_increase_care"]
    modified = apply_overrides(weights, [override])
    assert modified["care"] > 0.33


def test_apply_multiple_overrides():
    weights = {"risk": 0.33, "care": 0.33, "cunning": 0.34}
    override1 = RULE_OVERRIDES["fatigue_increase_care"]
    override2 = RULE_OVERRIDES["paranoia_increase_cunning"]
    modified = apply_overrides(weights, [override1, override2])
    assert modified["care"] > 0.33
    assert modified["cunning"] > 0.34


def test_get_dynamic_rule_text_empty():
    result = get_dynamic_rule_text([])
    assert result == ""


def test_get_dynamic_rule_text():
    override = RULE_OVERRIDES["fatigue_increase_care"]
    result = get_dynamic_rule_text([override])
    assert "ДИНАМИЧЕСКИЕ ПРАВИЛА:" in result
    assert override.description in result


def test_streak_bonus():
    assert get_streak_bonus(1) == 0.0
    assert get_streak_bonus(2) == 0.1
    assert get_streak_bonus(3) == 0.2
    assert get_streak_bonus(5) == 0.3


def test_streak_penalty():
    assert get_streak_penalty(1) == 0.0
    assert get_streak_penalty(2) == -0.1
    assert get_streak_penalty(3) == -0.2
    assert get_streak_penalty(5) == -0.3


def test_combined_fatigue_scar():
    scar = WorldScar(
        scar_key="test_scar",
        created_day=5,
        metadata_json="{}",
    )
    scars = [scar]
    emotions = EmotionalState(fatigue=7, hope=3, paranoia=3)
    branches = []
    overrides = get_active_overrides(scars, emotions, branches, 10)
    rule_ids = [o.rule_id for o in overrides]
    assert "exhaustion_combined" in rule_ids
