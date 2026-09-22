"""Round-trip кассетного инструмента: dump → правка → compile.

Гарантии:
- YAML-сценарий — полноправный редакционный формат хранителя: каждая
  кассета библиотеки разворачивается в YAML и собирается обратно в ту же
  модель (JSON на выходе нормализуется — авто-дефолты дописываются).
- Компилятор проходит тот же `validate_payload`, что движок: кривой сценарий
  отклоняется с локациями, без записи файла.
"""

from pathlib import Path

import yaml

from app.story.schema import validate_file
from scripts import cassette_tool

_CASSETTES_DIR = Path(__file__).resolve().parent.parent / "app" / "story" / "cassettes"
SHIPPED = sorted(_CASSETTES_DIR.glob("*.json"))


def _run(*argv: str) -> int:
    return cassette_tool.run(list(argv))


def _dump_yaml(tmp_path, cassette_path: Path) -> Path:
    out = tmp_path / "scenario.yaml"
    assert _run("dump", str(cassette_path), "-o", str(out)) == 0
    return out


def test_runtime_has_yaml() -> None:
    """Инструмент опирается на PyYAML: обязательный dep в requirements."""
    assert yaml.safe_load("a: 1") == {"a": 1}


def test_roundtrip_all_shipped_cassettes(tmp_path) -> None:
    assert SHIPPED, "в библиотеке должны быть кассеты-образцы"
    for path in SHIPPED:
        yaml_path = _dump_yaml(tmp_path, path)
        json_path = tmp_path / (path.stem + ".json")
        assert _run("compile", str(yaml_path), "-o", str(json_path)) == 0
        original = validate_file(path).cassette
        compiled = validate_file(json_path).cassette
        assert original is not None and compiled is not None
        assert compiled.model_dump() == original.model_dump()


def test_edit_card_and_chapter_survives_compile(tmp_path) -> None:
    """Правка одного дня/варианта: compile записывает её в кассету, остальное цело."""
    src = SHIPPED[0]
    yaml_path = _dump_yaml(tmp_path, src)
    payload = yaml.safe_load(yaml_path.read_text(encoding="utf-8-sig"))
    payload["days"][0]["chapter_title"] = "Правка: новая глава"
    payload["days"][0]["cards"][1]["title"] = "Правка: новый ход"
    yaml_path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    json_path = tmp_path / "edited.json"
    assert _run("compile", str(yaml_path), "-o", str(json_path)) == 0

    edited = validate_file(json_path).cassette
    original = validate_file(src).cassette
    assert edited is not None and original is not None
    assert edited.days[0].chapter_title == "Правка: новая глава"
    assert edited.days[0].cards[1].title == "Правка: новый ход"
    assert edited.cassette_id == original.cassette_id
    assert [day.day_index for day in edited.days] == [day.day_index for day in original.days]


def test_broken_scenario_rejected_without_write(tmp_path) -> None:
    """Отняли последний день месяца → месяц не сходится с календарём: реджект, файла нет."""
    src = SHIPPED[0]
    yaml_path = _dump_yaml(tmp_path, src)
    payload = yaml.safe_load(yaml_path.read_text(encoding="utf-8-sig"))
    payload["days"].pop()
    yaml_path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    json_path = tmp_path / "broken.json"
    assert _run("compile", str(yaml_path), "-o", str(json_path)) != 0
    assert not json_path.exists()


def test_check_validates_without_write(tmp_path) -> None:
    yaml_path = _dump_yaml(tmp_path, SHIPPED[0])
    json_path = tmp_path / "check.json"
    assert _run("compile", str(yaml_path), "-o", str(json_path), "--check") == 0
    assert not json_path.exists()


def test_dump_day_view_is_readable(tmp_path, capsys) -> None:
    """`dump --day N` печатает кадр для чтения: станция, глава, три варианта."""
    src = SHIPPED[0]
    assert _run("dump", str(src), "--day", "1") == 0
    out = capsys.readouterr().out
    cassette = validate_file(src).cassette
    assert cassette is not None
    day1 = cassette.days[0]
    assert f"День {day1.day_index}" in out
    assert day1.station in out
    assert day1.chapter_title in out
    assert day1.cards[0].title in out
    assert day1.cards[0].consequence in out