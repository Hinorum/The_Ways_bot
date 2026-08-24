"""ISO-недели для копилки лидерборда: чистые функции без зависимостей.

Неделя — ISO-ключ «YYYY-Www» по UTC. Принадлежность дня неделе считается
по моменту ОТКРЫТИЯ дня (opens_at): день, открытый в воскресенье 11:00 UTC,
целиком относится к уходящей неделе, даже если итоги по нему подводятся в
понедельник. Выплата проходит в понедельник после закрытия последнего дня
недели; доли призовых мест, которым не нашлось достойного игрока,
переносятся в копилку новой недели.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def iso_week_key(moment: datetime) -> str:
    """ISO-ключ недели «YYYY-Www» (например «2026-W34») по UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    iso = moment.astimezone(timezone.utc).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def week_bounds(key: str) -> tuple[datetime, datetime]:
    """Границы недели [понедельник 00:00 UTC, следующий понедельник 00:00 UTC)."""
    year_text, week_text = key.split("-W")
    start = datetime.fromisocalendar(int(year_text), int(week_text), 1).replace(tzinfo=timezone.utc)
    return start, start + timedelta(weeks=1)


def previous_week_key(now: datetime | None = None) -> str:
    """Ключ последней ПОЛНОСТЬЮ прошедшей недели."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return iso_week_key(now.astimezone(timezone.utc) - timedelta(weeks=1))


def parse_prize_pcts(spec: str) -> list[int]:
    """«20,30,50» → [20, 30, 50]; мусор отбрасывается, пустой список → нет выплат."""
    pcts: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if part.isdigit() and int(part) > 0:
            pcts.append(int(part))
    return pcts[:3]
