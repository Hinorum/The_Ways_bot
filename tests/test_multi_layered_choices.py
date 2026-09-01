"""Тесты многослойных выборов."""

from __future__ import annotations

import pytest

from app.multi_layered_choices import (
    NPC_PARTNERS,
    MONTHLY_OATHS,
    get_available_partners,
    get_available_oaths,
    get_weekly_choice_text,
    get_monthly_choice_text,
    get_active_partner_bonuses,
    check_oath_completion,
)


def test_all_partners_have_keys():
    for key, partner in NPC_PARTNERS.items():
        assert partner.key == key
        assert partner.name
        assert partner.passive_bonus
        assert partner.active_ability


def test_all_oaths_have_keys():
    for key, oath in MONTHLY_OATHS.items():
        assert oath.key == key
        assert oath.name
        assert oath.requirement
        assert oath.reward
        assert oath.penalty


def test_weekly_choice_text():
    text = get_weekly_choice_text()
    assert "ГОЛОСОВАНИЕ НА НЕДЕЛЮ:" in text
    assert "Странник" in text
    assert "Страж" in text


def test_monthly_choice_text():
    text = get_monthly_choice_text()
    assert "ГОЛОСОВАНИЕ НА МЕСЯЦ:" in text
    assert "Клятва защиты" in text
    assert "Клятва истины" in text


def test_partner_bonuses_wanderer():
    bonuses = get_active_partner_bonuses("wanderer")
    assert bonuses.get("cunning") == 1


def test_partner_bonuses_guardian():
    bonuses = get_active_partner_bonuses("guardian")
    assert bonuses.get("care") == 1


def test_partner_bonuses_trickster():
    bonuses = get_active_partner_bonuses("trickster")
    assert bonuses.get("risk") == 1


def test_partner_bonuses_healer():
    bonuses = get_active_partner_bonuses("healer")
    assert bonuses.get("fatigue") == -1


def test_partner_bonuses_sage():
    bonuses = get_active_partner_bonuses("sage")
    assert bonuses.get("hope") == 1


def test_partner_bonuses_unknown():
    bonuses = get_active_partner_bonuses("unknown")
    assert bonuses == {}


def test_oath_completion_protect_weak():
    stats = {"care_streak": 7}
    assert check_oath_completion("protect_weak", stats) == True
    stats = {"care_streak": 5}
    assert check_oath_completion("protect_weak", stats) == False


def test_oath_completion_seek_truth():
    stats = {"cunning_streak": 5}
    assert check_oath_completion("seek_truth", stats) == True
    stats = {"cunning_streak": 3}
    assert check_oath_completion("seek_truth", stats) == False


def test_oath_completion_face_danger():
    stats = {"risk_streak": 6}
    assert check_oath_completion("face_danger", stats) == True
    stats = {"risk_streak": 4}
    assert check_oath_completion("face_danger", stats) == False


def test_oath_completion_balance_all():
    stats = {"risk_count": 3, "care_count": 2, "cunning_count": 2}
    assert check_oath_completion("balance_all", stats) == True
    stats = {"risk_count": 5, "care_count": 2, "cunning_count": 2}
    assert check_oath_completion("balance_all", stats) == False


def test_oath_completion_endure_all():
    stats = {"scars_endured": 3}
    assert check_oath_completion("endure_all", stats) == True
    stats = {"scars_endured": 2}
    assert check_oath_completion("endure_all", stats) == False


def test_oath_completion_unknown():
    assert check_oath_completion("unknown", {}) == False
