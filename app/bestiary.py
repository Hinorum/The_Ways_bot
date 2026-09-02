"""Бестиарий Сети — «Monster Manual» Правил Стаи.

Существа делятся на три рода:
- маски законов дня (Голос Стаи, Одинокий Волк, Середняк, Слепая Яма) —
  встречаются, когда выпадает соответствующий день;
- сюжетные звери сезона (Администратор, Еретик, кот, Чинитель, Совет,
  руины, огонь) — появляются, когда их ступень плана перестаёт быть бытовой;
- силы лабиринта (Крыса, Анубис) — приходят по позиции в сезоне: Крыса
  в первые дни круга, Анубис в последние.

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
        "лает в один голос; считается добрым знаком для тех, кто со всеми. "
        "Но стая, которая лает хором, перестаёт слышать одиноких — "
        "большинство удобно тем, кто не думает сам.",
    ),
    "wolf": (
        "Одинокий Волк",
        "Хозяин ночи меньшинства. Путь достаётся отставшему: там, где "
        "войско прошло мимо, он подбирает одинокие голоса. Одинокий не потому, "
        "что выбрал путь — а потому, что его путь выбросил стая.",
    ),
    "walker": (
        "Середняк",
        "Существо середины: бродит между крайностями и забирает то, что "
        "не громче и не тише остальных. Крайности его боятся. Но середина — "
        "это не мудрость, а страх. Он никогда не выбирает первым "
        "и никогда не отвечает за выбор.",
    ),
    "pit": (
        "Слепая Яма",
        "День без закона: яма глотает правило утром и отрыгивает его к "
        "итогам. Внутри слышен только хеш-шёпот обязательства. Яма не слепа — "
        "она закрывает глаза, когда ей удобно.",
    ),
    "master": (
        "Администратор",
        "Тень без лица, которая пересчитывает стаю и чинит лабиринт, мечтая вернуть "
        "всем ровный сон без единой ошибки. Не говорит вовсе — о нём сообщают только "
        "последствия пересчётов. Его одержимость порядком — та же тюрьма, только с "
        "замком снаружи.",
    ),
    "heretic": (
        "Еретик, Свернувший с Пути",
        "Палладин этой игры: пёс из старой Стаи, где один сон был на всех. "
        "Заскучал и увёл стаю переписывать правила. Говорит формулами, "
        "не оправдывается; его знак — апостроф. Он не освободил стаю — "
        "он переписал правила под себя и назвал это свободой.",
    ),
    "cat": (
        "Кошачий след",
        "Примета старого мира: след лапы на пыльной папке, сон, который "
        "никто не признаётся. Появляется, когда чужой план почти готов. "
        "Кошка приходит не помочь — а забрать то, что уже почти её.",
    ),
    "engineer": (
        "Чинитель порогов",
        "Старый мастер лабиринта из того мира, где был один сон на всех. "
        "Он чинит коридоры так, что те помнят, кого провели, — и ломает "
        "их так, что этого никто не узнаёт. Двуличие — его инструмент: "
        "одной рукой строит, другой — разрушает.",
    ),
    "chingiz": (
        "Двое с перехода",
        "Двое из старого свода, что держали тактику переходов между "
        "мирами: воин поминовения и полководец мисок. Они помнят каждый долг — "
        "и ждут, когда долг станет оружием.",
    ),
    "dogtown": (
        "Руины первого города",
        "То, что осталось от первого приюта стаи — пустоши хранят его "
        "каркас, а рынок над рекой торгует его памятью. Город мёртв, но его "
        "тень продолжает торговать тем, что уже не принадлежит.",
    ),
    "aretha": (
        "Поджигательница",
        "Персонификация риска и огня из старого мира: палит проводку, "
        "будит стены лабиринта и не признаёт чужих правил. Она не героиня — "
        "она пожар, который горит и себя тоже.",
    ),
    "rat": (
        "Крыса из стен Лабиринта",
        "Живёт в стёртых версиях дней, тех, что «не случились». Помнит все "
        "круги и продаёт дежавю-подсказки за память: «забота сегодня "
        "недооценена», «хитрость уже выкупили». Счёт её не стирает: никто не "
        "считает её за ценность. Она паразитирует на чужих ошибках и не "
        "стесняется этого.",
    ),
    "anubis": (
        "Анубис, Судья Цикла",
        "Древний, держит весы. Был до старой Стаи: взвешивает каждый круг, "
        "счёт против выбора. Встаёт в День Первого Лая и решает, разомкнётся "
        "ли петля, — что стая положила на чашу, то и станет осадком следующего. "
        "Он не справедлив — он точен. А точность не прощает.",
    ),
    "archivist": (
        "Архивариус",
        "Хозяин лабиринта: знает все коридоры наизусть и выбирает, какой открыть "
        "сегодня. Не торговец — проводник, который ведёт стаю туда, куда "
        "решил сам. Фонарь в его лапе показывает не путь, а тот путь, "
        "который он выбрал за тебя. Ключи на поясе — от каждой двери, "
        "включая те, которые стая ещё не нашла. Он не хранит папки — "
        "он управляет потоком дня."
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
    else:
        # Закон объявлен — Архивариус вышел из тени и показал расхождение.
        title, desc = BEASTIES["archivist"]
        wanted.append(("archivist", f"{title}. {desc}"))
    anchor_moment = round_row.opens_at
    from app.season import run_position, get_cached_anchor

    try:
        run_day, total = run_position(get_cached_anchor(anchor_moment), anchor_moment)
        stage = villain_stage(run_day, total)
        if run_day <= 4:
            # В первые дни круга лабиринт напоминает о себе: Крыса стен.
            title, desc = BEASTIES["rat"]
            wanted.append(("rat", f"{title}. {desc}"))
        if run_day >= total - 2:
            # На исходе сезона у Первого Лая встаёт судья цикла.
            title, desc = BEASTIES["anubis"]
            wanted.append(("anubis", f"{title}. {desc}"))
        if stage >= 1:
            title, desc = BEASTIES["master"]
            wanted.append(("master", f"{title}. {desc}"))
            # Еретик выходит из теней вместе с первой явной приметой Хозяина.
            title, desc = BEASTIES["heretic"]
            wanted.append(("heretic", f"{title}. {desc}"))
        if stage == 2:
            # В середине арки старый мир начинает чинить пороги изнутри.
            title, desc = BEASTIES["engineer"]
            wanted.append(("engineer", f"{title}. {desc}"))
        if stage >= 3:
            # В кризисе сезона старый мир напоминает о себе кошачьим следом.
            title, desc = BEASTIES["cat"]
            wanted.append(("cat", f"{title}. {desc}"))
            # Старый мир возвращается за стаей: Совет, руины и огонь.
            title, desc = BEASTIES["chingiz"]
            wanted.append(("chingiz", f"{title}. {desc}"))
            title, desc = BEASTIES["dogtown"]
            wanted.append(("dogtown", f"{title}. {desc}"))
            title, desc = BEASTIES["aretha"]
            wanted.append(("aretha", f"{title}. {desc}"))
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
    lines = ["📖 Бестиарий Лабиринта:"]
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
