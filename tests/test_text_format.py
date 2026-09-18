"""Формат исходящего текста: теги не светятся сырыми там, где нет HTML."""
from app.handlers.player import _commands_help
from app.style import strip_html


def test_commands_help_uses_pack_narrative() -> None:
    text = "\n".join(_commands_help())
    assert "<b>Команды Стаи</b>" in text
    assert "караван" not in text.lower()


def test_strip_html_removes_tags_keeps_content() -> None:
    assert strip_html("Код: <code>bv:AB12CD</code> и <b>жирный</b>.") == (
        "Код: bv:AB12CD и жирный."
    )
    assert strip_html("без тегов") == "без тегов"


def test_strip_html_handles_multiline() -> None:
    text = "<b>Итог</b>\n<i>Следы</i>: <code>1</code>"
    assert strip_html(text) == "Итог\nСледы: 1"