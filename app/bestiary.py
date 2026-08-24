"""Бестиарий Сети — «Monster Manual» Правил Стаи.

Существа делятся на два рода:
- маски законов дня (Голос Стаи, Одинокий Волк, Середняк, Слепая Яма) —
  встречаются, когда выпадает соответствующий день;
- сюжетные звери сезона (Хозяин Ошибки) — появляются, когда его ступень
  перестаёт быть бытовой.

Запись идемпотентна за сезон (уникальность season+beast_key), поэтому
/materialize может звать её каждый день без дублей. /best показывает
встреченных; невстреченные остаются «???» — коллекция как награда за
долгую игру. На механику денег и голосов бестиарий не влияет.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BestiarySighting, Round
from app.season import villain_stage

# Ключ → (титул, описание). Титул скрытого существа не показываем.
BEASTIES: dict[str, tuple[str, str]] = {
    "choir": (
        "Голос Стаи",
        "Ночное существо-хор: живёт в днях большинства. Слышно, когда стая "
        "лает в один голос; считается добрым знаком для тех, кто с всеми.",
    ),
    "wolf": (
        "Одинокий Волк",
        "Хозяин ночи меньшинства. Путь достаётся отставшему: там, где "
        "войско прошло мимо, он подбирает одинокие голоса.",
    ),
    "walker": (
        "Середняк",
        "Существо середины: бродит между крайностями и забирает то, что "
        "не громче и не тише остальных. Крайности его боятся.",
    ),
    "pit": (
        "Слепая Яма",
        "День без закона: яма глотает правило утром и отрыгивает его к "
        "итогам. Внутри слышен только хеш-шёпот обязательства.",
    ),
    "master": (
        "Хозяин Ошибки",
        "Безликий пересчётчик стаи. Не говорит вовсе — о нём сообщают "
        "последствия: лишние метки, чужие страницы, счёт, который "
        "сходится не так. Строит коридор к Первому Лаю.",
    ),
}


async def note_round(session: AsyncSession, round_row: Round, season_key_value: str | None = None) -> int:
    """Фиксирует встречи, которые несёт этот день. Возвращает число новых записей."""
    season = season_key_value or round_row.season or str(round_row.day_index)
    wanted: list[tuple[str, str]] = []
    mask_title, mask_desc = _mask_for(round_row)
    if mask_title is not None:
        key = _mask_key(round_row)
        wanted.append((key, f"{mask_title}. {mask_desc}"))
    if bool(getattr(round_row, "sealed", False)):
        title, desc = BEASTIES["pit"]
        wanted.append(("pit", f"{title}. {desc}"))
    anchor_moment = round_row.opens_at
    from app.season import run_position, get_cached_anchor

    try:
        run_day, total = run_position(get_cached_anchor(anchor_moment), anchor_moment)
        if villain_stage(run_day, total) >= 1:
            title, desc = BEASTIES["master"]
            wanted.append(("master", f"{title}. {desc}"))
    except Exception:
        pass
    created = 0
    for beast_key, description in wanted:
        exists = (
            await session.execute(
                select(BestiarySighting.id).where(
                    BestiarySighting.season == season,
                    BestiarySighting.beast_key == beast_key,
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            continue
        title = BEASTIES.get(beast_key, (beast_key, ""))[0]
        session.add(
            BestiarySighting(
                season=season,
                beast_key=beast_key,
                day_index=round_row.day_index,
                title=title,
                description=description,
            )
        )
        created += 1
    if created:
        await session.flush()
    return created


def _mask_for(round_row: Round) -> tuple[str | None, str | None]:
    from app.models import RULE_MASKS

    mask = RULE_MASKS.get(round_row.win_rule)
    if mask is None:
        return None, None
    return mask[0], mask[1]


def _mask_key(round_row: Round) -> str:
    from app.models import WinRule

    return {
        WinRule.MAJORITY: "choir",
        WinRule.MINORITY: "wolf",
        WinRule.MEDIAN: "walker",
    }.get(round_row.win_rule, "choir")


async def bestiary_text(session: AsyncSession) -> str:
    """Текст /best: встреченные существа целиком, невстреченные — «???»."""
    seen_rows = (
        await session.execute(
            select(BestiarySighting.beast_key).distinct().order_by(BestiarySighting.beast_key.asc())
        )
    ).scalars()
    seen = set(seen_rows.all())
    lines = ["📖 Бестиарий Сети:"]
    for key in sorted(BEASTIES):
        title, description = BEASTIES[key]
        if key in seen:
            lines.append(f"• {title} — {description}")
        else:
            lines.append("• ??? — это существо стая ещё не встречала.")
    hidden = len(BEASTIES) - len(seen & set(BEASTIES))
    lines.append(
        f"Встречено {len(seen & set(BEASTIES))} из {len(BEASTIES)}."
        + ("" if hidden == 0 else " Невстреченные ждут своего дня.")
    )
    return "\n".join(lines)
