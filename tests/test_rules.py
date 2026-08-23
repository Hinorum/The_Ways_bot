from types import SimpleNamespace

from app.lore import compose_chapter
from app.models import WinRule
from app.rounds import finish_tally, pick_winner, tied_positions
from app.tally import format_results


def test_majority_minority_median() -> None:
    counts = {0: 10, 1: 50, 2: 30}
    assert pick_winner(counts, WinRule.MAJORITY) == 1
    assert pick_winner(counts, WinRule.MINORITY) == 0
    assert pick_winner(counts, WinRule.MEDIAN) == 2


def test_ties_are_deterministic() -> None:
    counts = {0: 7, 1: 7, 2: 3}
    assert pick_winner(counts, WinRule.MAJORITY) == 0
    assert pick_winner(counts, WinRule.MINORITY) == 2


def test_median_tie_candidates() -> None:
    """День пользователя: голоса 0-1-1 при законе медианы — претенденты II и III."""
    counts = {0: 0, 1: 1, 2: 1}
    assert tied_positions(counts, WinRule.MEDIAN) == [1, 2]
    # Без seed сохраняется прежнее детерминированное поведение.
    assert pick_winner(counts, WinRule.MEDIAN) == 1


def test_seeded_lot_is_fair_deterministic_and_limited_to_tied() -> None:
    """Жребий по обязательству: воспроизводим, ограничен ничейными путями,
    и разные обязательства действительно могут давать разных победителей."""
    counts = {0: 0, 1: 1, 2: 1}
    seen: set[int] = set()
    for n in range(40):
        seed = f"c{n % 10}:d1"
        winner = pick_winner(counts, WinRule.MEDIAN, seed=seed)
        assert winner in (1, 2)
        assert pick_winner(counts, WinRule.MEDIAN, seed=seed) == winner
        seen.add(winner)
    assert len(seen) == 2  # жребий не «прибит» к позиции


def test_results_text_explains_the_lot() -> None:
    round_row = SimpleNamespace(
        day_index=1,
        win_rule=WinRule.MEDIAN,
        winner_card=2,
        vote_counts_json='{"0": 0, "1": 1, "2": 1}',
        tie_note="Голоса разделились (II и III) — жребий закона по обязательству дня выбрал путь III.",
        cards=[
            SimpleNamespace(position=0, title="Сон вповалку", consequence="с0"),
            SimpleNamespace(position=1, title="Чужое имя", consequence="с1"),
            SimpleNamespace(position=2, title="Красный сигнал", consequence="с2"),
        ],
    )
    text = format_results(round_row)
    assert "🤝 Голоса разделились (II и III)" in text
    assert "жребий закона" in text


async def test_finish_tally_records_tie_note(session) -> None:
    from datetime import datetime, timedelta, timezone

    from app.models import Card, Round, RoundStatus

    now = datetime.now(timezone.utc)
    round_row = Round(
        day_index=9601,
        status=RoundStatus.TALLYING,
        win_rule=WinRule.MEDIAN,
        rule_commitment="ab12cd34",
        chapter_title="t",
        chapter_text="x",
        lore_summary="l",
        opens_at=now - timedelta(hours=25),
        voting_ends_at=now - timedelta(hours=1),
        tally_ends_at=now - timedelta(minutes=30),
        vote_counts_json="{}",
    )
    # Карты цепляем к транзиентному объекту: каскад расставит FK при flush.
    for position, title in enumerate(["A", "B", "C"]):
        round_row.cards.append(Card(position=position, title=title, description="д", image_path="", consequence="к"))
    session.add(round_row)
    await session.commit()
    from app.models import Vote

    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=1, card_position=1),
            Vote(round_id=round_row.id, player_id=2, card_position=2),
        ]
    )
    await session.commit()

    finished, closed = await finish_tally(session, round_row)
    assert closed is True
    assert finished.winner_card in (1, 2)
    assert finished.tie_note is not None
    assert "жребий" in finished.tie_note
    # Воспроизводимость: тот же обязательство+день дают того же победителя.
    expected = pick_winner({0: 0, 1: 1, 2: 1}, WinRule.MEDIAN, seed="ab12cd34:9601")
    assert finished.winner_card == expected


def test_story_references_previous_day() -> None:
    first = compose_chapter(1, [])
    assert len(first["cards"]) == 3
    beat = f"{first['cards'][0]['title']}: {first['cards'][0]['consequence']}"
    second = compose_chapter(2, [beat])
    assert first["cards"][0]["title"] in second["text"] or "След" in second["lore_summary"]
    assert len({card["title"] for card in second["cards"]}) == 3
