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
        with_choices=True,
    )


def test_base_chapter_length() -> None:
    prompt = _prompt("СЕЗОН: акт 2.")
    assert "1000-1300 знаков" in prompt
    # Оба места: инструкция и JSON-схема.
    assert prompt.count("1000-1300") == 2


def test_expanded_in_prologue_and_midpoint() -> None:
    prologue = _prompt("Сезон: акт 1.\nПРОЛОГ, день 2 — «Дневник».", is_expanded=True)
    assert "1300-1600 знаков" in prologue and prologue.count("1300-1600") == 2
    midpoint = _prompt("Сезон: акт 2.\nПОВОРОТ СЕРЕДИНЫ: сегодня Хозяин Ошибки.", is_expanded=True)
    assert "1300-1600 знаков" in midpoint


def test_card_description_budget_unchanged() -> None:
    prompt = _prompt(None)
    assert "description (1-2 предложения)" in prompt


def test_prompt_block_budget_keeps_edges() -> None:
    block = "НАЧАЛО " + ("середина " * 80) + " СВЕЖИЙ_КОНТЕКСТ"
    compact = story._prompt_block(block, 120)
    assert len(compact) <= 160  # маркер сокращения добавляет служебную строку
    assert compact.startswith("НАЧАЛО")
    assert compact.endswith("СВЕЖИЙ_КОНТЕКСТ")
    assert "сокращена" in compact


def test_sniff_scene_appends_trail_tint() -> None:
    from app.callings import calling_by_key
    from app.handlers import compose_sniff_scene

    guardian = calling_by_key("guardian")
    plain = compose_sniff_scene("k:1", guardian, "Приют")
    tinted = compose_sniff_scene("k:1", guardian, "Приют",
                                 trail_tint="Твой След — «Пастух»: хор ведёт.")
    assert tinted.startswith(plain)
    assert "Пастух" in tinted
