"""Тесты системы стриков и титулов прогрессии: чистая логика app.streaks.

update_streak мутирует только объект игрока — сессия БД ему не нужна,
поэтому тесты обходятся без сети и без БД.
"""

from __future__ import annotations

import pytest

from app.models import Player
from app.streaks import (
    next_title,
    remaining_word,
    streak_text,
    title_for_streak,
    update_streak,
)


@pytest.mark.asyncio
async def test_update_streak_increments_and_keeps_best():
    player = Player(current_streak=0, best_streak=0)
    assert player.current_streak == 0
    assert player.best_streak == 0

    for _ in range(3):
        await update_streak(None, player, was_correct=True)

    assert player.current_streak == 3
    assert player.best_streak == 3


@pytest.mark.asyncio
async def test_update_streak_resets_on_wrong_pick_but_keeps_best():
    player = Player()
    player.current_streak = 7
    player.best_streak = 7

    await update_streak(None, player, was_correct=False)

    assert player.current_streak == 0
    assert player.best_streak == 7


@pytest.mark.asyncio
async def test_update_streak_best_tracks_deeper_series():
    player = Player()
    player.current_streak = 5
    player.best_streak = 5

    await update_streak(None, player, was_correct=True)

    assert player.current_streak == 6
    assert player.best_streak == 6


def test_title_for_streak_thresholds():
    assert title_for_streak(0).key == "novice"
    assert title_for_streak(2).key == "novice"
    assert title_for_streak(3).key == "tracking"
    assert title_for_streak(4).key == "tracking"
    assert title_for_streak(5).key == "scout"
    assert title_for_streak(6).key == "scout"
    assert title_for_streak(7).key == "ranger"
    assert title_for_streak(49).key == "legend"
    assert title_for_streak(50).key == "prophet"
    assert title_for_streak(1000).key == "prophet"


def test_next_title_boundaries():
    assert next_title(0).key == "tracking"
    assert next_title(2).key == "tracking"
    assert next_title(3).key == "scout"
    assert next_title(49).key == "prophet"
    assert next_title(50) is None


def test_streak_text_with_active_series():
    player = Player(current_streak=4, best_streak=6)
    text = streak_text(player)
    assert "Следопыт" in text
    assert "Серия верных путей: 4" in text
    assert "Лучшая: 6" in text
    assert "Разведчик" in text


def test_streak_text_zero_series_shows_best_only():
    player = Player(current_streak=0, best_streak=2)
    text = streak_text(player)
    assert "Серия верных путей:" not in text
    assert "Лучшая серия: 2" in text
    assert "🐾" in text


def test_streak_text_at_max_title():
    player = Player(current_streak=50, best_streak=50)
    text = streak_text(player)
    assert "достиг вершины" in text


@pytest.mark.parametrize(
    "n,expected",
    [
        (1, "путь"),
        (21, "путь"),
        (101, "путь"),
        (2, "пути"),
        (3, "пути"),
        (4, "пути"),
        (22, "пути"),
        (0, "путей"),
        (5, "путей"),
        (11, "путей"),
        (12, "путей"),
        (13, "путей"),
        (14, "путей"),
        (25, "путей"),
    ],
)
def test_remaining_word_pluralization(n: int, expected: str):
    assert remaining_word(n) == expected