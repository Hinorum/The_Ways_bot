"""Тесты тайных комнат."""

from __future__ import annotations

import random
import pytest

from app.secret_rooms import (
    SECRET_ROOMS,
    check_secret_room_discovery,
    format_room_discovery,
    apply_room_effect,
    get_room_text_for_prompt,
)
from app.pack_state import PackNeeds


def test_all_rooms_have_keys():
    for key, room in SECRET_ROOMS.items():
        assert room.key == key
        assert room.name
        assert room.description
        assert 0 < room.discovery_chance <= 1


def test_check_discovery_no_scars():
    rng = random.Random(42)
    discovered = set()
    room = check_secret_room_discovery(rng, [], discovered)
    # Может вернуть комнату или None
    assert room is None or room.key in SECRET_ROOMS


def test_check_discovery_requires_scar():
    rng = random.Random(42)
    discovered = set()
    # collapsed_passage requires burned_path
    room = check_secret_room_discovery(
        rng,
        [],  # Нет шрамов
        discovered,
    )
    if room and room.key == "collapsed_passage":
        # Не должно happen без шрама
        assert False, "Room requiring scar discovered without scar"


def test_check_discovery_already_discovered():
    rng = random.Random(42)
    discovered = {"forgotten_cache", "wounded_dog", "echo_chamber",
                  "dark_tunnel", "ancient_inscription", "collapsed_passage",
                  "spirit_well", "memory_pool"}
    room = check_secret_room_discovery(rng, [], discovered)
    assert room is None


def test_format_room_discovery():
    room = SECRET_ROOMS["forgotten_cache"]
    text = format_room_discovery(room)
    assert "ТАЙНАЯ КОМНАТА" in text
    assert room.name in text


def test_apply_room_effect_hunger():
    needs = PackNeeds(hunger=8, thirst=5, health=10)
    effect = {"hunger": -3}
    result = apply_room_effect(effect, needs)
    assert needs.hunger == 5
    assert "голод" in result["text"]


def test_apply_room_effect_health():
    needs = PackNeeds(hunger=5, thirst=5, health=5)
    effect = {"health": +2}
    result = apply_room_effect(effect, needs)
    assert needs.health == 7


def test_apply_room_effect_clamp():
    needs = PackNeeds(hunger=9, thirst=5, health=10)
    effect = {"hunger": -5}
    result = apply_room_effect(effect, needs)
    assert needs.hunger == 4  # 9 - 5 = 4


def test_apply_room_effect_none():
    needs = PackNeeds(hunger=5, thirst=5, health=10)
    result = apply_room_effect(None, needs)
    assert "Ничего не изменилось" in result["text"]


def test_get_room_text_empty():
    text = get_room_text_for_prompt(set())
    assert text == ""


def test_get_room_text_with_rooms():
    text = get_room_text_for_prompt({"forgotten_cache", "wounded_dog"})
    assert "ОБНАРУЖЕННЫЕ" in text
    assert "Забытый тайник" in text


def test_discovery_chance_sum():
    total = sum(room.discovery_chance for room in SECRET_ROOMS.values())
    # Общий шанс не должен быть слишком высоким
    assert total < 2.0
