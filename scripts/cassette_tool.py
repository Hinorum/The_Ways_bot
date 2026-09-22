"""Кассетный инструментарий хранителя: декомпилятор/компилятор кассет.

Кассета в репозитории — JSON (эталон движка, `app/story/cassettes/*.json`),
машиночитаемый и строго валидируемый `app/story/schema.py`. Для правок
хранитель работает с YAML-«сценарием»: декомпилятор печатает месяц днями,
станцией, главой и тремя вариантами голосования, компилятор собирает
отредактированный YAML обратно в JSON через тот же `validate_payload` —
контракт не дублируется, единственный гейт остаётся в schema.py.

Примеры:
    python scripts/cassette_tool.py dump app/story/cassettes/imeniny-chasov.json
    python scripts/cassette_tool.py dump app/story/cassettes/imeniny-chasov.json --day 5
    python scripts/cassette_tool.py compile edits/imeniny-chasov.yaml --check
    python scripts/cassette_tool.py compile edits/imeniny-chasov.yaml
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

# Запуск скрипта из любого каталога: инструмент ходит в app.story.schema.
# isort: off
if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# isort: on

from app.story.schema import Cassette, ValidationResult, validate_file, validate_payload

_DUMP_USAGE = "dump КАССЕТА.json [-o СЦЕНАРИЙ.yaml] [--day N]"
_COMPILE_USAGE = "compile СЦЕНАРИЙ.yaml [-o КАССЕТА.json] [--check]"


def to_yaml_text(cassette: Cassette) -> str:
    """Полная кассета → YAML-сценарий (порядок ключей = контракту, defaults явные)."""
    payload = cassette.model_dump(mode="json")
    return yaml.safe_dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=120,
    )


def day_view(cassette: Cassette, day: int) -> str:
    """Человекочитаемый кадр одного дня главной дороги (без записи)."""
    item = cassette.day_for(day, "main")
    if item is None:
        raise ValueError(
            f"дня {day} нет на главной дороге (месяц {cassette.month}, " f"дней {len(cassette.days)})"
        )
    lines = [
        f"=== {cassette.month} · День {item.day_index} · "
        f"закон-метка {item.rule_hint} · {item.station} ===",
        item.chapter_title,
        "",
        item.chapter_text,
        "",
    ]
    for card in sorted(item.cards, key=lambda entry: entry.position):
        lines.append(f"[{card.position}] {card.title}")
        lines.append(f"    Суть: {card.description}")
        lines.append(f"    Канон, если уцелеет: {card.consequence}")
        if card.image_path:
            lines.append(f"    image_path: {card.image_path}")
        lines.append("")
    if item.hook_text:
        lines.append(f"(пометка автора) {item.hook_text}")
    if item.tie_note:
        lines.append(f"(оговорка ничьей) {item.tie_note}")
    return "\n".join(lines).rstrip()


def compose(yaml_text: str) -> ValidationResult:
    """YAML-сценарий → ValidationResult (тот же гейт, что у движка)."""
    try:
        payload = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return ValidationResult(cassette=None, errors=[f"не YAML: {exc}"])
    if not isinstance(payload, dict):
        return ValidationResult(cassette=None, errors=["корень YAML — объект кассеты"])
    return validate_payload(payload)


def dump_json(cassette: Cassette, path: Path) -> None:
    """Пишет JSON-эталон кассеты (нормализованный, серж голов авто-дефолтами)."""
    text = json.dumps(cassette.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    _atomic_write(path, text)


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
        help="куда писать *.yaml (по умолчанию рядом с исходником, тот же stem)",
    )
    p_dump.add_argument(
        "--day", type=int, default=None,
        help="печать одного дня главной дороги в терминал (без записи файла)",
    )

    p_compile = sub.add_parser("compile", usage=_COMPILE_USAGE, help="YAML-сценарий → кассета JSON")
    p_compile.add_argument("source", help="путь к *.yaml")
    p_compile.add_argument(
        "-o", "--out", default=None,
        help="куда писать *.json (по умолчанию рядом с yaml, тот же stem)",
    )
    p_compile.add_argument("--check", action="store_true", help="только валидация, без записи")
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
            try:
                print(day_view(result.cassette, args.day))
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

    return 2  # не должно случаться: subparsers required


def main(argv: list[str] | None = None) -> None:
    sys.exit(run(argv))


if __name__ == "__main__":
    main()