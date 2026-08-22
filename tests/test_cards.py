from pathlib import Path

from app.story import render_card


def test_local_card_render(tmp_path: Path) -> None:
    path = tmp_path / "card.png"
    render_card(path, "Ржавые ворота", "Пройти туда, где пахнет железом.", 0)
    assert path.exists()
    assert path.stat().st_size > 1000
