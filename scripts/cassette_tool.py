"""Кассетный инструментарий хранителя: декомпилятор/компилятор кассет.

Кассета в репозитории — JSON (эталон движка, `app/story/cassettes/*.json`),
машиночитаемый и строго валидируемый `app/story/schema.py`. Для правок
хранитель работает с YAML-«сценарием»: декомпилятор печатает месяц днями,
станцией, главой и тремя вариантами голосования, компилятор собирает
отредактированный YAML обратно в JSON через тот же `validate_payload` —
контракт не дублируется, единственный гейт остаётся в schema.py.

Весь функционал делегирован `app.story.editor` — тому же ядру, что
использует панель хранителя в боте (`/cassette` → «Редактор плёнки»).

Примеры:
    python scripts/cassette_tool.py dump app/story/cassettes/imeniny-chasov.json
    python scripts/cassette_tool.py dump app/story/cassettes/imeniny-chasov.json --day 5
    python scripts/cassette_tool.py dump app/story/cassettes/imeniny-chasov.json --day 14 --road morning
    python scripts/cassette_tool.py dump app/story/cassettes/imeniny-chasov.json --day 12 -o edits/day12.yaml
    python scripts/cassette_tool.py patch app/story/cassettes/imeniny-chasov.json edits/day12.yaml --day 12 --check
    python scripts/cassette_tool.py compile edits/imeniny-chasov.yaml --check
    python scripts/cassette_tool.py compile edits/imeniny-chasov.yaml
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml
from pydantic import ValidationError

# Запуск скрипта из любого каталога: инструмент ходит в app.story.schema.
# isort: off
if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# isort: on

from app.story import editor as ed
from app.story.schema import (
    Cassette,
    DayModel,
    ValidationResult,
    validate_file,
    validate_payload,
)

_DUMP_USAGE = "dump КАССЕТА.json [-o СЦЕНАРИЙ.yaml] [--day N] [--road ДОРОГА]"
_COMPILE_USAGE = "compile СЦЕНАРИЙ.yaml [-o КАССЕТА.json] [--check]"
_PATCH_USAGE = "patch КАССЕТА.json ФРАГМЕНТ.yaml --day N [--road ДОРОГА] [-o КАССЕТА.json] [--check]"


def to_yaml_text(cassette: Cassette) -> str:
    """Полная кассета → YAML-сценарий (порядок ключей = контракту, defaults явные)."""
    return ed.scenario_yaml(cassette)


def to_day_yaml(day: DayModel, road: str = "main") -> str:
    """Фрагмент дня для patch: YAML + заголовок-метка дороги (`# дорога: …`)."""
    return ed.day_yaml(day, road)


def day_view(cassette: Cassette, day: int, road: str = "main") -> str:
    """Человекочитаемый кадр одного дня на дороге (по умолчанию — main)."""
    return ed.day_view_text(cassette, day, road)


def compose(yaml_text: str) -> ValidationResult:
    """YAML-сценарий → ValidationResult (тот же гейт, что у движка)."""
    return ed.scenario_from_text(yaml_text)


def dump_json(cassette: Cassette, path: Path) -> None:
    """Пишет JSON-эталон кассеты (нормализованный, серж голов авто-дефолтами)."""
    ed.write_json(cassette, path)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cassette_tool",
        description="Декомпилятор/компилятор сюжетных кассет The Ways.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_dump = sub.add_parser("dump", usage=_DUMP_USAGE, help="кассета JSON → YAML-сценарий")
    p_dump.add_argument("source", help="путь к *.json кассеты")
    p_dump.add_argument(
        "-o", "--out", default=None,
        help="куда писать *.yaml (вся кассета или фрагмент дня, см. --day)",
    )
    p_dump.add_argument(
        "--day", type=int, default=None,
        help="один день: без -o печатает кадр в терминал, с -o пишет фрагмент для patch",
    )
    p_dump.add_argument(
        "--road", default="main",
        help="дорога дня (main или имя перемотки switch.to); по умолчанию main",
    )

    p_compile = sub.add_parser("compile", usage=_COMPILE_USAGE, help="YAML-сценарий → кассета JSON")
    p_compile.add_argument("source", help="путь к *.yaml")
    p_compile.add_argument(
        "-o", "--out", default=None,
        help="куда писать *.json (по умолчанию рядом с yaml, тот же stem)",
    )
    p_compile.add_argument("--check", action="store_true", help="только валидация, без записи")

    p_patch = sub.add_parser("patch", usage=_PATCH_USAGE, help="правка одного дня кассеты")
    p_patch.add_argument("cassette", help="путь к *.json кассеты")
    p_patch.add_argument("day_file", help="путь к *.yaml фрагменту дня (dump --day N -o ФРАГМЕНТ.yaml)")
    p_patch.add_argument("--day", type=int, required=True, help="день месяца, который правим")
    p_patch.add_argument(
        "--road", default="main",
        help="дорога: main или имя перемотки (switch.to); по умолчанию main",
    )
    p_patch.add_argument(
        "-o", "--out", default=None,
        help="куда писать *.json (по умолчанию — на место кассеты)",
    )
    p_patch.add_argument("--check", action="store_true", help="только валидация результата")
    return parser


def run(argv: list[str] | None = None) -> int:
    """Точка входа CLI. Возвращает код выхода (0 = успех)."""
    args = _build_parser().parse_args(argv)

    if args.command == "dump":
        source = Path(args.source)
        result = validate_file(source)
        if result.cassette is None:
            print("dump: кассета не читается:", file=sys.stderr)
            for error in result.errors:
                print(f"  - {error}", file=sys.stderr)
            return 1
        if args.day is not None:
            item = result.cassette.day_for(args.day, args.road)
            if item is None:
                print(
                    f"dump: дня {args.day} нет на дороге {args.road}",
                    file=sys.stderr,
                )
                return 1
            if args.out:
                out = Path(args.out)
                _atomic_write(out, to_day_yaml(item, road=args.road))
                print(f"dump: день {args.day} (дорога {args.road}) -> {out}")
                return 0
            try:
                print(day_view(result.cassette, args.day, road=args.road))
            except ValueError as exc:
                print(f"dump: {exc}", file=sys.stderr)
                return 1
            return 0
        out = Path(args.out) if args.out else source.with_suffix(".yaml")
        _atomic_write(out, to_yaml_text(result.cassette))
        print(f"dump: {source} -> {out}")
        return 0

    if args.command == "compile":
        source = Path(args.source)
        try:
            text = source.read_text(encoding="utf-8-sig")
        except OSError as exc:
            print(f"compile: не прочитать {source}: {exc}", file=sys.stderr)
            return 1
        result = compose(text)
        if result.cassette is None:
            print("compile: кассета не принята:", file=sys.stderr)
            for error in result.errors:
                print(f"  - {error}", file=sys.stderr)
            return 1
        for warning in result.warnings:
            print(f"compile: замечание — {warning}")
        if args.check:
            print(f"compile --check: кассета валидна ({source.name}).")
            return 0
        out = Path(args.out) if args.out else source.with_suffix(".json")
        dump_json(result.cassette, out)
        print(f"compile: {source} -> {out}")
        return 0

    if args.command == "patch":
        cassette_path = Path(args.cassette)
        base = validate_file(cassette_path)
        if base.cassette is None:
            print("patch: кассета не читается:", file=sys.stderr)
            for error in base.errors:
                print(f"  - {error}", file=sys.stderr)
            return 1
        if args.road != "main" and not any(
            fork.to == args.road for fork in base.cassette.switch
        ):
            roads = ", ".join(
                ["main"] + [fork.to for fork in base.cassette.switch]
            )
            print(
                f"patch: дороги {args.road} в кассете нет (есть: {roads})",
                file=sys.stderr,
            )
            return 1
        try:
            text = Path(args.day_file).read_text(encoding="utf-8-sig")
        except OSError as exc:
            print(f"patch: не прочитать {args.day_file}: {exc}", file=sys.stderr)
            return 1
        try:
            fragment = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            print(f"patch: фрагмент не YAML: {exc}", file=sys.stderr)
            return 1
        if not isinstance(fragment, dict):
            print("patch: фрагмент дня — это объект (dump --day N -o), а не список", file=sys.stderr)
            return 1
        try:
            day = DayModel.model_validate(fragment)
        except ValidationError as exc:
            print("patch: фрагмент дня не принят:", file=sys.stderr)
            for error in exc.errors():
                location = ".".join(str(part) for part in error["loc"])
                print(f"  - {location}: {error['msg']}", file=sys.stderr)
            return 1
        if day.day_index != args.day:
            print(
                f"patch: в фрагменте day_index {day.day_index}, а заявлен --day {args.day}",
                file=sys.stderr,
            )
            return 1
        payload = base.cassette.model_dump(mode="json")
        if not ed.replace_day(payload, args.road, day.model_dump(mode="json")):
            print(
                f"patch: день {args.day} вне пределов дороги {args.road}",
                file=sys.stderr,
            )
            return 1
        result = validate_payload(payload)
        if result.cassette is None:
            print("patch: итоговая кассета не принята:", file=sys.stderr)
            for error in result.errors:
                print(f"  - {error}", file=sys.stderr)
            return 1
        for warning in result.warnings:
            print(f"patch: замечание — {warning}")
        if args.check:
            print(
                f"patch --check: правка дня {args.day} ({args.road}) валидна "
                f"для {cassette_path.name}."
            )
            return 0
        out = Path(args.out) if args.out else cassette_path
        dump_json(result.cassette, out)
        print(f"patch: {cassette_path} -> {out} (день {args.day}, дорога {args.road})")
        return 0

    return 2  # не должно случаться: subparsers required


def main(argv: list[str] | None = None) -> None:
    sys.exit(run(argv))


if __name__ == "__main__":
    main()