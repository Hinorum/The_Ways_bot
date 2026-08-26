"""Вычитка живого дня №3: типографика, контроль длины, стилевые запреты.

Инцидент-уроки: ASCII-кавычки и «...» в проде; глава-конспект вдвое короче
контракта; закон дня цитировался как инструкция к интерфейсу; отголосок
вчера был мета-фразой; титул пролога показывал следующий день.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import settings
from app.models import Card, Round, RoundStatus, WinRule
from app.season import default_anchor, set_run_anchor_cache
from app.story import (
    _build_story_prompt,
    generate_chapter,
    polish_typography,
)


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
    # Офлайн-аварийный пол: 800 знаков (добор примет в lore).
    assert len(result["text"]) >= 800
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


def test_status_no_season_footer_for_non_crisis_days() -> None:
    """Футер сезона показывается только в последние 7 дней (кризис)."""
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
    # Футер сезона НЕ показывается для дней вне кризиса (первые N-7 дней).
    assert "Пролог дня:" not in text
    assert "До финала:" not in text
    assert "Пять собак" not in text
