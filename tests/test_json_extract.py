"""Хрупкий парсинг JSON — главная причина срывов нейро-главы в офлайн.

Эти тесты фиксируют устойчивость _extract_json (и _clamp_sentence) к реальным
вывертам бесплатных LLM: текст вокруг JSON, скобка внутри строки и обрезка по
токенному лимиту с незакрытым объектом.
"""

from app.story import _clamp_sentence, _extract_json

import pytest


def test_extract_json_ignores_prose_around() -> None:
    content = 'Вот твой JSON, держи:\n{"title": "День 5", "text": "Стан", "cards": []}\nнадеюсь пригодится'
    assert _extract_json(content)["title"] == "День 5"


def test_extract_json_braces_inside_string() -> None:
    # rfind('}') по наивному срезу сломал бы это: скобка внутри строки.
    content = '{"text": "Он сказал: «} — и всё»", "cards": []}'
    data = _extract_json(content)
    assert data["text"] == "Он сказал: «} — и всё»"


def test_extract_json_repairs_truncated_object() -> None:
    # max_tokens срезал хвост: последнее поле закрыто, но сам объект не закрыт.
    content = '{"title": "День 5", "text": "История дня"'  # без «}» на конце
    data = _extract_json(content)
    assert data["title"] == "День 5"
    assert data["text"] == "История дня"


def test_extract_json_rejects_plain_text() -> None:
    with pytest.raises(ValueError):
        _extract_json("модель забыла JSON и написала просто прозу без фигурных скобок")


def test_clamp_sentence_cuts_on_punctuation() -> None:
    assert _clamp_sentence("One. Two. Three.", 7) == "One."
    assert _clamp_sentence("long string with no dot in it", 10) == "long strin…"


def test_clamp_sentence_leaves_short_untouched() -> None:
    assert _clamp_sentence("short", 50) == "short"
    assert _clamp_sentence("One. Two.", 50) == "One. Two."
