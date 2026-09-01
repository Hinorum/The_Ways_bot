"""Тесты деревьев последствий."""

from __future__ import annotations

import pytest

from app.consequence_trees import (
    CONSEQUENCE_TREES,
    check_tree_trigger,
    get_stage_text,
    get_stage_choices,
    should_advance,
    get_delay,
    format_active_branches,
)


def test_tree_trigger_found():
    tree = check_tree_trigger("Клыки на кости")
    assert tree is not None
    assert tree.key == "foreign_pack_debt"


def test_tree_trigger_not_found():
    tree = check_tree_trigger("Неизвестная карта")
    assert tree is None


def test_stage_text():
    tree = CONSEQUENCE_TREES["foreign_pack_debt"]
    text = get_stage_text(tree, 0)
    assert "Чужая стая" in text


def test_stage_text_invalid():
    tree = CONSEQUENCE_TREES["foreign_pack_debt"]
    text = get_stage_text(tree, 99)
    assert text == ""


def test_stage_choices():
    tree = CONSEQUENCE_TREES["foreign_pack_debt"]
    choices = get_stage_choices(tree, 1)  # Стадия 1 имеет выбор
    assert choices is not None
    assert "care" in choices
    assert "risk" in choices


def test_stage_choices_none():
    tree = CONSEQUENCE_TREES["foreign_pack_debt"]
    choices = get_stage_choices(tree, 0)  # Стадия 0 без выбора
    assert choices is None


def test_should_advance():
    tree = CONSEQUENCE_TREES["foreign_pack_debt"]
    assert should_advance(tree, 0) == True  # Авто-переход
    assert should_advance(tree, 1) == False  # Ожидает выбор


def test_get_delay():
    tree = CONSEQUENCE_TREES["foreign_pack_debt"]
    assert get_delay(tree, 0) == 5  # 5 дней задержки
    assert get_delay(tree, 1) == 0  # Без задержки


def test_format_empty_branches():
    result = format_active_branches([])
    assert result == ""


def test_format_active_branches():
    from unittest.mock import MagicMock

    branch = MagicMock(branch_key="foreign_pack_debt", current_stage=0)
    result = format_active_branches([branch])
    assert "АКТИВНЫЕ ПОСЛЕДСТВИЯ:" in result
    assert "Чужая стая" in result


def test_all_trees_have_stages():
    for key, tree in CONSEQUENCE_TREES.items():
        assert len(tree.stages) > 0, f"Дерево {key} не имеет стадий"


def test_all_trees_have_trigger():
    for key, tree in CONSEQUENCE_TREES.items():
        assert tree.trigger_card, f"Дерево {key} не имеет триггера"


def test_stage_counts():
    assert len(CONSEQUENCE_TREES["foreign_pack_debt"].stages) == 3
    assert len(CONSEQUENCE_TREES["burned_bridge"].stages) == 3
    assert len(CONSEQUENCE_TREES["stolen_food"].stages) == 3
    assert len(CONSEQUENCE_TREES["labyrinth_doubt"].stages) == 3
    assert len(CONSEQUENCE_TREES["warm_hearth"].stages) == 2
