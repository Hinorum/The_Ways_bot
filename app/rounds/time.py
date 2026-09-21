from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.config import settings

_ROMAN = ("I", "II", "III")


def _now() -> datetime:
    return datetime.now(UTC)


def utc_aware(value: datetime) -> datetime:
    """Гарантирует tzinfo=UTC у даты из БД.

    Postgres с TIMESTAMPTZ возвращает aware-даты, но SQLite игнорирует
    timezone=True и отдаёт наивные значения — без нормализации любое
    сравнение «дата из базы против _now()» падает на локальных прогонах.
    """
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _next_hour_slot(after: datetime, hour: int) -> datetime:
    """Ближайший момент «after-дня или позже» ровно в hour:00 UTC."""
    hour %= 24
    candidate = after.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(days=1)
    return candidate


def _day_window(opens_at: datetime) -> tuple[datetime, datetime]:
    """Границы голосования и подсчёта дня на сетке UTC.

    Голосование закрывается ровно в day_close_hour_utc — сразу после
    закрытия следуют мгновенный подсчёт и итоги. Первый день стартует сразу
    после создания/сброса, дальше дни идут строго по сетке 11:00 UTC даже
    после простоя бота.
    """
    # Бесшовный день: голосование закрывается в DAY_CLOSE_HOUR_UTC,
    # подсчёт и итоги — сразу (секунды), новый день открывается следом
    # из заготовки. TALLYING как час простоя больше не существует;
    # поле tally_ends_at сохранено для совместимости схемы.
    voting_ends_at = _next_hour_slot(opens_at, settings.day_close_hour_utc)
    if voting_ends_at - opens_at < timedelta(hours=6):
        # Открылись слишком близко к границе — короткий день никому не нужен,
        # переносим закрытие на следующие сутки (сетка сохраняется).
        voting_ends_at += timedelta(days=1)
    return voting_ends_at, voting_ends_at