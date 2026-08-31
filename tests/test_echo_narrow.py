"""Эхо-сужение арены и серединный твист дня.

Механизмы из аудита вовлечения:
- Эхо-сужение (_narrowed_card): недавние выборы подкрашивают «горячий»
  архетип тёплым эхом, а «мёрзлый» — ловушкой Хозяина Ошибки.
- Серединный твист (_day_twist): редкий детерминированный сдвиг главы,
  чтобы день не читался предсказуемой формулой.
"""

from __future__ import annotations

from random import Random


from app.lore import CardDraft, _cards, _day_twist, _narrowed_card


def _draft(tag: str = "risk") -> CardDraft:
    return CardDraft("X", "desc", "cons", tag, "img")


# ---------- Эхо-сужение ----------


def test_hot_archetype_gets_warm_description() -> None:
    hot = ["risk", "risk", "risk", "care"]  # архетип встречался >=3 раз в окне
    narrowed = _narrowed_card(_draft("risk"), "risk", hot, day_index=5, salt="s")
    warm = ("уже знает эту тропу" in narrowed.description
            or "След знаком" in narrowed.description)
    assert warm
    assert narrowed.consequence == "cons"  # последствие не тронуто


def test_frozen_archetype_gets_villain_trap() -> None:
    frozen = ["care", "care", "care", "cunning"]  # risk не появлялся
    narrowed = _narrowed_card(_draft("risk"), "risk", frozen, day_index=5, salt="s")
    # Последствие продлено ловушкой (все три хвоста упоминают чужой след:
    # «чужая подпись» / «аккуратная метка» / «пересчитывает стаю»).
    assert narrowed.description == "desc"
    assert len(narrowed.consequence) > len("cons")
    assert any(k in narrowed.consequence for k in ("чуж", "метк", "пересчитывает"))


def test_balanced_archetype_untouched() -> None:
    mixed = ["risk", "care", "cunning"]  # нет ни горячего, ни мёрзлого
    original = _draft("risk")
    narrowed = _narrowed_card(original, "risk", mixed, day_index=5, salt="s")
    assert narrowed == original


def test_empty_history_untouched() -> None:
    original = _draft("risk")
    assert _narrowed_card(original, "risk", [], day_index=5, salt="s") == original


def test_narrow_deterministic() -> None:
    hot = ["risk"] * 3 + ["care"]
    a = _narrowed_card(_draft("risk"), "risk", hot, day_index=5, salt="s")
    b = _narrowed_card(_draft("risk"), "risk", hot, day_index=5, salt="s")
    assert a == b


# ---------- Серединный твист ----------


def test_midtwist_rare_and_deterministic() -> None:
    twist_days = {d for d in range(1, 201) if _day_twist(d, [], "salt")}
    assert 0 < len(twist_days) < 120  # редкий, но случается
    assert _day_twist(7, [], "salt") == _day_twist(7, [], "salt")


# ---------- Сквозная проверка карт без истории (обратная совместимость) ----------


def test_cards_without_history_returns_full_trio() -> None:
    rng = Random(1)
    cards = _cards(rng, day_index=1)
    assert len(cards) == 3
    assert {c.tag for c in cards} == {"risk", "care", "cunning"}
