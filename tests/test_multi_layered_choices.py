"""Тесты многослойных выборов."""

from __future__ import annotations

import pytest

from app.multi_layered_choices import (
    NPC_PARTNERS,
    MONTHLY_OATHS,
    get_weekly_choice_text,
    get_monthly_choice_text,
    get_active_partner_bonuses,
    format_partner_block,
    format_oath_block,
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
    assert bonuses.get("health") == 1


def test_partner_bonuses_sage():
    bonuses = get_active_partner_bonuses("sage")
    assert bonuses.get("hope") == 1


def test_partner_bonuses_unknown():
    bonuses = get_active_partner_bonuses("unknown")
    assert bonuses == {}


def test_format_partner_block():
    block = format_partner_block("wanderer")
    assert "NPC-ПАРТНЁР" in block
    assert "Странник" in block


def test_format_partner_block_none():
    block = format_partner_block(None)
    assert block == ""


def test_format_oath_block():
    block = format_oath_block("protect_weak")
    assert "КЛЯТВА" in block
    assert "Клятва защиты" in block


def test_format_oath_block_none():
    block = format_oath_block(None)
    assert block == ""


def test_partner_tag_bonus_in_dict():
    for key, partner in NPC_PARTNERS.items():
        assert isinstance(partner.tag_bonus, dict)
        assert len(partner.tag_bonus) > 0
