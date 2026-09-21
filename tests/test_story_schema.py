"""Контракт сюжетной кассеты: месяцы 28–31 день, структура, лимиты, табу.

Кассета обязана описывать ровно один календарный месяц («YYYY-MM») и содержать
ровно столько дней, сколько в этом месяце по календарю. Жёсткие ошибки
отвергают кассету; бюджет режиссуры (rule_hint) — мягкие замечания.
"""

from __future__ import annotations

import json
from datetime import date

from app.story.schema import (
    FIELD_LIMITS,
    ValidationResult,
    validate_file,
    validate_payload,
)


def _day(index: int, **overrides) -> dict:
    data = {
        "day_index": index,
        "station": f"Станция {index}",
        "chapter_title": f"Глава {index}",
        "chapter_text": "Стая собирается у котла и решает, куда идти.",
        "hook_text": None,
        "rule_hint": "any",
        "cards": [
            {
                "position": 0,
                "title": f"Путь А {index}",
                "description": "Громкий, очевидный путь.",
                "consequence": "Стая пошла путём А и нашла свет.",
                "tag": "care",
                "image_path": "",
            },
            {
                "position": 1,
                "title": f"Путь Б {index}",
                "description": "Тихий, рискованный путь.",
                "consequence": "Стая ушла путём Б и нашла тень.",
                "tag": "care",
                "image_path": "",
            },
            {
                "position": 2,
                "title": f"Путь В {index}",
                "description": "Середина, компромисс.",
                "consequence": "Стая осталась и дождалась утра.",
                "tag": "care",
                "image_path": "",
            },
        ],
        "tie_note": None,
    }
    data.update(overrides)
    return data


def _payload(month: str, n_days: int, **overrides) -> dict:
    payload = {
        "cassette_id": "test-kasseta",
        "month": month,
        "title": "Тестовая кассета",
        "logline": "проверка контракта.",
        "days": [_day(i) for i in range(1, n_days + 1)],
    }
    payload.update(overrides)
    return payload


def test_month_lengths_28_to_31() -> None:
    # Февраль 2027 — 28 дней, февраль 2024 (високосный) — 29, апрель — 30, январь — 31.
    for month, n in (
        ("2027-02", 28),
        ("2024-02", 29),
        ("2026-04", 30),
        ("2026-01", 31),
    ):
        result = validate_payload(_payload(month, n))
        assert result.ok, result.errors
        assert result.cassette is not None
        assert len(result.cassette.days) == n


def test_wrong_days_count_for_month_rejected() -> None:
    result = validate_payload(_payload("2027-02", 30))
    assert not result.ok
    assert any("2027-02" in error for error in result.errors)


def test_duplicate_day_index_rejected() -> None:
    payload = _payload("2026-04", 30)
    payload["days"][4]["day_index"] = 1
    result = validate_payload(payload)
    assert not result.ok


def test_missing_day_rejected() -> None:
    result = validate_payload(_payload("2026-01", 29))  # в январе 31 день
    assert not result.ok


def test_bad_cards_positions_rejected() -> None:
    payload = _payload("2026-04", 30)
    payload["days"][0]["cards"][1]["position"] = 0
    result = validate_payload(payload)
    assert not result.ok
    assert any("позиции 0, 1, 2" in error for error in result.errors)


def test_bad_rule_hint_rejected() -> None:
    payload = _payload("2026-04", 30)
    payload["days"][0]["rule_hint"] = "majorit"
    result = validate_payload(payload)
    assert not result.ok


def test_bad_month_format_rejected() -> None:
    assert not validate_payload(_payload("2026-13", 30)).ok
    assert not validate_payload(_payload("okt-2026", 30)).ok
    assert not validate_payload(_payload("", 30)).ok


def test_field_length_limits_rejected() -> None:
    too_long = "а" * (FIELD_LIMITS["chapter_title"] + 1)
    result = validate_payload(
        _payload("2026-04", 30, days=[_day(1, chapter_title=too_long)])
    )
    assert not result.ok

    too_long_card = "а" * (FIELD_LIMITS["card_title"] + 1)
    payload = _payload("2026-04", 30)
    payload["days"][0]["cards"][0]["title"] = too_long_card
    assert not validate_payload(payload).ok

    payload = _payload("2026-04", 30)
    payload["days"][0]["hook_text"] = "к" * (FIELD_LIMITS["hook_text"] + 1)
    assert not validate_payload(payload).ok

    payload = _payload("2026-04", 30)
    payload["days"][0]["tie_note"] = "т" * (FIELD_LIMITS["tie_note"] + 1)
    assert not validate_payload(payload).ok


def test_empty_card_fields_rejected() -> None:
    payload = _payload("2026-04", 30)
    payload["days"][0]["cards"][0]["title"] = ""
    assert not validate_payload(payload).ok


def test_taboo_word_rejected() -> None:
    payload = _payload("2026-04", 30)
    payload["days"][0]["chapter_text"] = "Наши граммы дают хороший доход стае."
    result = validate_payload(payload)
    assert not result.ok
    assert any("стоп-слова" in error for error in result.errors)


def test_rule_hint_budget_is_soft_warning() -> None:
    payload = _payload("2026-04", 30, days=[_day(i, rule_hint="majority") for i in range(1, 31)])
    result = validate_payload(payload)
    assert result.ok
    assert any("majority" in warning for warning in result.warnings)


def test_missing_attribution_is_soft_warning() -> None:
    """Отсутствие клейма (attribution) — warning, а не ошибка (см. промпт §5)."""
    result = validate_payload(_payload("2026-04", 30))
    assert result.ok
    assert result.cassette is not None
    assert not (result.cassette.attribution or "").strip()
    assert any("attribution" in warning for warning in result.warnings)


def test_active_day_matches_calendar_day() -> None:
    result = validate_payload(_payload("2026-10", 31))
    assert result.cassette is not None
    day = result.cassette.active_day(date(2026, 10, 15))
    assert day is not None and day.day_index == 15
    # Последний день месяца играется (октябрь — 31 день, на 31-е числа есть день).
    assert result.cassette.active_day(date(2026, 10, 31)).day_index == 31
    # Другой месяц — кассета не играется (стоп на стыке).
    assert result.cassette.active_day(date(2026, 9, 15)) is None


def test_active_day_keeps_full_scene_text() -> None:
    """Глава кассеты — текст сцены (не крючок): сохраняется как есть, целиком."""
    result = validate_payload(_payload("2026-10", 31))
    day = result.cassette.active_day(date(2026, 10, 1))
    assert day is not None
    assert day.chapter_text == _day(1)["chapter_text"]


def test_validate_file_reads_and_validates(tmp_path) -> None:
    cassette = "cassettes"
    good_dir = tmp_path / cassette
    good_dir.mkdir()
    good = good_dir / "ok.json"
    good.write_text(
        json.dumps(_payload("2026-04", 30), ensure_ascii=False), encoding="utf-8"
    )
    result: ValidationResult = validate_file(good)
    assert result.ok

    broken = good_dir / "broken.json"
    broken.write_text("{не json", encoding="utf-8")
    result = validate_file(broken)
    assert not result.ok
    assert any("не JSON" in error for error in result.errors)

    missing = good_dir / "nope.json"
    result = validate_file(missing)
    assert not result.ok
    assert any("не прочитать" in error for error in result.errors)


def test_bom_is_tolerated(tmp_path) -> None:
    raw = json.dumps(_payload("2026-04", 30), ensure_ascii=False).encode("utf-8")
    path = tmp_path / "bom.json"
    path.write_bytes(b"\xef\xbb\xbf" + raw)
    assert validate_file(path).ok