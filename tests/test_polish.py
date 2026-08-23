"""Шлифовка: окно канона, устойчивость поста дня, живость офлайн-лора, эпилог дня."""

import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from app.broadcast import status_text
from app.config import settings
from app.handlers import _canon_text
from app.lore import compose_chapter
from app.models import RULE_PHRASES, Card, Round, RoundStatus, StoryBeat, WinRule


def _beat(day: int, text_len: int = 200) -> SimpleNamespace:
    return SimpleNamespace(
        day_index=day,
        winning_title=f"Путь дня {day}",
        winning_text="с" * text_len,
    )


def test_canon_keeps_newest_days_in_budget() -> None:
    beats = [_beat(day) for day in range(1, 31)]
    text, truncated = _canon_text(beats)
    assert truncated is True
    assert "День 30." in text
    assert "День 29." in text
    assert "День 1." not in text
    # Хронологический порядок сохранён внутри окна.
    positions = [text.index(f"День {day}.") for day in range(20, 31)]
    assert positions == sorted(positions)
    assert len(text) <= 3500 + len("Ранние дни растворились в шуме порталов.\n\n")


def test_canon_fits_all_when_short() -> None:
    beats = [_beat(day, text_len=40) for day in range(1, 6)]
    text, truncated = _canon_text(beats)
    assert truncated is False
    for day in range(1, 6):
        assert f"День {day}." in text


def test_status_text_survives_huge_cards() -> None:
    round_row = Round(
        day_index=3,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        rule_commitment="c",
        chapter_title="День 3. Испытание",
        chapter_text="Текст главы.",
        lore_summary="lore",
        opens_at=datetime.now(timezone.utc),
        voting_ends_at=datetime.now(timezone.utc) + timedelta(hours=23),
        tally_ends_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    cards = [
        Card(
            round_id=1,
            position=i,
            title=f"Карта {i}",
            description="д" * 500,
            consequence="Канон.",
            tag="care",
            image_path="",
        )
        for i in range(3)
    ]
    round_row.cards = cards
    text = status_text(round_row)
    # Фазовая строка и дедлайн не вытесняются длинными описаниями.
    assert "Закон дня:" in text
    assert "Голосование до:" in text
    for i in range(3):
        assert f"{['I', 'II', 'III'][i]}. Карта {i}" in text
        assert "д" * 281 not in text


def test_offline_lore_varies_between_days() -> None:
    """Фолбэк-лор не должен повторять одни и те же последствия изо дня в день."""
    seen_texts = set()
    seen_consequences = set()
    for day in range(2, 12):
        chapter = compose_chapter(day, ["Костёр стаи: появился общий костёр"], WinRule.MAJORITY)
        seen_texts.add(chapter["text"])
        for card in chapter["cards"]:
            seen_consequences.add(card["consequence"])
        assert len({card["title"] for card in chapter["cards"]}) == 3
        assert {card["tag"] for card in chapter["cards"]} == {"risk", "care", "cunning"}
    # За десять дней тексты глав и последствия различаются.
    assert len(seen_texts) >= 8
    assert len(seen_consequences) >= 12


def test_offline_backstory_of_yesterday_choice() -> None:
    """Глава продолжает историю стаи: вчерашний выбор назван, рассказ не пустой."""
    beat = "Костёр стаи: появился общий костёр проигравших дней."
    chapter = compose_chapter(4, [beat], WinRule.MINORITY)
    assert "«Костёр стаи»" in chapter["text"]
    assert "общий костёр проигравших дней" in chapter["text"]
    assert len(chapter["text"]) >= 350
    # Первый день — предыстории ещё нет.
    first = compose_chapter(1, [], WinRule.MAJORITY)
    assert "Вчера стая выбрала" not in first["text"]


def test_story_prompt_demands_full_narrative() -> None:
    from app.models import RULE_PHRASES
    from app.story import _build_story_prompt

    prompt = _build_story_prompt(
        5,
        ["Костёр стаи: появился общий костёр"],
        WinRule.MAJORITY,
    )
    assert "400-900" in prompt
    assert "новых главных персонажей не вводи" in prompt
    assert "предыстория" in prompt and "сцена сейчас" in prompt
    assert "до 140 знаков" in prompt  # лимит остаётся только для карт
    # Закон дня объявлен как известный факт.
    assert RULE_PHRASES[WinRule.MAJORITY] in prompt


def test_winner_photo_reuses_generated_file(tmp_path, monkeypatch) -> None:
    """Итоги получают фото победившей карты — без новой генерации."""
    from app.broadcast import winner_photo

    finished = Round(
        day_index=9,
        status=RoundStatus.CLOSED,
        win_rule=WinRule.MAJORITY,
        rule_commitment="c",
        chapter_title="t",
        chapter_text="text",
        lore_summary="lore",
        opens_at=datetime.now(timezone.utc),
        voting_ends_at=datetime.now(timezone.utc),
        tally_ends_at=datetime.now(timezone.utc),
        winner_card=2,
    )
    image = tmp_path / "day8_card2.jpg"
    image.write_bytes(b"\xff\xd8\xfffakejpeg")
    finished.cards = [
        Card(position=i, title=f"C{i}", description="d", consequence="Канон.",
             tag="care", image_path=str(image))
        for i in range(3)
    ]
    photo = winner_photo(finished)
    assert photo is not None

    # Файл пропал — рисуется локальный шаблон, а не падение.
    image.unlink()
    monkeypatch.setattr("app.broadcast.render_card", lambda p, t, d, pos: p.write_bytes(b"x"))
    photo = winner_photo(finished)
    assert photo is not None and image.exists()

    # Нет победителя — нет фото.
    finished.winner_card = None
    assert winner_photo(finished) is None


def _seed_beat(day: int) -> "StoryBeat":
    return StoryBeat(
        day_index=day,
        winning_title=f"Тропа {day}",
        winning_text=f"След дня {day}.",
        win_rule="majority",
        vote_counts="{}",
    )


async def test_previous_beats_window_is_capped(session) -> None:
    """Канон в промпте не растёт бесконечно — иначе через месяцы токены кончатся."""
    from app.rounds import previous_beats

    base = 500 + int.from_bytes(os.urandom(2), "big")
    session.add_all([_seed_beat(base + i) for i in range(20)])
    await session.commit()

    beats = await previous_beats(session)
    assert len(beats) == 12
    # Хронологический порядок, окно — последние дни.
    assert beats[0] == f"Тропа {base + 8}: След дня {base + 8}."
    assert beats[-1] == f"Тропа {base + 19}: След дня {base + 19}."


async def test_finish_tally_survives_missing_cards(session) -> None:
    """Отравленный день (карты потеряны) закрывается, а не роняет тик навсегда."""
    from app.models import RoundStatus
    from app.rounds import finish_tally

    round_row = Round(
        day_index=13,
        status=RoundStatus.TALLYING,
        win_rule=WinRule.MAJORITY,
        rule_commitment="c",
        chapter_title="t",
        chapter_text="text",
        lore_summary="lore",
        opens_at=datetime.now(timezone.utc),
        voting_ends_at=datetime.now(timezone.utc),
        tally_ends_at=datetime.now(timezone.utc),
    )
    session.add(round_row)
    # Пустая коллекция до коммита: иначе доступ к .cards в finish_tally
    # будит ленивую загрузку вне greenlet.
    round_row.cards = []
    await session.commit()

    finished, closed_here = await finish_tally(session, round_row)
    assert closed_here is True
    assert finished.winner_card == 0
    assert finished.status == RoundStatus.CLOSED
    beat = (
        await session.execute(select(StoryBeat).where(StoryBeat.day_index == 13))
    ).scalar_one()
    assert "Путь I" in beat.winning_title


async def test_create_next_round_dedupes_on_race(session, monkeypatch) -> None:
    """Гонка «планировщик против /advance»: второй создатель получает created=False."""
    from unittest.mock import AsyncMock

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.art_director import offline_bible
    from app import rounds as rounds_mod

    async def instant_chapter(day_index, beats, rule=None, echoes=None, distant_echoes=None, **kwargs):
        return compose_chapter(day_index, beats, rule, echoes)

    monkeypatch.setattr(rounds_mod, "generate_chapter", instant_chapter)
    monkeypatch.setattr(
        rounds_mod,
        "plan_day_art",
        AsyncMock(side_effect=lambda chapter, beats=None, anchor=None, extra_motifs=None: offline_bible(chapter)),
    )
    monkeypatch.setattr(rounds_mod, "fetch_day_image", AsyncMock(return_value=True))

    row_k, created_first = await rounds_mod.create_next_round_detailed(session)
    assert created_first is True
    # Rollback внутри второго создания истит объект — id нужен заранее.
    row_k_id = row_k.id

    # Конкурент (тик) успел вставить следующий день — сессия на том же движке.
    ghost_day = row_k.day_index + 1
    rival_maker = async_sessionmaker(session.bind, expire_on_commit=False, class_=AsyncSession)
    async with rival_maker() as rival:
        rival.add(
            Round(
                day_index=ghost_day,
                status=RoundStatus.OPEN,
                win_rule=WinRule.MAJORITY,
                rule_commitment="c",
                chapter_title="ghost",
                chapter_text="text",
                lore_summary="lore",
                opens_at=datetime.now(timezone.utc),
                voting_ends_at=datetime.now(timezone.utc),
                tally_ends_at=datetime.now(timezone.utc),
            )
        )
        await rival.commit()

    # Второй создатель ещё считает последним днём row_k — устаревшее чтение.
    real_latest = rounds_mod.get_latest_round
    calls = {"n": 0}

    async def racing_latest(s):
        calls["n"] += 1
        if calls["n"] == 1:
            return row_k
        return await real_latest(s)

    monkeypatch.setattr(rounds_mod, "get_latest_round", racing_latest)
    loser, created_second = await rounds_mod.create_next_round_detailed(session)
    assert created_second is False
    assert loser.id != row_k_id
    assert loser.day_index == ghost_day


async def test_results_message_appends_epilogue() -> None:
    from app.broadcast import results_message

    finished = SimpleNamespace(
        day_index=7,
        win_rule=WinRule.MAJORITY,
        winner_card=1,
        vote_counts_json='{"0": 2, "1": 9, "2": 4}',
        cards=[
            SimpleNamespace(position=i, title=f"Карта {i}", consequence="Канон.")
            for i in range(3)
        ],
        epilogue_text="Пёс запомнил эту тропу.",
    )
    text = await results_message(finished)
    assert "Итог дня 7" in text
    assert "Пёс запомнил эту тропу." in text
    finished.epilogue_text = ""
    assert "Пёс запомнил" not in await results_message(finished)


async def test_claim_announcement_is_exactly_once(session) -> None:
    """Двойной пост дня невозможен: второй претендент получает False."""
    from app.rounds import claim_announcement

    rnd = Round(
        day_index=41,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        rule_commitment="c",
        chapter_title="t",
        chapter_text="text",
        lore_summary="lore",
        opens_at=datetime.now(timezone.utc),
        voting_ends_at=datetime.now(timezone.utc) + timedelta(hours=23),
        tally_ends_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    session.add(rnd)
    await session.commit()
    try:
        assert await claim_announcement(session, rnd) is True
        assert await claim_announcement(session, rnd) is False
        # Читаем метку напрямую из БД, минуя identity map сессии.
        stamp = (
            await session.execute(select(Round.announced_at).where(Round.id == rnd.id))
        ).scalar_one()
        assert stamp is not None
    finally:
        await session.delete(rnd)
        await session.commit()


@pytest.mark.slow
async def test_reset_game_wipes_history_and_starts_day_one(session, monkeypatch) -> None:
    """Сброс стирает дни/голоса/ставки/очки, но хранит кошельки и копилку месяца."""
    monkeypatch.setattr(settings, "use_free_images", False)
    monkeypatch.setattr(settings, "use_free_story_llm", False)

    from app.models import LeaderboardPot, Payout, Player, Stake, StoryBeat, Vote
    from app.rounds import reset_game

    old_round = Round(
        day_index=5,
        status=RoundStatus.CLOSED,
        win_rule=WinRule.MAJORITY,
        rule_commitment="c",
        chapter_title="t",
        chapter_text="text",
        lore_summary="lore",
        opens_at=datetime.now(timezone.utc),
        voting_ends_at=datetime.now(timezone.utc),
        tally_ends_at=datetime.now(timezone.utc),
        winner_card=0,
        vote_counts_json='{"0": 3}',
    )
    session.add(old_round)
    await session.flush()
    player = Player(id=9201, username="vet", score=77, correct_picks=9,
                    wallet_address="EQ" + "b" * 46)
    session.add_all(
        [
            player,
            Vote(round_id=old_round.id, player_id=player.id, card_position=0),
            Stake(round_id=old_round.id, player_id=player.id, amount_nanotons=500_000_000,
                  tx_hash="reset-tx-1", status="confirmed", network=settings.ton_network),
            # status="sent": сброс стирает только ЗАКРЫТЫЕ обязательства;
            # висящий в очереди перевод блокировал бы сброс (см. test_guards).
            Payout(round_id=old_round.id, kind="rake", amount_nanotons=2_500_000,
                   dest_address="", network=settings.ton_network, status="sent"),
            StoryBeat(day_index=5, winning_title="t", winning_text="x",
                      win_rule="majority", vote_counts="{}"),
            LeaderboardPot(month="2030-01", nanotons=7_700_000_000),
        ]
    )
    await session.commit()

    fresh = await reset_game(session)
    try:
        assert fresh.day_index == 1
        assert fresh.status == RoundStatus.OPEN
        assert fresh.voting_ends_at > datetime.now(timezone.utc)
        # История стёрта (день 5 исчез; остался только новый день 1 —
        # SQLite переиспользует id после полной очистки таблицы).
        assert (
            await session.execute(select(Round).where(Round.day_index == 5))
        ).scalar_one_or_none() is None
        assert (await session.execute(select(Vote))).all() == []
        assert (await session.execute(select(Stake))).all() == []
        assert (await session.execute(select(Payout))).all() == []
        assert (await session.execute(select(StoryBeat))).all() == []
        # Счёт обнулён, кошелёк жив.
        kept = await session.get(Player, 9201)
        assert kept is not None and kept.score == 0 and kept.correct_picks == 0
        assert kept.wallet_address == "EQ" + "b" * 46
        # Деньги казны не тронуты.
        pot = (await session.execute(select(LeaderboardPot))).scalar_one()
        assert pot.nanotons == 7_700_000_000
    finally:
        for card in list(fresh.cards):
            await session.delete(card)
        await session.delete(fresh)
        await session.commit()


@pytest.mark.slow
async def test_reset_game_keep_story_preserves_canon(session, monkeypatch) -> None:
    """«Разделить команды»: счёты и деньги чисты, но канон и эхо живут."""
    monkeypatch.setattr(settings, "use_free_images", False)
    monkeypatch.setattr(settings, "use_free_story_llm", False)

    from app.models import LoreEcho, Player, StoryBeat
    from app.rounds import reset_game

    player = Player(id=9301, username="keeper", score=42)
    session.add_all(
        [
            player,
            StoryBeat(day_index=7, winning_title="Костёр стаи", winning_text="общий огонь",
                      win_rule="majority", vote_counts="{}"),
            LoreEcho(born_day=5, source_day=2, kind="risk", title="Ржавая миска",
                     description="мелькает у порталов", strength=3,
                     earliest_day=6, status="surfaced", surfaced_day=6),
        ]
    )
    await session.commit()

    fresh = await reset_game(session, keep_story=True)
    try:
        assert fresh.day_index == 1
        beat = (await session.execute(select(StoryBeat))).scalar_one()
        assert beat.winning_title == "Костёр стаи"
        echo = (await session.execute(select(LoreEcho))).scalar_one()
        assert echo.title == "Ржавая миска"
        kept = await session.get(Player, 9301)
        assert kept is not None and kept.score == 0
        # Новый день вырос из живого канона.
        assert "Костёр стаи" in fresh.chapter_text or "Костёр стаи" in (fresh.lore_summary or "")
    finally:
        await session.execute(sa_delete(StoryBeat))
        await session.execute(sa_delete(LoreEcho))
        await session.commit()


async def test_results_message_shows_day_economics(session) -> None:
    """Итоги показывают банк, явку, проценты путей, коэффициент и копилку."""
    from app.broadcast import results_message
    from app.config import settings
    from app.models import LeaderboardPot, Payout, Player, Stake, Vote

    rnd = Round(
        day_index=21,
        status=RoundStatus.CLOSED,
        win_rule=WinRule.MAJORITY,
        rule_commitment="c",
        chapter_title="t",
        chapter_text="text",
        lore_summary="lore",
        opens_at=datetime.now(timezone.utc),
        voting_ends_at=datetime.now(timezone.utc),
        tally_ends_at=datetime.now(timezone.utc),
        winner_card=1,
        vote_counts_json='{"0": 2, "1": 9, "2": 4}',
        pot_nanotons=3_000_000_000,
    )
    rnd.cards = [
        Card(position=i, title=f"C{i}", description="d", consequence="Канон.",
             tag="care", image_path="")
        for i in range(3)
    ]
    session.add(rnd)
    await session.flush()
    winner = Player(id=9101, username="win", score=0)
    loser = Player(id=9102, username="lose", score=0)
    session.add_all([winner, loser])
    await session.flush()
    net = settings.ton_network
    session.add_all(
        [
            Vote(round_id=rnd.id, player_id=winner.id, card_position=1),
            Vote(round_id=rnd.id, player_id=loser.id, card_position=0),
            Stake(round_id=rnd.id, player_id=winner.id, amount_nanotons=2_000_000_000,
                  tx_hash="eco-tx-1", status="confirmed", network=net),
            Stake(round_id=rnd.id, player_id=loser.id, amount_nanotons=1_000_000_000,
                  tx_hash="eco-tx-2", status="confirmed", network=net),
            Payout(round_id=rnd.id, player_id=winner.id, kind="prize",
                   amount_nanotons=5_800_000_000, dest_address="EQ" + "a" * 46, network=net),
            Payout(round_id=rnd.id, kind="bonus", amount_nanotons=60_000_000,
                   dest_address="", network=net),
            Payout(round_id=rnd.id, kind="leaderboard", amount_nanotons=20_000_000,
                   dest_address="", network=net),
            LeaderboardPot(month=rnd.tally_ends_at.strftime("%Y-%m"), nanotons=1_340_000_000),
        ]
    )
    await session.commit()

    text = await results_message(rnd, session)
    assert "Банк дня: 3.00 Gram · Играло: 15" in text
    assert "I 13% · II 60% · III 27%" in text
    assert "×2.90" in text
    assert "Угадавшим без ставки роздано: 0.06 Gram" in text
    assert "В копилку месяца ушло: 0.02 Gram" in text
    assert "всего в банке месяца: 1.34 Gram" in text


async def test_results_message_refund_day_has_no_multiplier(session) -> None:
    """Никто из поставивших не угадал — банк возвращён, коэффициента нет."""
    from app.broadcast import results_message
    from app.config import settings
    from app.models import Player, Stake, Vote

    rnd = Round(
        day_index=22,
        status=RoundStatus.CLOSED,
        win_rule=WinRule.MAJORITY,
        rule_commitment="c",
        chapter_title="t",
        chapter_text="text",
        lore_summary="lore",
        opens_at=datetime.now(timezone.utc),
        voting_ends_at=datetime.now(timezone.utc),
        tally_ends_at=datetime.now(timezone.utc),
        winner_card=0,
        vote_counts_json='{"0": 3}',
        pot_nanotons=1_000_000_000,
    )
    rnd.cards = [
        Card(position=i, title=f"C{i}", description="d", consequence="Канон.",
             tag="care", image_path="")
        for i in range(3)
    ]
    session.add(rnd)
    await session.flush()
    gambler = Player(id=9103, username="gambler", score=0)
    session.add(gambler)
    await session.flush()
    session.add_all(
        [
            Vote(round_id=rnd.id, player_id=gambler.id, card_position=0),
            Stake(round_id=rnd.id, player_id=gambler.id, amount_nanotons=1_000_000_000,
                  tx_hash="ref-tx-1", status="confirmed",
                  network=settings.ton_network),
        ]
    )
    await session.commit()

    text = await results_message(rnd, session)
    assert "Банк дня: 1.00 Gram" in text
    assert "возвращены" in text
    assert "×" not in text


async def test_write_epilogue_saves_once(session, monkeypatch) -> None:
    from app.models import RoundStatus
    from app.rounds import write_epilogue

    calls = []

    async def fake_epilogue(**kwargs) -> str:
        calls.append(kwargs)
        return "Тропа вздохнула и затвердела."

    monkeypatch.setattr("app.rounds.generate_epilogue", fake_epilogue)

    round_row = Round(
        day_index=11,
        status=RoundStatus.CLOSED,
        win_rule=WinRule.MAJORITY,
        rule_commitment="c",
        chapter_title="t",
        chapter_text="text",
        lore_summary="lore",
        opens_at=datetime.now(timezone.utc),
        voting_ends_at=datetime.now(timezone.utc),
        tally_ends_at=datetime.now(timezone.utc),
        winner_card=0,
        vote_counts_json='{"0": 5, "1": 2, "2": 3}',
    )
    # Карты цепляем до первого коммита: присваивание связи на сохранённом
    # объекте будит ленивую загрузку вне greenlet.
    round_row.cards = [
        Card(position=i, title=f"C{i}", description="d",
             image_path="", consequence="Канон.", tag="care")
        for i in range(3)
    ]
    session.add(round_row)
    await session.commit()

    text = await write_epilogue(session, round_row)
    assert text == "Тропа вздохнула и затвердела."
    # В промпт уходят расклад и закон.
    assert calls[0]["counts_line"].startswith("I — 5")
    assert calls[0]["rule_phrase"]
    # Идемпотентность: второй вызов генератор не будит.
    assert await write_epilogue(session, round_row) == text
    assert len(calls) == 1


async def test_generate_epilogue_parses_and_falls_back(monkeypatch) -> None:
    from app import story

    async def good(messages, timeout=45):
        return (
            {"choices": [{"message": {"content": "  Пёс вздохнул. Тропа остыла. "}}]},
            "model-x",
        )

    monkeypatch.setattr(story, "_chat_completion", good)
    text = await story.generate_epilogue(3, "T", "c", "I — 1", RULE_PHRASES[WinRule.MAJORITY])
    assert text == "Пёс вздохнул. Тропа остыла."

    async def silent(messages, timeout=45):
        return None

    monkeypatch.setattr(story, "_chat_completion", silent)
    assert await story.generate_epilogue(3, "T", "c", "", "") == ""

    async def broken(messages, timeout=45):
        return {"nope": True}, "m"

    monkeypatch.setattr(story, "_chat_completion", broken)
    assert await story.generate_epilogue(3, "T", "c", "", "") == ""


async def test_chapter_generation_retries_on_bad_payload(monkeypatch) -> None:
    from app import story

    good_content = json.dumps(
        {
            "title": "День 5. Испытание",
            "text": "История дня на 400 знаков и больше: ты стоишь у развилки...",
            "lore_summary": "s",
            "cover_prompt": "scene",
            "cards": [
                {"title": f"T{i}", "description": "d", "consequence": "c", "tag": "risk"}
                for i in range(3)
            ],
        },
        ensure_ascii=False,
    )

    async def flaky(messages, timeout=45):
        if flaky.calls == 0:
            flaky.calls = 1
            return {"choices": [{"message": {"content": "битый ответ без json"}}]}, "m1"
        flaky.calls += 1
        return {"choices": [{"message": {"content": good_content}}]}, "m2"

    flaky.calls = 0
    monkeypatch.setattr(story, "_chat_completion", flaky)
    chapter = await story._free_story_llm(5, [])
    assert chapter is not None
    assert len(chapter["cards"]) == 3
    assert flaky.calls == 2
