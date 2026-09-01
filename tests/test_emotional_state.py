"""Тесты эмоционального профиля стаи."""

from __future__ import annotations

import pytest

from app.emotional_state import (
    EmotionProfile,
    apply_emotion_shift,
    get_emotion_phase,
    get_emotion_tint,
    get_card_distribution_modifier,
    emotion_block_for_prompt,
    EMOTION_SHIFTS,
)


def test_emotion_profile_clamp():
    profile = EmotionProfile(fatigue=15, hope=-5, paranoia=12)
    profile.clamp()
    assert profile.fatigue == 10
    assert profile.hope == 0
    assert profile.paranoia == 10


def test_apply_risk_shift():
    profile = EmotionProfile(fatigue=5, hope=5, paranoia=5)
    apply_emotion_shift(profile, "risk")
    assert profile.fatigue == 6
    assert profile.hope == 4
    assert profile.paranoia == 5


def test_apply_care_shift():
    profile = EmotionProfile(fatigue=5, hope=5, paranoia=5)
    apply_emotion_shift(profile, "care")
    assert profile.fatigue == 4
    assert profile.hope == 6
    assert profile.paranoia == 5


def test_apply_cunning_shift():
    profile = EmotionProfile(fatigue=5, hope=5, paranoia=5)
    apply_emotion_shift(profile, "cunning")
    assert profile.fatigue == 5
    assert profile.hope == 4
    assert profile.paranoia == 6


def test_apply_unknown_tag():
    profile = EmotionProfile(fatigue=5, hope=5, paranoia=5)
    apply_emotion_shift(profile, "unknown")
    assert profile.fatigue == 5
    assert profile.hope == 5
    assert profile.paranoia == 5


def test_emotion_phase_exhausted():
    profile = EmotionProfile(fatigue=8, hope=3, paranoia=3)
    assert get_emotion_phase(profile) == "exhausted"


def test_emotion_phase_inspired():
    profile = EmotionProfile(fatigue=3, hope=8, paranoia=3)
    assert get_emotion_phase(profile) == "inspired"


def test_emotion_phase_suspicious():
    profile = EmotionProfile(fatigue=3, hope=3, paranoia=8)
    assert get_emotion_phase(profile) == "suspicious"


def test_emotion_phase_balanced():
    profile = EmotionProfile(fatigue=5, hope=5, paranoia=5)
    assert get_emotion_phase(profile) == "balanced"


def test_emotion_tint():
    profile = EmotionProfile(fatigue=8, hope=3, paranoia=3)
    tint = get_emotion_tint(profile)
    assert "устала" in tint


def test_card_distribution_exhausted():
    profile = EmotionProfile(fatigue=8, hope=3, paranoia=3)
    dist = get_card_distribution_modifier(profile)
    assert dist["care"] > dist["risk"]  # Больше care при усталости


def test_card_distribution_inspired():
    profile = EmotionProfile(fatigue=3, hope=8, paranoia=3)
    dist = get_card_distribution_modifier(profile)
    assert dist["risk"] > dist["care"]  # Больше risk при надежде


def test_card_distribution_suspicious():
    profile = EmotionProfile(fatigue=3, hope=3, paranoia=8)
    dist = get_card_distribution_modifier(profile)
    assert dist["cunning"] > dist["risk"]  # Больше cunning при паранойе


def test_card_distribution_balanced():
    profile = EmotionProfile(fatigue=5, hope=5, paranoia=5)
    dist = get_card_distribution_modifier(profile)
    # При сбалансированных эмоциях — примерно равные веса
    assert abs(dist["risk"] - dist["care"]) < 0.1


def test_emotion_block_balanced():
    profile = EmotionProfile(fatigue=5, hope=5, paranoia=5)
    block = emotion_block_for_prompt(profile)
    assert block is None  # Сбалансированные — без блока


def test_emotion_block_exhausted():
    profile = EmotionProfile(fatigue=8, hope=3, paranoia=3)
    block = emotion_block_for_prompt(profile)
    assert block is not None
    assert "УСТАЛОСТЬ" in block


def test_emotion_block_inspired():
    profile = EmotionProfile(fatigue=3, hope=8, paranoia=3)
    block = emotion_block_for_prompt(profile)
    assert block is not None
    assert "НАДЕЖДА" in block


def test_emotion_block_suspicious():
    profile = EmotionProfile(fatigue=3, hope=3, paranoia=8)
    block = emotion_block_for_prompt(profile)
    assert block is not None
    assert "ПАРАНОЙЯ" in block


def test_all_shifts_defined():
    for tag in ("risk", "care", "cunning"):
        assert tag in EMOTION_SHIFTS
        assert "fatigue" in EMOTION_SHIFTS[tag]
        assert "hope" in EMOTION_SHIFTS[tag]
        assert "paranoia" in EMOTION_SHIFTS[tag]
