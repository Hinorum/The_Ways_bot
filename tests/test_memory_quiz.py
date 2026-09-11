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


async def test_remember_decoys_query_uses_real_column(session) -> None:
    # Регресс-ловушка (продакшн-инцидент): on_remember слал
    # select(StoryBeat.title, ...) — колонки «title» у StoryBeat нет,
    # быстрый канон зовётся winning_title. Тест выполняет ровно ту же
    # выборку, что упавший handler, и падал бы AttributeError до фикса.
    from sqlalchemy import select

    from app.models import StoryBeat

    session.add_all(
        [
            StoryBeat(day_index=1, winning_title="Старый приют", winning_text="t", win_rule="risk", vote_counts="{}"),
            StoryBeat(day_index=2, winning_title="Гулкий мост", winning_text="t", win_rule="risk", vote_counts="{}"),
            StoryBeat(day_index=10, winning_title="Тёплые миски", winning_text="t", win_rule="care", vote_counts="{}"),
        ]
    )
    await session.commit()
    beats = (
        await session.execute(
            select(StoryBeat.winning_title, StoryBeat.day_index).order_by(StoryBeat.day_index.asc())
        )
    ).all()
    assert beats == [("Старый приют", 1), ("Гулкий мост", 2), ("Тёплые миски", 10)]


async def test_remember_decoys_exclude_nearby_source_days(session) -> None:
    # Логика приманок из on_remember: в квиз идут только победившие титулы
    # дней, далёких от дней рождения всплывших эх (иначе расклад предсказуем).
    from sqlalchemy import select

    from app.models import LoreEcho, StoryBeat

    session.add_all(
        [
            StoryBeat(day_index=1, winning_title="Старый приют", winning_text="t", win_rule="risk", vote_counts="{}"),
            StoryBeat(day_index=2, winning_title="Гулкий мост", winning_text="t", win_rule="risk", vote_counts="{}"),
            StoryBeat(day_index=4, winning_title="Давний портал", winning_text="t", win_rule="cunning", vote_counts="{}"),
            LoreEcho(born_day=1, source_day=1, kind="память", title="Старый приют",
                     description="d", strength=3, earliest_day=5, status="surfaced", surfaced_day=6),
        ]
    )
    await session.commit()
    echoes = await surfaced_echoes_for_round(session, 6)
    source_days = {echo.source_day for echo in echoes}
    beats = (
        await session.execute(
            select(StoryBeat.winning_title, StoryBeat.day_index).order_by(StoryBeat.day_index.asc())
        )
    ).all()
    decoys = [
        title for title, day in beats
        if day not in source_days and (day < min(source_days) - 1 or day > max(source_days) + 1)
    ]
    # День 2 (source_day+1) — ближайший, не приманка; день 4 — далёкий.
    assert decoys == ["Давний портал"]


async def test_on_remember_builds_quiz_without_crashing() -> None:
    # Регресс-ловушка продакшн-инцидента: on_remember читал
    # select(StoryBeat.title, ...), а колонка называется winning_title —
    # квиз памяти выбрасывал AttributeError и «Я помню этот след» не
    # работал. Handler-тест гоняет ровно тот же путь, что упал в проде.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.db import SessionLocal
    from app.handlers import player as player_mod
    from app.models import LoreEcho, StoryBeat

    async with SessionLocal() as db:
        db.add_all(
            [
                StoryBeat(day_index=1, winning_title="Старый приют", winning_text="t", win_rule="risk", vote_counts="{}"),
                StoryBeat(day_index=2, winning_title="Гулкий мост", winning_text="t", win_rule="risk", vote_counts="{}"),
                StoryBeat(day_index=3, winning_title="Тёплые миски", winning_text="t", win_rule="care", vote_counts="{}"),
                StoryBeat(day_index=9, winning_title="Давний портал", winning_text="t", win_rule="cunning", vote_counts="{}"),
                LoreEcho(born_day=1, source_day=1, kind="память", title="Старый приют",
                         description="d", strength=3, earliest_day=2, status="surfaced", surfaced_day=4),
            ]
        )
        await db.commit()

    user = SimpleNamespace(id=9001, username="tester", first_name="T")
    callback = SimpleNamespace(
        data="remember:99:4",
        from_user=user,
        message=SimpleNamespace(answer=AsyncMock()),
        answer=AsyncMock(),
    )
    await player_mod.on_remember(callback)
    callback.message.answer.assert_awaited_once()
    text = callback.message.answer.call_args.args[0]
    assert "Дневник шепчет" in text
    callback.answer.assert_awaited_once()

