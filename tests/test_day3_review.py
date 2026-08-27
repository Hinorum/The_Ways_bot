"""Вычитка живого дня №3: типографика, контроль длины, стилевые запреты.

Инцидент-уроки: ASCII-кавычки и «...» в проде; глава-конспект вдвое короче
контракта; закон дня цитировался как инструкция к интерфейсу; отголосок
вчера был мета-фразой; титул пролога показывал следующий день.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import settings
from app.models import Card, Round, RoundStatus, WinRule
from app.season import default_anchor, set_run_anchor_cache
from app.story import (
    _build_story_prompt,
    generate_chapter,
    polish_typography,
)
from app.broadcast import status_text


CHAPTER = {
    "title": "День 3. Почти даром",
    "text": "x" * 1500,
    "lore_summary": "л",
    "cover_prompt": "cover",
    "cards": [
        {"title": f"t{i}", "description": "d", "consequence": "c", "tag": tag}
        for i, tag in enumerate(("risk", "care", "cunning"))
    ],
}


def test_polish_typography_quotes_dash_ellipsis() -> None:
    raw = "'Считай, даром отдаю... почти даром', - говорит он."
    fixed = polish_typography(raw)
    assert "«Считай, даром отдаю… почти даром»" in fixed
    assert ", — говорит" in fixed
    assert "..." not in fixed


def test_polish_typography_keeps_apostrophes_inside_words() -> None:
    assert polish_typography("don't stop") == "don't stop"


async def test_short_neural_chapter_falls_back_offline(monkeypatch) -> None:
    """Глава-конспект (~1000 знаков) отклоняется — уходит офлайн-сборка."""
    monkeypatch.setattr(settings, "use_free_story_llm", True)

    def payload_for(text_len: int):
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"title":"К","place":"П","text":"' + "а" * text_len + '",'
                            '"lore_summary":"л","cover_prompt":"c",'
                            '"cards":[{"title":"a","description":"d","consequence":"c",'
                            '"tag":"risk"},{"title":"b","description":"d","consequence":"c",'
                            '"tag":"care"},{"title":"v","description":"d","consequence":"c",'
                            '"tag":"cunning"}]}'
                        )
                    }
                }
            ]
        }

    async def fake_chat(messages, timeout=40):
        return payload_for(300), "short-model"

    monkeypatch.setattr("app.story._chat_completion", fake_chat)
    result = await generate_chapter(3, ["вчера: след"], win_rule=None)
    # Офлайн-аварийный пол: 1000 знаков (добор примет в lore).
    assert len(result["text"]) >= 1000
    assert result["title"] != "К"

    long_call = {"count": 0}

    async def fake_chat_long(messages, timeout=40):
        long_call["count"] += 1
        return payload_for(2000), "long-model"

    monkeypatch.setattr("app.story._chat_completion", fake_chat_long)
    result = await generate_chapter(4, ["вчера: след"], win_rule=None)
    assert long_call["count"] >= 1 and len(result["text"]) >= 1200


def test_prompt_bans_law_formula_and_meta_echo() -> None:
    from app.models import WinRule

    prompt = _build_story_prompt(
        3,
        ["вчера стая взломала ржавые ворота"],
        win_rule=WinRule.MAJORITY,
    )
    # Образный голос Архивариуса вместо дословной механики.
    assert "ЗАПРЕЩЕНО цитировать формулировку дословно" in prompt
    # Отголосок — конкретная деталь, не мета-фраза.
    assert "КОНКРЕТНУЮ деталь" in prompt and "напоминает о" in prompt
    # Разнообразие начал карт.
    assert "первое слово и конструкция каждой — свои" in prompt
    # Прологовый фокус: одно лицо, прочие фоном.
    assert "одно вводимое лицо" in prompt
    # Закат больше не изобретается миром без солнца.
    assert "до заката" not in prompt and "темноты сети" in prompt


def test_status_prologue_title_matches_open_day() -> None:
    """Титул пролога в статусе = день ОТКРЫТИЯ, а не завтрашний момент."""
    from app.broadcast import status_text

    opens = datetime(2026, 8, 24, 11, 0, tzinfo=timezone.utc)  # далеко от границ месяца
    anchor = default_anchor(opens - timedelta(days=2))  # сегодня — день 3 забега
    set_run_anchor_cache(anchor)

    round_row = Round(
        id=90_500,
        day_index=3,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MEDIAN,
        rule_commitment="c:s",
        chapter_title="День 3",
        chapter_text="т",
        lore_summary="л",
        cover_path="",
        opens_at=opens,
        voting_ends_at=opens + timedelta(hours=23),
        tally_ends_at=opens + timedelta(hours=23),
    )
    for position in range(3):
        round_row.cards.append(
            Card(position=position, title=f"t{position}", description="d", consequence="c")
        )
    text = status_text(round_row)
    assert "Пролог дня: «Лайнер»" in text
    assert "Пять собак" not in text


# ---------- Объединение поста дня (жалоба на дубль заголовка) ----------


def _round_with_story(day_index: int, tmp_path, story_len: int):
    from app.broadcast import build_day_post

    cover = Path(tmp_path) / f"day{day_index}_cover.jpg"
    cover.write_bytes(b"\xff\xd8\xfffakejpeg")
    round_row = Round(
        id=90_600 + day_index,
        day_index=day_index,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        rule_commitment="c",
        chapter_title=f"День {day_index}. Портал лает",
        chapter_text="Утро началось с чужого эха в мисках." * max(1, story_len // 40),
        lore_summary="л",
        cover_path=str(cover),
        opens_at=datetime.now(timezone.utc),
        voting_ends_at=datetime.now(timezone.utc) + timedelta(hours=23),
        tally_ends_at=datetime.now(timezone.utc) + timedelta(hours=23),
    )
    round_row.cards.append(
        Card(position=0, title="Тропа", description="d", consequence="c", image_path="")
    )
    return round_row, build_day_post


def test_short_story_merges_into_cover_caption(tmp_path) -> None:
    round_row, build = _round_with_story(1, tmp_path, 60)
    media, story_in_caption = build(round_row)
    assert len(media) == 1
    assert story_in_caption is True
    # Заголовок и история в одном посте — дубля нет.
    assert "Портал лает" in media[0].caption
    assert "Утро началось с чужого эха" in media[0].caption
    assert len(media[0].caption) <= 1024

    status = status_text(
        round_row,
        show_title=not story_in_caption,
        include_story=not story_in_caption,
    )
    assert "День 1. Портал лает" not in status  # дубля заголовка нет
    assert "Утро началось с чужого эха" not in status  # история не повторяется
    assert "I. Тропа" in status  # развилка и служебные строки на месте


def test_long_story_keeps_separate_messages(tmp_path) -> None:
    round_row, build = _round_with_story(2, tmp_path, 1500)
    media, story_in_caption = build(round_row)
    assert story_in_caption is False  # глава длиннее подписи
    assert "Портал лает" in media[0].caption and "стая идёт" not in media[0].caption
    status = status_text(round_row, show_title=not story_in_caption)
    # Титул ровно один раз (на обложке), история целиком в тексте.
    assert media[0].caption.count("Портал лает") == 1
    assert status.count("Портал лает") == 1
    assert len(status) <= 4096


# ---------- Тихий пролог: день 1 без Лая и без закона ----------


def test_prologue_day1_quiet_no_bark_no_law() -> None:
    from app.lore import compose_chapter

    block = (
        "ПРОЛОГ, день 1 знакомства — «Приход». Новая арка начинается с "
        "воспоминания о Последнем Пути."
    )
    chapter = compose_chapter(
        1,
        [],
        win_rule=WinRule.MEDIAN,
        season_block=block,
        salt="quiet-prologue",
    )
    assert "Первый Лай" not in chapter["text"]
    assert "Середина знает меру" not in chapter["text"]
    assert "Закон" not in chapter["text"].split("Одна карта на всех")[0]
    assert "Одна карта на всех." in chapter["text"]
