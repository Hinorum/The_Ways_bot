"""Дальняя память мира: хэш-эмбеддинги, recall и вплетение давнего канона."""

from app.lore import compose_chapter
from app.memory import cosine, embed, recall_beats, similarity
from app.story import _build_story_prompt


def test_embed_is_deterministic_and_normalized() -> None:
    first = embed("Стая нашла волчью тропу у старого портала")
    second = embed("Стая нашла волчью тропу у старого портала")
    assert first == second
    norm = sum(x * x for x in first) ** 0.5
    assert abs(norm - 1.0) < 1e-6
    # Пустой текст — нулевой вектор без падений.
    assert all(x == 0.0 for x in embed("!!!"))


def test_similarity_orders_topics() -> None:
    query = "волк вышел на тропу стаи"
    close = "Огромный волк следил за стаей из тумана и ушёл к порталу"
    far = "Миски наполнились тёплой похлёбкой, стая легла спать"
    assert similarity(query, close) > similarity(query, far)


def test_recall_finds_old_wolf_day_and_respects_window() -> None:
    beats = []
    for day in range(1, 21):
        if day == 4:
            text = "Волчья стая: серый вожак перекрыл старый перевал и смотрел из тьмы"
        elif day == 18:
            text = "Речная переправа: стая перешла холодный брод по грудь"
        else:
            text = f"Обычный день: стая шла вперёд и кормилась у портала номер {day}"
        beats.append(f"День {day}. {text}")

    recalled = recall_beats(beats, query="волк на перевале", k=3)
    assert len(recalled) <= 3
    assert any("волч" in beat.lower() or "перевал" in beat.lower() for beat in recalled)
    # Свежее окно не участвует: день 18 не всплывает как «давний».
    assert all(not beat.startswith("День 18.") for beat in recalled)

    # Мало канона — вспоминать нечего.
    assert recall_beats(beats[:10], query="волк") == []
    assert recall_beats(beats, query="   ") == []


def test_compose_chapter_weaves_distant_canon() -> None:
    distant = ["День 4. Волчья стая: серый вожак перекрыл перевал."]
    chapter = compose_chapter(15, ["Прошлый путь: стая ушла к воде"], distant_echoes=distant)
    assert "давнее" in chapter["text"]
    assert "Волчья стая" in chapter["text"]
    # Пустой список ничего не ломает и ничего не добавляет.
    plain = compose_chapter(15, ["Прошлый путь: стая ушла к воде"], distant_echoes=[])
    assert "давнее" not in plain["text"]


def test_story_prompt_carries_distant_block() -> None:
    prompt = _build_story_prompt(
        15,
        ["Прошлый путь: стая ушла к воде"],
        distant_echoes=["День 4. Волчья стая: серый вожак перекрыл перевал."],
    )
    assert "Давний канон" in prompt
    assert "перевал" in prompt
