"""Нрав стаи: две оси D&D — рандомный старт, дрейф от путей, во всём конвейере."""

from __future__ import annotations

from app.season import (
    AXIS_MAX,
    AXIS_MIN,
    alignment_block,
    alignment_finale_line,
    alignment_motifs,
    alignment_tints,
    apply_alignment_drift,
    default_anchor,
    roll_axes,
)


def test_start_position_is_fully_random() -> None:
    """Старт полностью случайный по диапазону: нейтраль возможна по рандому."""
    for _ in range(200):
        order, moral = roll_axes()
        assert AXIS_MIN <= order <= AXIS_MAX
        assert AXIS_MIN <= moral <= AXIS_MAX
    from datetime import datetime, timezone as _tz

    # Статистически: на 100 роллах ноль обязан выпасть хотя бы раз на ось
    # (вероятность пропуска ~ (4/5)^100 ≈ 2e-10).
    zeros = {axis: False for axis in ("order_axis", "moral_axis")}
    for _ in range(100):
        anchor = default_anchor(datetime(2026, 8, 24, tzinfo=_tz.utc))
        if anchor["order_axis"] == 0:
            zeros["order_axis"] = True
        if anchor["moral_axis"] == 0:
            zeros["moral_axis"] = True
    assert all(zeros.values())


def test_labels_cover_all_nine_combinations() -> None:
    from app.season import alignment_label as label

    assert label(2, 2) == "Законопослушная-добрая"
    assert label(-2, -2) == "Хаотичная-злая"
    assert label(0, -1) == "Нейтральная-злая"
    assert label(1, 0) == "Законопослушная-нейтральная"
    assert label(0, 0) == "Нейтральная стая"


def test_drift_rules_match_tags_and_clamp() -> None:
    # care → добро+порядок (мораль и порядок +1).
    anchor = {"order_axis": 1, "moral_axis": 1}
    o, m, changed = apply_alignment_drift(anchor, "care")
    assert (o, m) == (2, 2) and changed

    # риск → хаос: порядок −1, мораль ±1 (знак детерминирован сидом дня).
    anchor = {"order_axis": 1, "moral_axis": 1}
    o, m, changed = apply_alignment_drift(anchor, "risk", seed=7)
    assert o == 0 and m in (0, 2) and changed

    # cunning → расчёт+порядок (порядок +1, мораль −1).
    anchor = {"order_axis": 1, "moral_axis": 1}
    o, m, changed = apply_alignment_drift(anchor, "cunning")
    assert (o, m, changed) == (2, 0, True)

    # Потолок добра: мораль на AXIS_MAX не растёт (clamp), порядок — растёт.
    anchor = {"order_axis": 0, "moral_axis": AXIS_MAX}
    o, m, changed = apply_alignment_drift(anchor, "care")
    assert (o, m, changed) == (1, AXIS_MAX, True)

    # Клампы на краях: за край не выходит (порядок −2 + 1 = −1, мораль у верха −1).
    anchor = {"order_axis": AXIS_MIN, "moral_axis": AXIS_MAX}
    o, m, changed = apply_alignment_drift(anchor, "cunning")
    assert (o, m, changed) == (AXIS_MIN + 1, AXIS_MAX - 1, True)
    # Неизвестный тег ничего не двигает.
    anchor = {"order_axis": 1, "moral_axis": -1}
    assert apply_alignment_drift(anchor, "unknown") == ((1, -1), False)


def test_alignment_block_carries_directives_and_label() -> None:
    block = alignment_block(-2, -2)
    assert "Хаотичная-злая" in block
    assert "лазы" in block and "чёрный юмор" in block
    assert "смакования жестокости" in block  # граница тона задана явно
    good = alignment_block(2, 2)
    assert "по протоколу" in good and "жертвует" in good


def test_motifs_and_tints_follow_quadrant() -> None:
    motifs_evil = alignment_motifs(-1, -1)
    motifs_good = alignment_motifs(1, 1)
    assert motifs_evil != motifs_good
    assert len(alignment_tints(-2, 2, salt="s")) == 2  # обе оси ненулевые
    assert alignment_tints(0, 0, salt="s") == []  # нейтраль — без тинтов


def test_finale_line_uses_lowercased_label() -> None:
    line = alignment_finale_line(-1, -1)
    assert "хаотичная-злая" in line
    assert "Лай это запомнил" in line


def test_story_prompt_includes_alignment_block() -> None:
    from app.story import _build_story_prompt

    prompt = _build_story_prompt(
        5,
        ["Костёр стаи: появился общий костёр"],
        None,
        season_block="Сезон: акт 1.",
        alignment_block="НРАВ СТАИ — Хаотичная-злая. Тестовая директива.",
    )
    assert "НРАВ СТАИ — Хаотичная-злая" in prompt
    assert "Тестовая директива" in prompt


async def test_offline_chapter_appends_tint_sentences() -> None:
    from app.lore import compose_chapter
    from app.models import WinRule

    chapter = compose_chapter(
        5,
        ["Костёр стаи: появился общий костёр"],
        WinRule.MAJORITY,
        salt="tint",
        tint_lines=[
            "Правила здесь стареют быстрее собак.",
            "Выгода прежде всего: стая смотрит на чужие миски без совести.",
        ],
    )
    text = chapter["text"]
    assert "быстрее собак" in text
    assert "чужие миски" in text
