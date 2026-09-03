"""Квиз памяти: детерминированный расклад, условная кнопка, всплытие."""


from app.echoes import build_memory_quiz, surfaced_echoes_for_round
from app.models import LoreEcho


async def test_surfaced_only_for_round(session) -> None:
    session.add_all(
        [
            LoreEcho(born_day=1, source_day=1, kind="память", title="A",
                     description="d", strength=3, earliest_day=5,
                     status="surfaced", surfaced_day=7),
            LoreEcho(born_day=2, source_day=2, kind="угроза", title="B",
                     description="d", strength=1, earliest_day=6,
                     status="surfaced", surfaced_day=8),
            LoreEcho(born_day=3, source_day=3, kind="обман", title="C",
                     description="d", strength=1, earliest_day=9,
                     status="dormant"),
        ]
    )
    await session.commit()
    titles = [echo.title for echo in await surfaced_echoes_for_round(session, 7)]
    assert titles == ["A"]


def test_quiz_is_deterministic_and_correct() -> None:
    first = build_memory_quiz(501, 33, ["Тёплые миски"], ["Старый приют", "Гулкий мост", "Портал у речки"])
    second = build_memory_quiz(501, 33, ["Тёплые миски"], ["Старый приют", "Гулкий мост", "Портал у речки"])
    assert first == second
    assert len(first["options"]) == 3
    assert first["true_title"] in first["options"]
    # Верный индекс действительно указывает на истину.
    for index in first["correct"]:
        assert first["options"][index] in ("Тёплые миски",)
    # Разные игроки/дни получают разный расклад хотя бы иногда.
    layouts = {
        tuple(build_memory_quiz(pid, 33, ["Истина"], ["Д1", "Д2", "Д3"])["options"])
        for pid in range(30)
    }
    assert len(layouts) > 1


def test_quiz_never_uses_decoy_equal_to_truth() -> None:
    quiz = build_memory_quiz(77, 10, ["Мостики"], ["Мостики", "Башня"])
    assert quiz["options"].count("Мостики") == 1
    assert "Башня" in quiz["options"]


def test_quiz_none_without_truth() -> None:
    assert build_memory_quiz(1, 1, [], ["Д1", "Д2"]) is None
    assert build_memory_quiz(1, 1, ["  "], []) is None


def test_keyboard_remember_is_conditional() -> None:
    from aiogram.utils.keyboard import InlineKeyboardBuilder  # noqa: F401

    from app.broadcast import cards_keyboard

    plain = cards_keyboard(5)
    marked = cards_keyboard(5, remember=True)
    plain_labels = [btn.text for row in plain.inline_keyboard for btn in row]
    marked_labels = [btn.text for row in marked.inline_keyboard for btn in row]
    assert not any("помню" in label.lower() for label in plain_labels)
    assert any("помню" in label.lower() for label in marked_labels)


def test_correct_memory_choice_uses_index_not_text() -> None:
    # Регресс-ловушка: handler раньше сверял текст варианта с множеством
    # индексов (всегда False) — верный ответ никогда не засчитывался,
    # из-за чего призвание «Жрец» и «+1 нюх» были недостижимы.
    from app.echoes import correct_memory_choice

    quiz = build_memory_quiz(501, 33, ["Тёплые миски"], ["Старый приют", "Гулкий мост"])
    for index in quiz["correct"]:
        assert correct_memory_choice(quiz, index) is True
    wrong = [i for i in range(len(quiz["options"])) if i not in quiz["correct"]]
    for index in wrong:
        assert correct_memory_choice(quiz, index) is False

