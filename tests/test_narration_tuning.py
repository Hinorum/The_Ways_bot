"""Длина главы: пролог и поворот просят расширенный диапазон."""


from app import story


def _prompt(season_block: str | None, *, is_expanded: bool = False) -> str:
    return story._build_story_prompt(
        1,
        ["День 1. Прошлое: что-то было."],
        None,
        [],
        season_block=season_block,
        is_expanded=is_expanded,
    )


def test_base_chapter_length() -> None:
    prompt = _prompt("СЕЗОН: акт 2.")
    assert "1200-1500 знаков" in prompt
    # Оба места: инструкция и JSON-схема.
    assert prompt.count("1200-1500") == 2


def test_expanded_in_prologue_and_midpoint() -> None:
    prologue = _prompt("Сезон: акт 1.\nПРОЛОГ, день 2 — «Архивариус».", is_expanded=True)
    assert "1400-1700 знаков" in prologue and prologue.count("1400-1700") == 2
    midpoint = _prompt("Сезон: акт 2.\nПОВОРОТ СЕРЕДИНЫ: сегодня Хозяин Ошибки.", is_expanded=True)
    assert "1400-1700 знаков" in midpoint


def test_card_description_budget_unchanged() -> None:
    prompt = _prompt(None)
    assert "не больше 280 знаков" in prompt


def test_sniff_scene_appends_trail_tint() -> None:
    from app.callings import calling_by_key
    from app.handlers import compose_sniff_scene

    guardian = calling_by_key("guardian")
    plain = compose_sniff_scene("k:1", guardian, "Приют")
    tinted = compose_sniff_scene("k:1", guardian, "Приют",
                                 trail_tint="Твой След — «Пастух»: хор ведёт.")
    assert tinted.startswith(plain)
    assert "Пастух" in tinted
