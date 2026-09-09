from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.story import _images_similar, render_card


@pytest.mark.slow
def test_local_card_render(tmp_path: Path) -> None:
    path = tmp_path / "card.png"
    render_card(path, "Ржавые ворота", "Пройти туда, где пахнет железом.", 0)
    assert path.exists()
    assert path.stat().st_size > 1000


def test_images_similar_detects_duplicates_and_differences(tmp_path: Path) -> None:
    def canvas(shift: int, color: tuple[int, int, int]) -> Path:
        img = Image.new("RGB", (64, 48), color)
        draw = ImageDraw.Draw(img)
        draw.rectangle((8 + shift, 6, 40 + shift, 36), fill=(255, 255, 255))
        path = tmp_path / f"img_{color[0]}_{shift}.png"
        img.save(path)
        return path

    a = canvas(0, (120, 60, 30))
    b = canvas(0, (122, 61, 31))  # тот же кадр, чуть другой шум кодирования
    c = canvas(20, (20, 40, 220))  # грубо другой кадр
    assert _images_similar(a, b)
    assert not _images_similar(a, c)


def test_images_similar_missing_file_is_safe(tmp_path: Path) -> None:
    exists = tmp_path / "exists.png"
    Image.new("RGB", (16, 12), (10, 10, 10)).save(exists)
    assert _images_similar(exists, tmp_path / "missing.png") is False
