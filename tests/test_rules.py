from app.lore import compose_chapter
from app.models import WinRule
from app.rounds import pick_winner


def test_majority_minority_median() -> None:
    counts = {0: 10, 1: 50, 2: 30}
    assert pick_winner(counts, WinRule.MAJORITY) == 1
    assert pick_winner(counts, WinRule.MINORITY) == 0
    assert pick_winner(counts, WinRule.MEDIAN) == 2


def test_ties_are_deterministic() -> None:
    counts = {0: 7, 1: 7, 2: 3}
    assert pick_winner(counts, WinRule.MAJORITY) == 0
    assert pick_winner(counts, WinRule.MINORITY) == 2


def test_story_references_previous_day() -> None:
    first = compose_chapter(1, [])
    assert len(first["cards"]) == 3
    beat = f"{first['cards'][0]['title']}: {first['cards'][0]['consequence']}"
    second = compose_chapter(2, [beat])
    assert first["cards"][0]["title"] in second["text"] or "След" in second["lore_summary"]
    assert len({card["title"] for card in second["cards"]}) == 3
