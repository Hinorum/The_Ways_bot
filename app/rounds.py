from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import secrets
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import delete, distinct, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.echoes import collect_due_echoes, spawn_echoes_from_round
from app.models import (
    Card,
    Income,
    LoreEcho,
    MemoryHit,
    Payout,
    Player,
    PreparedDay,
    RevoteGrant,
    Round,
    RoundStatus,
    RULE_PHRASES,
    Stake,
    StoryBeat,
    Vote,
    WatcherState,
    WinRule,
    WorldChoice,
    WorldEvent,
    WorldLocation,
    WorldCharacter,
    WorldSnapshot,
)
from app.art_director import build_image_prompt, character_motifs_for, plan_day_art, short_image_prompt
from app.memory import recall_beats
from app.season import season_key
from app.story import fetch_day_image, generate_chapter, generate_epilogue, render_cover
from app.ton_pay import pending_payout_count


logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def utc_aware(value: datetime) -> datetime:
    """Гарантирует tzinfo=UTC у даты из БД.

    Postgres с TIMESTAMPTZ возвращает aware-даты, но SQLite игнорирует
    timezone=True и отдаёт наивные значения — без нормализации любое
    сравнение «дата из базы против _now()» падает на локальных прогонах.
    """
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


_ROMAN = ("I", "II", "III")


def _next_hour_slot(after: datetime, hour: int) -> datetime:
    """Ближайший момент «after-дня или позже» ровно в hour:00 UTC."""
    hour %= 24
    candidate = after.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(days=1)
    return candidate


def _day_window(opens_at: datetime) -> tuple[datetime, datetime]:
    """Границы дня на сетке UTC.

    Голосование закрывается в (day_open_hour_utc - 1):00 — за час до открытия
    следующего дня; этот час занимает подсчёт. Первый день стартует сразу
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


async def write_epilogue(session: AsyncSession, round_row: Round) -> str:
    """После закрытия дня просит нейросеть описать, чем отозвался победивший путь.

    Пишется один раз (idемпотентно по непустому epilogue_text); при молчании
    сети итоги остаются сухим шаблоном — день не ломается.
    """
    if (
        not settings.use_free_story_llm
        or round_row.winner_card is None
        or round_row.epilogue_text
    ):
        return round_row.epilogue_text or ""
    counts = {}
    try:
        counts = {int(k): int(v) for k, v in json.loads(round_row.vote_counts_json or "{}").items()}
    except ValueError:
        pass
    counts_line = ", ".join(
        f"{_ROMAN[pos]} — {counts.get(pos, 0)}" for pos in range(3)
    )
    winner = next((c for c in round_row.cards if c.position == round_row.winner_card), None)
    if winner is None:
        return ""
    # Финальный день сезона: эпилог закрывает месяц как финальный аккорд.
    season_note = None
    try:
        from app.season import (
            alignment_finale_line,
            anchor_axes,
            is_run_finale,
            run_position,
        )
        from app.story_arc import arc_stage

        anchor = await get_run_anchor(session)
        run_day, total = run_position(anchor, utc_aware(round_row.voting_ends_at))
        if is_run_finale(run_day, total):
            order, moral = anchor_axes(anchor)
            season_note = (
                "Этот день закрыл сезон: эпилог должен прозвучать финальным "
                "аккордом месяца — мир после Первого Лая уже другой. "
                + alignment_finale_line(order, moral)
            )
        else:
            # Обычный день месяца: крючок эпилога продолжает текущий этап арки,
            # чтобы вечер не выпадал из сквозной линии (эпилог — часть цепочки).
            stage = arc_stage(run_day, total)
            season_note = (
                f"Арка месяца, этап «{stage['name']}» (день {run_day} из {total}): "
                f"крючок эпилога должен вести внутрь этого этапа, а не в никуда. "
                f"Тон этапа: {stage['tone']}."
            )
    except Exception:
        season_note = None
    try:
        text = await generate_epilogue(
            day_index=round_row.day_index,
            winner_title=winner.title,
            winner_consequence=winner.consequence,
            counts_line=counts_line,
            rule_phrase=RULE_PHRASES[round_row.win_rule],
            season_note=season_note,
        )
    except Exception as exc:
        logger = logging.getLogger(__name__)
        logger.warning("Эпилог дня %s не написан: %s", round_row.day_index, exc)
        return ""
    if text:
        round_row.epilogue_text = text
        await session.commit()
    return text


def commit_rule(rule: WinRule, salt: str) -> str:
    return hashlib.sha256(f"{rule.value}:{salt}".encode()).hexdigest()


def sealed_day(day_index: int) -> bool:
    """Глухой день: закон не объявляется утром — только хеш-обязательство.

    Расписание детерминированное (каждый N-й день), чтобы игроки могли
    положиться на ритм; день 1 никогда не глухой.
    """
    every = max(0, settings.sealed_day_every)
    if every <= 0:
        return False
    return day_index % every == min(7, every - 1)


# ---------- Живой банк дня (для синхронных постов) ----------

# round_id -> (нанотоны подтверждённых ставок, число ставок дня).
# Обновляется каждым тиком; синхронный статус дня читает без БД.
_POT_CACHE: dict[int, tuple[int, int]] = {}


def get_cached_pot(round_id: int) -> tuple[int, int]:
    return _POT_CACHE.get(round_id, (0, 0))


async def refresh_round_pot_cache(session: AsyncSession, round_row: Round) -> None:
    """Банк дня = сумма подтверждённых ставок; счётчик — все ставки дня."""
    rows = (
        await session.execute(
            select(Stake.amount_nanotons, Stake.status).where(Stake.round_id == round_row.id)
        )
    ).all()
    nano = sum(int(amount) for amount, status in rows if status == "confirmed")
    if len(_POT_CACHE) > 64:
        _POT_CACHE.clear()
    _POT_CACHE[round_row.id] = (nano, len(rows))


async def get_run_anchor(session: AsyncSession) -> dict:
    """Якорь забега из watcher_state; для новых инстансов создаётся «сейчас».

    Кэш в season обновляется здесь же: синхронные посты дня (broadcast)
    берут якорь оттуда без обращения к БД.

    При смене сезона (эпохи) оси перекатываются детерминированно: мир
    обновляется вместе с аркой, а дрейф от голосов стаи начинается заново.
    """
    from app.season import (
        RUN_START_KEY,
        current_season,
        default_anchor,
        parse_anchor,
        season_base_axes,
        set_run_anchor_cache,
    )
    from app.models import WatcherState

    row = await session.get(WatcherState, RUN_START_KEY)
    anchor = parse_anchor(row.value if row is not None else None)
    moment = _now()
    if anchor is None:
        anchor = default_anchor(moment)
        payload = json.dumps(anchor, ensure_ascii=False)
        if row is None:
            session.add(WatcherState(key=RUN_START_KEY, value=payload))
        else:
            row.value = payload
        await session.commit()
    else:
        # Смена сезона: перекат осей + обновление номера сезона.
        detected_season = current_season(anchor, moment)
        stored_season = anchor.get("season", 1)
        if detected_season != stored_season:
            axes = season_base_axes(anchor.get("key", ""), detected_season)
            anchor["season"] = detected_season
            anchor["order_axis"] = axes[0]
            anchor["moral_axis"] = axes[1]
            if row is not None:
                row.value = json.dumps(anchor, ensure_ascii=False)
                await session.commit()
        else:
            # Легаси-якорь без характера: роллим один раз и закрепляем.
            missing = [axis for axis in ("order_axis", "moral_axis") if axis not in anchor]
            if missing:
                import random as _random

                from app.season import _AXIS_START_POOL

                for axis in missing:
                    anchor[axis] = _random.choice(_AXIS_START_POOL)
                row.value = json.dumps(anchor, ensure_ascii=False)
                await session.commit()
    set_run_anchor_cache(anchor)
    return anchor


async def _villain_block(
    session: AsyncSession, open_moment: datetime, anchor: dict
) -> str | None:
    """Сюжет-машина сезона: продвигает план Администратора и отдаёт блок промпта.

    Состояние живёт в watcher_state (один ключ на весь мир): ступень и список
    событий. Ступени считаются по дням ЗАБЕГА (от сброса), а не календаря.
    """
    import json as _json

    from app.models import WatcherState
    from app.season import (
        VILLAIN_KEY,
        run_position,
        villain_event,
        villain_prompt_block,
        villain_stage,
    )

    key = anchor["key"]
    run_day, total = run_position(anchor, open_moment)
    stage = villain_stage(run_day, total)

    row = await session.get(WatcherState, VILLAIN_KEY)
    data: dict = {}
    if row is not None and row.value:
        try:
            data = json.loads(row.value)
        except ValueError:
            data = {}
    if data.get("season") != key or not isinstance(data, dict):
        # Новый сезон (или полный сброс): свежий план и своя соль событий —
        # перезапуск игры начинает арку с других канонических вех.
        data = {"season": key, "stage": -1, "events": [], "salt": secrets.token_hex(4)}

    changed = False
    while data["stage"] < stage:
        data["stage"] += 1
        data.setdefault("events", []).append(
            villain_event(key, data["stage"], data.get("salt", ""))
        )
        changed = True
    if changed or row is None:
        payload = _json.dumps(data, ensure_ascii=False)
        if row is None:
            session.add(WatcherState(key=VILLAIN_KEY, value=payload))
        else:
            row.value = payload
        await session.commit()
    return villain_prompt_block(list(data.get("events") or [])[-3:], stage)


async def get_active_round(session: AsyncSession) -> Round | None:
    result = await session.execute(
        select(Round)
        .options(selectinload(Round.cards))
        .where(Round.status.in_([RoundStatus.OPEN, RoundStatus.TALLYING]))
        .order_by(Round.day_index.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_round(session: AsyncSession, round_id: int) -> Round | None:
    result = await session.execute(
        select(Round).options(selectinload(Round.cards)).where(Round.id == round_id)
    )
    return result.scalar_one_or_none()


async def get_latest_round(session: AsyncSession) -> Round | None:
    result = await session.execute(
        select(Round).options(selectinload(Round.cards)).order_by(Round.day_index.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def previous_beats(session: AsyncSession, limit: int = 12) -> list[str]:
    """Канон последних дней для промпта главы, по порядку.

    Окно ограничено: без лимита через несколько месяцев канон переполнил бы
    контекст модели и генерация молча деградировала бы до офлайн-лора.
    Ранние дни растворяются в шуме порталов — как и в /lore.
    """
    result = await session.execute(
        select(StoryBeat).order_by(StoryBeat.day_index.desc()).limit(limit)
    )
    rows = list(result.scalars())
    rows.reverse()
    return [f"{beat.winning_title}: {beat.winning_text}" for beat in rows]


async def _previous_round_stats(
    session: AsyncSession,
) -> tuple[dict[int, int], int, int]:
    """Голоса, ставки и численность предыдущего закрытого дня для DDA.

    Возвращает (vote_counts, total_stakes_nanotons, player_count).
    Если предыдущего дня нет — дефолты (3, 0, 10).
    """
    from app.models import Stake, Vote

    beat_row = (
        await session.execute(
            select(StoryBeat).order_by(StoryBeat.day_index.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if beat_row is None:
        return {0: 1, 1: 1, 2: 1}, 0, 10

    import json

    try:
        counts = json.loads(beat_row.vote_counts)
        counts = {int(k): v for k, v in counts.items()}
    except Exception:
        counts = {0: 1, 1: 1, 2: 1}

    round_row = (
        await session.execute(
            select(Round).where(Round.day_index == beat_row.day_index).limit(1)
        )
    ).scalar_one_or_none()

    total_stakes = 0
    voter_count = 10
    if round_row is not None:
        stakes_result = await session.execute(
            select(func.coalesce(func.sum(Stake.amount_nanotons), 0)).where(
                Stake.round_id == round_row.id,
                Stake.status.in_(["confirmed", "settled"]),
            )
        )
        total_stakes = int(stakes_result.scalar() or 0)

        voters_result = await session.execute(
            select(func.count(func.distinct(Vote.player_id))).where(
                Vote.round_id == round_row.id
            )
        )
        voter_count = int(voters_result.scalar() or 10)

    return counts, total_stakes, max(voter_count, 1)


async def season_tag_balance(session: AsyncSession, key: str) -> dict[str, int]:
    """Характер стаи за сезон: теги победивших путей закрытых дней."""
    result = await session.execute(
        select(Card.tag)
        .join(Round, Card.round_id == Round.id)
        .where(
            Round.season == key,
            Round.status == RoundStatus.CLOSED,
            Card.position == Round.winner_card,
        )
    )
    balance = {"risk": 0, "care": 0, "cunning": 0}
    for (tag,) in result.all():
        balance[tag if tag in balance else "care"] += 1
    return balance


async def previous_season_summary(session: AsyncSession, current_key: str) -> str | None:
    """Осадок финала прошлого сезона: последний канон предыдущего месяца."""
    year, month = (int(part) for part in current_key.split("-"))
    prev_key = f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"
    result = await session.execute(
        select(Round.day_index)
        .where(Round.season == prev_key, Round.status == RoundStatus.CLOSED)
        .order_by(Round.day_index.desc())
        .limit(1)
    )
    day_index = result.scalar_one_or_none()
    if day_index is None:
        return None
    beat = (
        await session.execute(select(StoryBeat).where(StoryBeat.day_index == day_index))
    ).scalar_one_or_none()
    if beat is None:
        return None
    summary = f"{beat.winning_title}: {beat.winning_text}"
    return summary[:180]


async def places_memory_block(session: AsyncSession, limit: int = 10) -> str | None:
    """Память мест для промпта: где стая уже была и что там изменилось."""
    result = await session.execute(
        select(Round.place, Round.day_index)
        .where(Round.place.is_not(None))
        .order_by(Round.day_index.desc())
        .limit(limit * 3)
    )
    seen: dict[str, int] = {}
    for place, day_index in result.all():
        seen.setdefault(place, day_index)
        if len(seen) >= limit:
            break
    if not seen:
        return None
    lines: list[str] = []
    for place, day_index in seen.items():
        beat = (
            await session.execute(select(StoryBeat).where(StoryBeat.day_index == day_index))
        ).scalar_one_or_none()
        snippet = beat.winning_text[:90] if beat else ""
        lines.append(f"- «{place}»: {snippet}")
    return "\n".join(lines)


async def _load_art_anchor(session: AsyncSession) -> dict | None:
    from app.models import WatcherState
    from app.art_director import AnchorKey

    row = await session.get(WatcherState, AnchorKey)
    if row is None:
        return None
    try:
        data = json.loads(row.value)
        return data if isinstance(data, dict) and data.get("palette") else None
    except ValueError:
        return None


async def _save_art_anchor(session: AsyncSession, bible: dict) -> None:
    """Сохраняем компактный якорь библии: следующий день продолжит стиль."""
    from app.art_director import AnchorKey, compact_anchor
    from app.models import WatcherState

    anchor = compact_anchor(bible)
    if not anchor.get("palette"):
        return
    blob = json.dumps(anchor, ensure_ascii=False)[:250]
    row = await session.get(WatcherState, AnchorKey)
    if row is None:
        session.add(WatcherState(key=AnchorKey, value=blob))
    else:
        row.value = blob
    await session.commit()


def _day_bible_key(day_index: int) -> str:
    return f"art_bible:{day_index}"


async def _save_day_bible(session: AsyncSession, day_index: int, bible: dict) -> None:
    """Полная библия дня в watcher_state (Text, без лимитов).

    Обложка утром, апгрейд заглушек через четверть часа и вечерний костёр
    используют ОДНУ визуальную схему дня — без расфокуса на офлайн-палитру.
    """
    from app.models import WatcherState

    blob = json.dumps(bible, ensure_ascii=False)
    row = await session.get(WatcherState, _day_bible_key(day_index))
    if row is None:
        session.add(WatcherState(key=_day_bible_key(day_index), value=blob))
    else:
        row.value = blob
    await session.commit()


async def _load_day_bible(session: AsyncSession, day_index: int) -> dict | None:
    from app.models import WatcherState

    row = await session.get(WatcherState, _day_bible_key(day_index))
    if row is None or not row.value:
        return None
    try:
        data = json.loads(row.value)
        return data if isinstance(data, dict) and data.get("shots") else None
    except ValueError:
        return None


async def recent_repeats_block(session: AsyncSession, day_index: int, limit: int = 7) -> str | None:
    """Банк повторов: формулировки и места последних дней в промпт главы.

    Модель не должна строить сегодняшний день на дословных повторах своих же
    описаний и названий мест (окно 7 дней). None — нечего заносить в копилку.
    """
    rows = await session.execute(
        select(Round)
        .options(selectinload(Round.cards))
        .where(Round.day_index >= day_index - limit, Round.day_index < day_index)
        .order_by(Round.day_index.desc())
        .limit(limit)
    )
    lines: list[str] = []
    for round_row in rows.scalars():
        parts: list[str] = []
        if round_row.place:
            parts.append(f"место «{round_row.place}»")
        for card in sorted(round_row.cards, key=lambda c: c.position):
            snippet = (card.description or "").strip()
            if snippet:
                parts.append(f"«{card.title}»: {snippet[:90]}")
        if parts:
            lines.append("- " + "; ".join(parts))
    if not lines:
        return None
    return (
        "Банк повторов — формулировки и места, которые уже звучали в каноне "
        "последних дней. НЕ повторяй их дословно; если сцена снова ведёт стаю "
        "в уже знакомое место — покажи, что здесь изменилось, а не перескажи "
        "старое описание заново:\n"
        + "\n".join(lines)
        + "\n"
    )


# ---------- Ротация гост-блоков промпта ----------

# "promises" (книга обещаний) удалена — её место в ротации заняла линия
# Еретика: «Правила Еретика» объясняют механики мира как его изобретения.
_GUEST_POOL = ("villain", "heretic", "echoes", "focus", "places", "distant")


def guest_blocks_for(day_index: int) -> set[str]:
    """Бюджет главы: ≤4 сюжетных блока. Постоянные (закон, нрав,
    акт-рамка) не считаются. Гости ротируются парами по дню забега:
    {villain↔focus} / {heretic↔places} / {echoes↔distant} — каждая пара
    видна через день, антагонист дышит через день без давления."""
    first = day_index % len(_GUEST_POOL)
    second = (first + 3) % len(_GUEST_POOL)
    return {_GUEST_POOL[first], _GUEST_POOL[second]}


async def _plan_and_render(
    session: AsyncSession,
    day_index: int,
    opens_hint: datetime | None = None,
) -> dict:
    """Тяжёлая половина создания дня: глава, библия арта и обложка.

    Всё сетевое и медленное — здесь. Результат — лёгкий JSON-payload,
    который материализуется в раунд за миллисекунды. Глава собирается
    ОДИН раз на день и сразу с известным каноном («вчера» уже закрыт),
    поэтому итог вчерашнего выбора вплетён в начало — без прегенерации
    веток и без перерисовки обложки.
    """
    beats = await previous_beats(session)
    echoes = await collect_due_echoes(session, day_index)
    salt = secrets.token_hex(16)

    # Шрамы мира: загружаем активные и проверяем новые от вчерашнего выбора
    from app.scar_rules import load_active_scars, process_round_scars
    from app.lore import tags_from_beats

    active_scars = await load_active_scars(session, day_index)
    active_scar_keys = {s.scar_key for s in active_scars}

    # Обработка шрамов от предыдущего раунда (вчерашний winning tag)
    history_tags = tags_from_beats(beats)
    yesterday_winner_tag = None
    if history_tags:
        # Вчерашний тег = тег победившей карты за вчерашний день
        yesterday_winner_tag = history_tags[-1] if history_tags else None
        new_scars = await process_round_scars(session, yesterday_winner_tag, history_tags, day_index)
        for scar in new_scars:
            active_scar_keys.add(scar.scar_key)

    # Эмоциональный профиль: обработка от вчерашнего выбора
    from app.emotional_state import process_round_emotions, emotion_block_for_prompt

    emotion_profile = await process_round_emotions(session, yesterday_winner_tag, day_index)
    emotion_block = emotion_block_for_prompt(emotion_profile)

    # Потребности стаи: голод, жажда, здоровье
    from app.pack_state import process_round_needs, get_needs_block, check_death

    pack_needs = await process_round_needs(session, yesterday_winner_tag, day_index)
    needs_block = get_needs_block(pack_needs)

    # Проверяем смерть стаи
    if check_death(pack_needs):
        return {
            "title": "Конец",
            "text": "Стая погибла. Мир стих.",
            "cards": [],
            "image_prompt": "dark empty maze, no dogs, silence",
            "game_over": True,
        }

    # Деревья последствий: загрузка активных ветвей
    from app.consequence_trees import (
        load_active_branches, format_active_branches,
        check_tree_trigger, create_branch, advance_branch,
        CONSEQUENCE_TREES,
    )

    active_branches = await load_active_branches(session, day_index)
    branches_block = format_active_branches(active_branches)

    # Проверяем, нужно ли создать новую ветвь от вчерашнего выбора
    if beats:
        last_beat = beats[-1] if beats else ""
        existing_keys = {b.branch_key for b in active_branches}
        for tree in CONSEQUENCE_TREES.values():
            if tree.trigger_card in last_beat and tree.key not in existing_keys:
                new_branch = await create_branch(session, tree, day_index)
                active_branches.append(new_branch)
                existing_keys.add(tree.key)
        branches_block = format_active_branches(active_branches)

    # Динамические правила: определяем активные переопределения
    from app.dynamic_rules import (
        get_active_overrides, apply_overrides, get_dynamic_rule_text,
    )

    dynamic_overrides = get_active_overrides(active_scars, emotion_profile, active_branches, day_index)
    dynamic_rules_block = get_dynamic_rule_text(dynamic_overrides)

    # Сезонная рамка: арка привязана к забегу (от сброса), финал — День
    # Первого Лая на длине месяца старта забега.
    open_moment = utc_aware(opens_hint) if opens_hint is not None else _now()
    from app.season import (
        alignment_block,
        alignment_motifs,
        alignment_tints,
        anchor_axes,
        heretic_prompt_block,
        season_block as build_season_block,
    )

    anchor = await get_run_anchor(session)
    guests = guest_blocks_for(day_index)
    key = anchor["key"]
    balance = await season_tag_balance(session, key)
    prev_summary = None
    if day_index <= 2:
        prev_summary = await previous_season_summary(session, key)

    # Load AI-generated prologue beats and season arc from DB
    db_prologue_beats = None
    db_season_arc = None
    try:
        from app.prologue import load_prologue_beats_from_db
        from app.story_arc import load_season_arc_from_db
        from app.season import current_season as _current_season
        from datetime import datetime as _dt

        season_num = _current_season(anchor, open_moment)
        db_prologue_beats = await load_prologue_beats_from_db(session, season=season_num)
        db_season_arc = await load_season_arc_from_db(session, season=season_num)
    except Exception:
        pass

    sblock = build_season_block(
        anchor=anchor,
        moment=open_moment,
        balance=balance,
        previous_season_summary=prev_summary,
        db_prologue_beats=db_prologue_beats,
        db_season_arc=db_season_arc,
    )
    places_block = (
        await places_memory_block(session) if "places" in guests else None
    )
    # Призвания стаи: Ведущий может показать их одним касанием в сцене.
    from app.callings import callings_prompt_block

    callings_block = None
    try:
        callings_block = await callings_prompt_block(session)
    except Exception:
        pass
    if callings_block:
        sblock = f"{sblock}\n{callings_block}"
    # Характер стаи: определённый по голосованиям, влияет на тон повествования.
    from app.trail import trail_stats, trail_prompt_block

    try:
        # Используем агрегированную статистику по всем игрокам
        # (упрощённо: берём данные из anchor)
        trail_data = None
        if anchor and "order_axis" in anchor and "moral_axis" in anchor:
            # Конвертируем оси anchor в формат trail_stats
            order = anchor.get("order_axis", 0)
            moral = anchor.get("moral_axis", 0)
            trail_data = {
                "order": order,
                "moral": moral,
                "total": 100,  # Заглушка
                "conformity": (order + 1) / 2,
                "heart_share": (moral + 1) / 2,
                "fang_share": 0.5,
            }
        trail_block = trail_prompt_block(trail_data)
        if trail_block:
            sblock = f"{sblock}\n{trail_block}"
    except Exception:
        pass
    # Отношения NPC к стае: канон последних дней в одной строке тона.
    from app.relations import load_relations, relations_prompt_block, get_npc_titles

    try:
        npc_sentiments = await load_relations(session)
        npc_titles = await get_npc_titles(session)
        relations_block = relations_prompt_block(npc_sentiments, npc_titles=npc_titles)
    except Exception:
        npc_sentiments = {}
        relations_block = None
    if relations_block:
        sblock = f"{sblock}\n{relations_block}"
    # AI-реакции NPC: уникальные описания поведения
    try:
        from app.relations import generate_npc_reaction
        npc_reactions = []
        for npc_key, sentiment in npc_sentiments.items():
            if sentiment != 0:
                reaction = await generate_npc_reaction(
                    npc_key, sentiment,
                    recent_events=story_context if 'story_context' in dir() else "",
                    recent_choices=choices_context if 'choices_context' in dir() else "",
                )
                if reaction:
                    npc_reactions.append(reaction)
        if npc_reactions:
            sblock = f"{sblock}\nРеакции NPC: " + " ".join(npc_reactions)
    except Exception:
        pass
    # ── NPC chain-of-thought ──
    # Внутренний монолог NPC перед действием: по sentinent-ам дня.
    try:
        from app.npc_cog import generate_all_npc_cogs, npc_cogs_block

        npc_cogs = await generate_all_npc_cogs(npc_sentiments, day_index)
        cog_block = npc_cogs_block(npc_cogs)
        if cog_block:
            sblock = f"{sblock}\n{cog_block}"
    except Exception:
        logger.debug("NPC CoG не собран", exc_info=True)
    # ── Plugin prompt blocks ──
    # Плагины декларируют prompt_block capability и инжектируют данные
    # в季节ный блок. Это позволяет добавлять механики без изменения
    # основного pipeline сборки промпта.
    try:
        from app.plugins import PluginContext, registry as _plugin_registry
        from app.builtin_plugins import register_builtin_plugins

        register_builtin_plugins()
        plugin_ctx = PluginContext(session=session)
        plugin_blocks = await _plugin_registry.collect_prompt_blocks(plugin_ctx)
        for block in plugin_blocks:
            sblock = f"{sblock}\n{block}"
    except Exception:
        logger.debug("Plugin prompt blocks не собраны", exc_info=True)
    # Позиция забега нужна и линии Еретика, и серединному повороту ниже.
    from app.season import midpoint_day as season_midpoint
    from app.season import run_position as season_run_position
    from app.season import villain_stage as season_villain_stage

    run_day_now, total_now = season_run_position(anchor, open_moment)
    # Сквозная арка месяца: этапы, миссия дня, приметы Лая и лица арки.
    # Вплетается в season_block — видна и нейро-главе, и офлайн-сборке.
    # (Токен ЭТАП=N стабилен и разбирается составом лора.)
    from app.story_arc import arc_block as arc_block_builder

    try:
        sblock = f"{sblock}\n{arc_block_builder(run_day_now, total_now, key, prev_summary)}"
    except Exception:
        logger.warning("Блок арки месяца не собран (день продолжится без него)", exc_info=True)
    # Правила Еретика: вторая сюжетная линия, зеркало плана Хозяина Ошибки.
    # Идёт в season_block одним блоком (как призвания/отношения) — сигнатура
    # генератора главы не раздувается.
    if "heretic" in guests:
        try:
            heretic_block = heretic_prompt_block(
                key, season_villain_stage(run_day_now, total_now), run_day_now
            )
        except Exception:
            logger.warning("Блок Еретика не собран (день продолжится без него)", exc_info=True)
            heretic_block = None
        if heretic_block:
            sblock = f"{sblock}\n{heretic_block}"
    # План Хозяина Ошибки: продвигается по ступеням забега, канон — в промпт.
    villain = None
    if "villain" in guests:
        villain = await _villain_block(session, open_moment, anchor)

    # Серединный поворот: первый день ступени 2 — запечатанный день Середняка.
    twist = season_midpoint(run_day_now, total_now)

    # DDA: сложность зависит от engagement прошлого дня.
    prev_counts, prev_stakes, prev_voters = await _previous_round_stats(session)
    from app.difficulty import compute_difficulty_metrics, select_win_rule

    prev_metrics = compute_difficulty_metrics(
        counts=prev_counts,
        total_stakes=prev_stakes,
        player_count=prev_voters,
    )
    if twist:
        rule = WinRule.MEDIAN
    else:
        rule = WinRule(
            select_win_rule(
                prev_metrics,
                day_index,
                is_sealed=sealed_day(day_index) or False,
                is_midpoint=twist,
                seed=day_index,
            )
        )

    # Дальняя память мира: из давнего канона (старше окна) достаём дни,
    # сюжетно похожие на настоящее, — мир вспоминает собственную историю.
    canon_rows = await session.execute(
        select(StoryBeat).order_by(StoryBeat.day_index.asc())
    )
    canon = [
        f"{beat.winning_title}: {beat.winning_text}"
        for beat in canon_rows.scalars()
    ]
    query_parts = [beats[-1] if beats else "", *(echo.title for echo in echoes)]
    distant = recall_beats(canon, query=" ".join(filter(None, query_parts)))

    run_salt = secrets.token_hex(4)
    order_axis, moral_axis = anchor_axes(anchor)
    # Фокус-день NPC (каждый третий день забега).
    try:
        from app.relations import npc_focus_line_ai, get_npc_titles
        from app.season import run_position as _run_pos

        run_day_now, _total_now = _run_pos(anchor, open_moment)
        npc_titles = await get_npc_titles(session)
        focus_line = (
            await npc_focus_line_ai(
                run_day_now,
                relations=npc_sentiments,
                npc_titles=npc_titles,
            )
            if "focus" in guests
            else None
        )
    except Exception:
        focus_line = None
    # AI World Engine: блок персонажей для промпта
    characters_block = ""
    try:
        from app.story import _build_dynamic_character_block
        characters_block = await _build_dynamic_character_block(session)
    except Exception:
        pass
    # Банк повторов: формулировки и места последних дней — модель не должна
    # дублировать их дословно (литературный де-дуп, окно 7 дней).
    repeat_block = await recent_repeats_block(session, day_index)
    chapter = await generate_chapter(
        day_index, beats, rule,
        echoes=echoes if "echoes" in guests else [],
        distant_echoes=distant if "distant" in guests else [],
        season_block=sblock, places_block=places_block,
        villain_block=villain, sealed=sealed_day(day_index) or twist,
        salt=run_salt,
        alignment_block=alignment_block(order_axis, moral_axis),
        tint_lines=alignment_tints(order_axis, moral_axis, salt=run_salt),
        focus_line=focus_line,
        repeat_block=repeat_block,
        active_scar_keys=active_scar_keys,
        emotion_block=emotion_block,
        branches_block=branches_block,
        dynamic_rules_block=dynamic_rules_block,
        needs_block=needs_block,
        characters_block=characters_block,
        is_expanded=day_index == 1 or twist,
    )

    # Арт-директор: визуальный план дня, затем промпты каждого кадра.
    # Якорь предыдущего дня держит сериальность палитры и мотивов.
    echo_motifs = sorted({_ECHO_ART_MOTIFS.get(echo.kind, "portal hum haze") for echo in echoes})
    # Сквозные персонажи главы получают стабильные визуальные дескрипторы:
    # Лайнер в кадре сегодня и через неделю — одна и та же фигура.
    hero_motifs = character_motifs_for(f"{chapter.get('title', '')} {chapter.get('text', '')}")
    # Нрав стаи красит кадр: квадрант характера добавляет свой мотив первым.
    align_motifs = alignment_motifs(order_axis, moral_axis)
    anchor = await _load_art_anchor(session)
    bible = await plan_day_art(
        chapter, beats, anchor=anchor,
        extra_motifs=sorted(set(echo_motifs + hero_motifs + align_motifs)),
    )
    bible["motifs"] = align_motifs + [m for m in (bible.get("motifs") or []) if m not in align_motifs]
    await _save_art_anchor(session, bible)
    # Полная библия дня в watcher_state: апгрейд заглушек и вечерний костёр
    # дорисовываются по ТОЙ ЖЕ визуальной схеме, что и обложка утром.
    await _save_day_bible(session, day_index, bible)

    media_root = Path(settings.media_dir)
    cover_path = media_root / f"day{day_index}_cover.jpg"
    day_seed = 10_000 + day_index * 7

    # AI World Engine: генерируем нового NPC (1 раз в день)
    try:
        from app.story import _generate_session_characters
        needs_dict = {
            "hunger": pack_needs.hunger,
            "thirst": pack_needs.thirst,
            "health": pack_needs.health,
        }
        char_result = await _generate_session_characters(session, day_index, needs_dict, season=key)
        if char_result:
            logger.info("AIWorldEngine: %s", char_result)
    except Exception as e:
        logger.debug("AIWorldEngine: генерация персонажей не удалась: %s", e)

    # AI World Engine: получаем NPC для генерации обложки
    try:
        from app.models import WorldCharacter
        stmt = select(WorldCharacter).where(
            WorldCharacter.is_alive == True,
            WorldCharacter.last_seen_day >= day_index - 1,
        )
        ai_chars = (await session.execute(stmt)).scalars().all()
        if ai_chars:
            chapter["ai_characters"] = [
                {"name": c.name, "mood": c.mood, "trust": c.trust_stay, "role": c.role}
                for c in ai_chars
            ]
    except Exception as e:
        logger.debug("AIWorldEngine: запрос NPC для обложки не удался: %s", e)

    # AI World Engine: создаём контекст мира ОДИН раз для локаций и выборов
    world_ctx = None
    try:
        from app.world_engine import get_world_context
        needs_dict = {
            "hunger": pack_needs.hunger,
            "thirst": pack_needs.thirst,
            "health": pack_needs.health,
        }
        world_ctx = await get_world_context(session, day_index, needs_dict, season=key)
    except Exception as e:
        logger.debug("AIWorldEngine: не удалось собрать контекст мира: %s", e)

    # AI World Engine: пытаемся использовать AI-локацию
    try:
        from app.world_engine import get_or_create_location, update_location_visit
        from app.story import _chat_completion

        if world_ctx:
            ai_location = await get_or_create_location(session, world_ctx, _chat_completion)

            if ai_location:
                # Переопределяем локацию главы
                chapter["place"] = ai_location.name
                # Обновляем описание места в тексте главы
                if ai_location.description:
                    # Добавляем описание локации в начало текста
                    chapter["text"] = f"{ai_location.description}\n\n{chapter['text']}"
                # Передаём atmosphere и scene для генерации обложки
                if ai_location.atmosphere:
                    chapter["atmosphere"] = ai_location.atmosphere
                if ai_location.scene:
                    chapter["location_scene"] = ai_location.scene
                # Обновляем статистику посещения
                await update_location_visit(session, ai_location.name, day_index)
                logger.info("AIWorldEngine: использована AI-локация '%s' для дня %d", ai_location.name, day_index)
    except Exception as e:
        logger.warning("AIWorldEngine: ошибка генерации AI-локации: %s", e)

    # Сид обложки привязан к месту дня: возвращение в «Старый приют»
    # рисует тот же мир, а не новую случайную сцену.
    cover_seed = place_seed_for(chapter.get("place")) or day_seed
    # Предыдущая облока для dedup
    prev_cover_path = None
    if day_index > 1:
        prev_cover = media_root / f"day{day_index - 1}_cover.jpg"
        if prev_cover.exists():
            prev_cover_path = prev_cover
    # ОДИН кадр дня: обложка «мир после вчерашнего выбора». Пути голосования
    # остаются текстом и кнопками — залп из четырёх генераций бил free-лимиты
    # (429), и три карты из четырёх уходили в PIL-заглушки.
    fetched_cover = await fetch_day_image(
        build_image_prompt(bible, "cover", seed=cover_seed),
        short_image_prompt(bible, "cover", seed=cover_seed),
        cover_path,
        seed=cover_seed,
        width=1280,
        height=720,
        prev_cover_path=prev_cover_path,
    )
    if not fetched_cover:
        # PIL-рендер синхронный и тяжёлый — уводим из event loop.
        await asyncio.to_thread(render_cover, cover_path, chapter["title"], chapter["text"])
    # Стартовый кадр мира: один раз на забег (день 1). Падение молчит —
    # пост дня не зависит от него, файл переиспользуется /start и анонсами.
    if day_index == 1:
        from app.art_director import build_intro_prompt, build_intro_short_prompt

        intro_path = media_root / "run_intro.jpg"
        await fetch_day_image(
            build_intro_prompt(bible, seed=day_seed),
            build_intro_short_prompt(bible),
            intro_path,
            seed=day_seed + 9_000,
            width=1280,
            height=720,
        )
    cards_payload = []
    # AI World Engine: генерируем AI-выборы вместо фиксированных карт
    try:
        from app.world_engine import generate_ai_choices, record_choice
        from app.story import _chat_completion

        if world_ctx:
            ai_choices = await generate_ai_choices(session, world_ctx, _chat_completion)

        if ai_choices and len(ai_choices) >= 3:
            # Используем AI-выборы
            for position, choice in enumerate(ai_choices[:3]):
                cards_payload.append(
                    {
                        "position": position,
                        "title": choice.title,
                        "description": choice.description,
                        "consequence": choice.consequence,
                        "tag": choice.tag,
                        "image_path": "",
                        "food_cost": choice.food_cost,
                        "water_cost": choice.water_cost,
                        "health_risk": choice.health_risk,
                        "trust_change": choice.trust_change,
                        "emotional_consequence": choice.emotional_consequence,
                        "npc_reactions_json": json.dumps(choice.npc_reactions, ensure_ascii=False),
                    }
                )
            logger.info("AIWorldEngine: сгенерированы AI-выборы для дня %d", day_index)
        else:
            # Фолбэк на карты из главы (если AI вернул карточки) или офлайн-пул
            chapter_cards = chapter.get("cards") or []
            if not chapter_cards:
                # AI не вернул карточки — генерируем из офлайн-пула
                from app.lore import _cards
                import secrets as _secrets
                rng = _secrets.SystemRandom()
                offline_cards = _cards(rng, day_index)
                chapter_cards = [
                    {"title": c[0], "description": c[1], "consequence": c[2], "tag": tag}
                    for c, tag in zip(offline_cards, ["risk", "care", "cunning"])
                ]
            for position, card in enumerate(chapter_cards[:3]):
                cards_payload.append(
                    {
                        "position": position,
                        "title": card["title"],
                        "description": card["description"],
                        "consequence": card["consequence"],
                        "tag": card.get("tag", "care"),
                        "image_path": "",
                    }
                )
            logger.info("AIWorldEngine: фолбэк на карты для дня %d", day_index)
    except Exception as e:
        logger.warning("AIWorldEngine: ошибка генерации AI-выборов: %s", e)
        # Фолбэк на карты из главы или офлайн-пул
        chapter_cards = chapter.get("cards") or []
        if not chapter_cards:
            from app.lore import _cards
            import secrets as _secrets
            rng = _secrets.SystemRandom()
            offline_cards = _cards(rng, day_index)
            chapter_cards = [
                {"title": c[0], "description": c[1], "consequence": c[2], "tag": tag}
                for c, tag in zip(offline_cards, ["risk", "care", "cunning"])
            ]
        for position, card in enumerate(chapter_cards[:3]):
            cards_payload.append(
                {
                    "position": position,
                    "title": card["title"],
                    "description": card["description"],
                    "consequence": card["consequence"],
                    "tag": card.get("tag", "care"),
                    "image_path": "",
                }
            )
    return {
        "v": PREPARED_PAYLOAD_VERSION,
        "day_index": day_index,
        "rule": rule.value,
        "commitment": commit_rule(rule, salt) + ":" + salt,
        "sealed": sealed_day(day_index) or twist,
        "chapter_title": chapter["title"],
        "chapter_text": chapter["text"],
        "lore_summary": chapter["lore_summary"],
        "place": chapter.get("place"),
        "season": key,
        "cover_path": str(cover_path),
        "cards": cards_payload,
    }


def _payload_cards(payload: dict) -> list[dict]:
    cards = [dict(card) for card in payload.get("cards") or []]
    for position, card in enumerate(cards):
        card.setdefault("position", position)
        card.setdefault("tag", "care")
    return cards


async def _materialize_round(
    session: AsyncSession, payload: dict, latest: Round | None
) -> Round:
    """Быстрая половина: раунд и карты из готового payload. Только БД."""
    day_index = int(payload["day_index"])
    now = _now()
    opens_at = now if latest is None or latest.tally_ends_at is None else max(now, utc_aware(latest.tally_ends_at))
    voting_ends_at, tally_ends_at = _day_window(opens_at)
    round_row = Round(
        day_index=day_index,
        status=RoundStatus.OPEN,
        win_rule=WinRule(payload["rule"]),
        rule_commitment=payload["commitment"],
        sealed=bool(payload.get("sealed", False)),
        chapter_title=payload["chapter_title"],
        chapter_text=payload["chapter_text"],
        lore_summary=payload["lore_summary"],
        season=payload.get("season") or season_key(opens_at),
        place=payload.get("place"),
        cover_path=payload.get("cover_path", ""),
        opens_at=opens_at,
        voting_ends_at=voting_ends_at,
        tally_ends_at=tally_ends_at,
    )
    for card in _payload_cards(payload):
        # Коллекция наполняется до session.add: на транзиентном объекте это
        # чистый Python без ленивой загрузки, а FK проставит каскад на flush.
        # Возвращённый раунд отдаёт .cards сразу — без обращений к БД.
        round_row.cards.append(Card(**card))
    session.add(round_row)
    await session.flush()
    # Бестиарий: маска закона дня и (со 2-й ступени) Администратор —
    # по записи за сезон, идемпотентно.
    from app.bestiary import note_round as bestiary_note_round

    try:
        # Формируем контекст для AI-генерации описаний бестиария
        story_ctx = ""
        choices_ctx = ""
        if payload.get("chapter_text"):
            story_ctx = payload["chapter_text"][:1000]
        if payload.get("cards"):
            choices_lines = []
            for card in payload["cards"][:3]:
                title = card.get("title", "")
                desc = card.get("description", "")
                choices_lines.append(f"- {title}: {desc}")
            choices_ctx = "\n".join(choices_lines)

        await bestiary_note_round(
            session,
            round_row,
            season_key_value=payload.get("season"),
            llm_caller=_chat_completion if story_ctx else None,
            story_context=story_ctx,
            choices_context=choices_ctx,
        )
    except Exception:
        logger.warning("Запись бестиария дня %s не удалась", day_index, exc_info=True)
    return round_row


# Визуальная фактура типов эхов: содержание скрыто, фактура повторяется —
# внимательный игрок учится узнавать класс следа по кадру дня.
_ECHO_ART_MOTIFS = {
    "угроза": "ominous burnt-wire glow in the fog",
    "память": "a warm amber keepsake bowl catching light",
    "обман": "a mirage-like silhouette of an unfamiliar dog",
}

# Театр жребия: реплики к честному броску при ничьей (детерминированы сидом).
_TIE_THEATER = (
    "Кость архива стукнула о дно урны: путь {chosen}.",
    "Жребий запечатанного счёта лёг на {paths} — и указал {chosen}.",
    "Дневник перевернул страницу дважды; выпало {chosen}.",
)


# Формат payload'а дня. v4: инлайн-день — глава, карты и обложка рендерятся
# сразу целиком (раньше флажок-версия отличал заготовку трёх веток, собранную
# до вскрытия итогов; прегенерация убрана). Маркер остался как паспорт формата
# «свершившегося дня» для материализации.
PREPARED_PAYLOAD_VERSION = 4


def place_seed_for(place: str | None) -> int | None:
    """Стабильный сид обложки по названию места: возвращение стаи в место
    выглядит как возвращение — тот же кадр-база, а не новый случайный мир."""
    if not place or not place.strip():
        return None
    return 10_000 + zlib.crc32(place.strip().lower().encode("utf-8")) % 890_000


# ---------- Отложенный апгрейд PIL-заглушек картинок ----------


def _stubs_key(day_index: int) -> str:
    return f"img_stubs:{day_index}"


async def _record_image_stubs(
    session: AsyncSession, day_index: int, cover_stub: bool, card_positions: list[int]
) -> None:
    from app.models import WatcherState

    import time as _time

    payload = json.dumps(
        {
            "cover": cover_stub,
            "cards": sorted(card_positions),
            "ts": int(_time.time()),
        }
    )
    key = _stubs_key(day_index)
    row = await session.get(WatcherState, key)
    if row is None:
        session.add(WatcherState(key=key, value=payload))
    else:
        row.value = payload
    await session.commit()


async def _pop_image_stubs(session: AsyncSession, day_index: int) -> dict | None:
    from app.models import WatcherState

    key = _stubs_key(day_index)
    row = await session.get(WatcherState, key)
    if row is None or not row.value:
        return None
    try:
        data = json.loads(row.value)
    except ValueError:
        data = None
    await session.delete(row)
    await session.commit()
    return data if isinstance(data, dict) else None


async def upgrade_stub_images(day_index: int) -> int:
    """Вторая попытка для кадров, ушедших в PIL-заглушку (429/таймауты).

    Запускается через ~15 минут после анонса дня: пик троттлинга обычно
    спадает, и кадры дорисовываются нейромоделью — /today и будущие
    пересылки показывают уже полноценные картинки. Возвращает число
    обновлённых кадров.
    """
    from sqlalchemy.orm import selectinload

    from app.art_director import build_image_prompt, offline_bible, short_image_prompt
    from app.db import SessionLocal
    from app.story import fetch_day_image

    async with SessionLocal() as session:
        stubs = await _pop_image_stubs(session, day_index)
        round_row = (
            await session.execute(
                select(Round).options(selectinload(Round.cards)).where(Round.day_index == day_index).limit(1)
            )
        ).scalar_one_or_none()
    if round_row is None or not stubs:
        return 0
    media_root = Path(settings.media_dir)
    chapter_like = {
        "title": round_row.chapter_title,
        "text": round_row.chapter_text,
        "cards": [
            {"title": card.title, "image_prompt": ""}
            for card in sorted(round_row.cards, key=lambda item: item.position)
        ],
    }
    # Библия дня из watcher_state: заглушка дорисовывается по ТОЙ ЖЕ схеме,
    # что и утренняя обложка. Bibliи нет (обрыв после прегенерации) — офлайн.
    async with SessionLocal() as session:
        bible = await _load_day_bible(session, day_index) or offline_bible(chapter_like)
    day_seed = 10_000 + day_index * 7
    cover_seed = place_seed_for(round_row.place) or day_seed
    semaphore = asyncio.Semaphore(1)

    async def _pull(slot: str, prompt: str, short: str, dest: Path, seed: int) -> bool:
        async with semaphore:
            return await fetch_day_image(prompt, short, dest, seed=seed)

    jobs = []
    if stubs.get("cover"):
        jobs.append(
            (
                "cover",
                media_root / f"day{day_index}_cover.jpg",
                build_image_prompt(bible, "cover", seed=cover_seed),
                short_image_prompt(bible, "cover", seed=cover_seed),
                cover_seed,
            )
        )
    cards_by_pos = {card.position: card for card in round_row.cards}
    for position in stubs.get("cards", []):
        dest = media_root / f"day{day_index}_card{position}.jpg"
        _card = cards_by_pos.get(position)
        jobs.append(
            (
                str(position),
                dest,
                build_image_prompt(bible, str(position), seed=day_seed + position + 1),
                short_image_prompt(bible, str(position), seed=day_seed + position + 1),
                day_seed + position + 1,
            )
        )

    upgraded = 0
    remaining_cover = bool(stubs.get("cover"))
    remaining_cards: list[int] = []
    results = await asyncio.gather(*(_pull(slot, p, s, d, sd) for slot, d, p, s, sd in jobs))
    for (slot, _dest, _p, _s, _sd), ok in zip(jobs, results):
        if ok:
            upgraded += 1
            continue
        if slot == "cover":
            remaining_cover = True
        else:
            remaining_cards.append(int(slot))
    if remaining_cover or remaining_cards:
        async with SessionLocal() as session:
            await _record_image_stubs(session, day_index, remaining_cover, remaining_cards)
    logger.info("Апгрейд заглушек дня %d: перерисовано %d из %d", day_index, upgraded, len(jobs))
    return upgraded


async def _stamp_day_money_mode(session: AsyncSession, round_row: Round) -> None:
    """Снимает режим «версии игры» на открытие дня.

    Хранитель может переключить рубильник посреди текущего дня (из /panel);
    чтобы ставки/смена не «прыгали» на лету, решение фиксируется в момент
    материализации дня: новый день берёт актуальный режим, а уже открытый —
    живёт по своему снимку (см. Round.money_mode).
    """
    from app.ops import money_mode_enabled

    round_row.money_mode = bool(await money_mode_enabled(session))


async def create_next_round_detailed(
    session: AsyncSession, base_day_index: int | None = None
) -> tuple[Round, bool]:
    """Создаёт следующий день. Второе значение — был ли день создан сейчас.

    День рендерится сразу целиком (план → рендер → материализация), один раз,
    по известному канону вчера. Авто-материализации во время тика vs финализации
    защищены ранним выходом из гонки ниже и IntegrityError-хэндлингом.

    base_day_index — если задан, открывается именно день base_day_index + 1
    (а не latest.day_index + 1). Так финализация закрытого дня N открывает N+1
    детерминированно: если тик уже создал N+1, возвращаем его, а не эскалируем
    в N+2 (иначе «двойной день» — прыжок вперёд и потерянные итоги N+1).
    """
    latest = await get_latest_round(session)
    target_day = (
        base_day_index + 1
        if base_day_index is not None
        else (1 if latest is None else latest.day_index + 1)
    )
    # Ранний выход из гонки: нужный день уже открыт — отдаём его без рендера.
    already = (
        await session.execute(select(Round).where(Round.day_index == target_day).limit(1))
    ).scalar_one_or_none()
    if already is not None:
        return already, False
    # Остатки старой двофазной прегенерации (до релиза инлайн-дней) — чистим,
    # чтобы открытый сегодня день не перезаписался заготовкой вчерашней ночи.
    stale = await session.get(PreparedDay, target_day)
    if stale is not None:
        await session.delete(stale)
        await session.commit()

    day_index = target_day
    opens_hint = (
        max(_now(), utc_aware(latest.tally_ends_at))
        if latest is not None and latest.tally_ends_at is not None
        else None
    )
    payload = await _plan_and_render(session, day_index, opens_hint=opens_hint)
    try:
        round_row = await _materialize_round(session, payload, latest)
        await _stamp_day_money_mode(session, round_row)
    except IntegrityError:
        await session.rollback()
        existing = await get_latest_round(session)
        if existing is None:
            raise
        return existing, False
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await get_latest_round(session)
        if existing is None:
            raise
        return existing, False
    return round_row, True


async def create_next_round(session: AsyncSession, base_day_index: int | None = None) -> Round:
    row, _created = await create_next_round_detailed(session, base_day_index=base_day_index)
    return row


async def reset_game(session: AsyncSession, keep_story: bool = False) -> Round:
    """Сброс игры: чистые счёты и первый день заново.

    Стираются дни, карты, голоса, ставки, выплаты, эхо и канон; счёт игроков
    обнуляется. Кошельки, привязки чатов и копилка месяца не трогаются — это
    реальные обязательства казны, а не «результаты».

    keep_story=True — «разделить команды»: статистика и деньги обнуляются,
    но канон истории (StoryBeat) и эхо остаются, и новый первый день
    продолжает тот же мир с памятью о прошлом.

    Защита: пока в очереди есть хоть одна неотправленная выплата, сброс
    запрещён — иначе обнуление стёрло бы реальные денежные обязательства
    казны. Сначала разгреби очередь (автоциклы или вручную).
    """
    owed = await pending_payout_count(session)
    if owed:
        raise RuntimeError(
            f"Сброс запрещён: в очереди {owed} неотправленных выплат. "
            "Дождись автоплатежей или разбери зависшие вручную."
        )
    await session.execute(delete(Payout))
    await session.execute(delete(Stake))
    await session.execute(delete(Vote))
    await session.execute(delete(RevoteGrant))
    # Леджер доходов ссылается на дни FK (инцидент: DELETE FROM rounds падал
    # ForeignKeyViolation по incomes_round_id_fkey, и сброс молча откатывался
    # целиком). Память сети — личный счётчик по дням, тоже обнуляется.
    await session.execute(delete(Income))
    await session.execute(delete(MemoryHit))
    await session.execute(delete(Card))
    # Заготовки и визуальные библии старого канона больше не имеют силы:
    # мир переписан заново.
    await session.execute(delete(PreparedDay))
    await session.execute(delete(WatcherState).where(WatcherState.key.like("art_bible:%")))
    if not keep_story:
        await session.execute(delete(StoryBeat))
        await session.execute(delete(LoreEcho))
        # Полный сброс стирает и план Хозяина Ошибки: новый мир — новый план.
        from app.season import VILLAIN_KEY

        await session.execute(
            delete(WatcherState).where(WatcherState.key == VILLAIN_KEY)
        )
    await session.execute(delete(Round))
    await session.execute(update(Player).values(score=0, correct_picks=0))
    # Новый забег: якорь арки стартует сегодня — акты и план злодея
    # считаются от сброса, а не от календаря.
    from app.models import WatcherState as WS
    from app.season import RUN_START_KEY, default_anchor

    fresh_anchor = default_anchor(_now())
    payload = json.dumps(fresh_anchor, ensure_ascii=False)
    row = await session.get(WS, RUN_START_KEY)
    if row is None:
        session.add(WS(key=RUN_START_KEY, value=payload))
    else:
        row.value = payload
    await session.commit()
    row, _created = await create_next_round_detailed(session)
    return row


async def claim_announcement(session: AsyncSession, round_row: Round) -> bool:
    """Занимает право объявить день: True только для первого вызывающего.

    Условный UPDATE по пустому announced_at защищает от двойного поста,
    когда после деплоя секунду живут два процесса бота.
    """
    result = await session.execute(
        update(Round)
        .where(Round.id == round_row.id, Round.announced_at.is_(None))
        .values(announced_at=_now())
    )
    await session.commit()
    return result.rowcount > 0


async def ensure_current_round(session: AsyncSession) -> Round:
    current = await get_active_round(session)
    if current is not None:
        return current
    latest = await get_latest_round(session)
    if latest is not None and latest.status != RoundStatus.CLOSED:
        return latest
    if latest is not None and utc_aware(latest.tally_ends_at) > _now():
        return latest
    return await create_next_round(session)


async def heal_stale_rounds(session: AsyncSession) -> int:
    """Дочитывает дни, застрявшие не-закрытыми ПОЗАДИ актуального.

    Инцидент: после сбоя доставки анонса день остался в TALLYING, а следующий
    уже открылся — тик обрабатывает только актуальный день, и застрявший
    висел вечно: без подсчёта, без канона, с замороженными ставками.

    Для каждого такого дня по возрастанию: закрытие голосования → подсчёт →
    очки → эпилог. Финализация ставок отдельного вызова не требует: как
    только день получает CLOSED, его подхватывает settle_closed_rounds
    (цикл обслуживания ≤2 мин). Возвращает число вылеченных дней.
    """
    latest = await get_latest_round(session)
    if latest is None:
        return 0
    result = await session.execute(
        select(Round)
        .options(selectinload(Round.cards))
        .where(
            Round.day_index < latest.day_index,
            Round.status.in_([RoundStatus.OPEN, RoundStatus.TALLYING]),
        )
        .order_by(Round.day_index.asc())
    )
    stale = list(result.scalars())
    healed = 0
    from app.tally import award_points

    for round_row in stale:
        day = round_row.day_index
        try:
            if round_row.status == RoundStatus.OPEN:
                await close_voting(session, round_row)
            finished, closed_here = await finish_tally(session, round_row)
            if closed_here:
                await award_points(session, finished)
                # Финализация ставок: heal сам дёр finalize_day_payouts, а не
                # полагается на settle_closed_rounds (который может не работать
                # если ton_enabled=False при старте).
                from app.stakes import finalize_day_payouts
                try:
                    await finalize_day_payouts(session, finished)
                except Exception:
                    logger.warning("Финализация ставок вылеченного дня %s упала", day, exc_info=True)
                try:
                    await write_epilogue(session, finished)
                except Exception:
                    logger.warning("Эпилог вылеченного дня %s не удался", day, exc_info=True)
                healed += 1
                logger.info("Вылечен застрявший день %s: подсчёт завершён, ставки финализированы", day)
        except Exception as exc:
            logger.exception("Лечение застрявшего дня %s не удалось (повторится)", day, exc)
            await session.rollback()
    return healed


def public_round_view(round_row: Round) -> dict:
    """Counts stay secret while the round is open; the law is public from the start.

    В глухой день закон скрыт даже из view: наружу уходит только флаг sealed.
    """
    sealed = bool(getattr(round_row, "sealed", False))
    view = {
        "day_index": round_row.day_index,
        "status": round_row.status.value,
        "title": round_row.chapter_title,
        "text": round_row.chapter_text,
        "win_rule": None if sealed else round_row.win_rule.value,
        "sealed": sealed,
        "voting_ends_at": round_row.voting_ends_at,
        "tally_ends_at": round_row.tally_ends_at,
        "cards": [
            {
                "position": card.position,
                "title": card.title,
                "description": card.description,
                "image_path": card.image_path,
            }
            for card in sorted(round_row.cards, key=lambda item: item.position)
        ],
    }
    if round_row.status == RoundStatus.CLOSED:
        view["winner_card"] = round_row.winner_card
        view["vote_counts"] = json.loads(round_row.vote_counts_json or "{}")
        view["lore_summary"] = round_row.lore_summary
    return view


async def count_votes_for_tally(session: AsyncSession, round_id: int) -> dict[int, int]:
    """Allowed only from the tally job. One GROUP BY, O(n) scan of the day partition."""
    result = await session.execute(
        select(Vote.card_position, func.count())
        .where(Vote.round_id == round_id)
        .group_by(Vote.card_position)
    )
    counts = {0: 0, 1: 0, 2: 0}
    for position, total in result.all():
        counts[int(position)] = int(total)
    bonus = getattr(settings, "stake_vote_bonus_weight", 0) or 0
    if bonus > 0:
        # «Кожа в игре»: путь, за который хотя бы один игрок держит
        # подтверждённую ставку TON, получает плоский перевес. Ставка
        # привязывается к пути через голос самого игрока (Stake не хранит
        # позицию отдельно). Только confirmed — деньги реально заблокированы.
        stake_rows = await session.execute(
            select(Vote.card_position, func.count(distinct(Vote.player_id)))
            .join(
                Stake,
                (Stake.round_id == Vote.round_id)
                & (Stake.player_id == Vote.player_id),
            )
            .where(Vote.round_id == round_id, Stake.status == "confirmed")
            .group_by(Vote.card_position)
        )
        for position, holders in stake_rows.all():
            if holders > 0:
                counts[int(position)] += bonus
    return counts


def tied_positions(counts: dict[int, int], rule: WinRule) -> list[int]:
    """Все пути, претендующие на победу по закону дня (без учёта позиций)."""
    items = [(counts.get(i, 0), i) for i in range(3)]
    if rule is WinRule.MAJORITY:
        best = max(item[0] for item in items)
        return sorted(i for total, i in items if total == best)
    if rule is WinRule.MINORITY:
        worst = min(item[0] for item in items)
        return sorted(i for total, i in items if total == worst)
    ordered = sorted(items, key=lambda item: (item[0], item[1]))
    median = ordered[1][0]
    return sorted(i for total, i in items if total == median)


def pick_winner(counts: dict[int, int], rule: WinRule, seed: str | None = None) -> int:
    """Победитель по закону дня. Без seed — детерминированный fallback
    (меньший номер пути); с seed — честный жребий, посеянный утренним
    обязательством дня, чтобы ничья не решалась «номером карты»."""
    candidates = tied_positions(counts, rule)
    if len(candidates) > 1 and seed:
        return random.Random(f"law:{seed}").choice(candidates)
    return candidates[0]


async def _staked_paths(session: AsyncSession, round_id: int) -> set[int]:
    """Пути дня, за которые есть хотя бы один подтверждённый ставщик."""
    rows = await session.execute(
        select(Vote.card_position, func.count(distinct(Vote.player_id)))
        .join(
            Stake,
            (Stake.round_id == Vote.round_id) & (Stake.player_id == Vote.player_id),
        )
        .where(Vote.round_id == round_id, Stake.status == "confirmed")
        .group_by(Vote.card_position)
    )
    return {int(p) for p, holders in rows.all() if holders > 0}


def _pick_among(
    counts: dict[int, int], rule: WinRule, seed: str | None, paths: list[int]
) -> tuple[int, list[int]]:
    """(победитель, претенденты) по закону ДНЯ только внутри заданного набора
    путей. В отличие от pick_winner, «не заявленные» пути не существуют —
    отсутствующий путь не трактуется как 0 голосов и не лезет в MINORITY-минимум."""
    items = [(counts.get(p, 0), p) for p in paths]
    if not items:
        return 0, []
    if rule is WinRule.MAJORITY:
        ref = max(c for c, _ in items)
    elif rule is WinRule.MINORITY:
        ref = min(c for c, _ in items)
    else:  # MEDIAN
        ordered = sorted(items, key=lambda t: (t[0], t[1]))
        ref = ordered[len(ordered) // 2][0]
    cands = sorted(p for c, p in items if c == ref)
    if len(cands) > 1 and seed:
        winner = random.Random(f"law:{seed}").choice(cands)
    else:
        winner = cands[0]
    return winner, cands


def _prefer_staked(
    counts: dict[int, int], rule: WinRule, seed: str | None, staked: set[int]
) -> tuple[int, list[int]]:
    """(победитель, претенденты) с приоритетом ставящих.

    Если хотя бы один путь реально заблокирован ставкой TON, путь, за который
    НИКТО не держит деньги, не может победить: закон пересчитывается строго
    по ставящим путям. Это лечит MINORITY-патологию — там побеждает наименьший
    счёт, и голос против «пустого» пути мог бы случайно выиграть; отсекаем
    безденежные кандидатов до выбора. При отсутствии ставящих — исход по
    прежнему чисто-подсчётному закону целиком.
    """
    if not staked:
        return pick_winner(counts, rule, seed=seed), tied_positions(counts, rule)
    return _pick_among(counts, rule, seed, sorted(staked))


async def _winner_and_tied(
    session: AsyncSession,
    round_row: Round,
    counts: dict[int, int],
    seed: str,
) -> tuple[int, list[int]]:
    """Выбор победителя с необязательным приоритетом ставящих (win_rule_prefers_staked)."""
    if getattr(settings, "win_rule_prefers_staked", False):
        staked = await _staked_paths(session, round_row.id)
        return _prefer_staked(counts, round_row.win_rule, seed=seed, staked=staked)
    return pick_winner(counts, round_row.win_rule, seed=seed), tied_positions(
        counts, round_row.win_rule
    )


async def _echoes_already_spawned(session: AsyncSession, day_index: int) -> bool:
    result = await session.execute(
        select(func.count())
        .select_from(LoreEcho)
        .where(LoreEcho.born_day == day_index)
    )
    return bool(result.scalar_one())


async def close_voting(session: AsyncSession, round_row: Round) -> Round:
    if round_row.status != RoundStatus.OPEN:
        return round_row
    round_row.status = RoundStatus.TALLYING
    # Неиспользованные гранты смены пути сгорают вместе с днём (политика:
    # оплата действует только пока день открыт; возврат — вручную).
    await session.execute(
        update(RevoteGrant)
        .where(RevoteGrant.round_id == round_row.id, RevoteGrant.status == "granted")
        .values(status="expired")
    )
    await session.commit()
    # Следы дня рождаются сразу при закрытии голосования, а не в конце часа
    # подсчёта: следующий день рендерится уже после вскрытия итогов, и эхо
    # победителя (earliest_day=завтра) обязано существовать к моменту старта
    # генерации, иначе оно системно всплывало бы на день позже замысла. Исход
    # дня при этом не раскрывается: ни StoryBeat, ни счётчики не публикуются.
    counts = await count_votes_for_tally(session, round_row.id)
    seed = f"{round_row.rule_commitment}:{round_row.day_index}"
    round_row.winner_card, _ = await _winner_and_tied(session, round_row, counts, seed)
    if not await _echoes_already_spawned(session, round_row.day_index):
        # Свежая копия с картами (selectinload): у переданного объекта карты
        # могут быть не загружены — ленивый доступ вне greenlet запрещён.
        loaded = await get_round(session, round_row.id)
        source = loaded if loaded is not None else round_row
        source.winner_card = round_row.winner_card
        spawn_echoes_from_round(session, source)
    await session.commit()
    return round_row


async def finish_tally(session: AsyncSession, round_row: Round) -> tuple[Round, bool]:
    """Закрывает подсчёт атомарно. Второе значение — закрыт ли день этим вызовом.

    Условный UPDATE (status='tallying' → 'closed') защищает от гонки между
    планировщиком и /advance: проигравший вызов не начисляет очки повторно
    и не дублирует StoryBeat.
    """
    if round_row.status != RoundStatus.TALLYING:
        loaded = await get_round(session, round_row.id)
        return (loaded or round_row), False
    counts = await count_votes_for_tally(session, round_row.id)
    # Жребий сеется утренним обязательством: игроки не могут знать исход
    # ничьей заранее, но после вскрытия обязательства результат проверяем.
    seed = f"{round_row.rule_commitment}:{round_row.day_index}"
    winner, tied = await _winner_and_tied(session, round_row, counts, seed)
    tie_note: str | None = None
    if len(tied) > 1:
        # Театр жребия: детерминированная реплика к честному броску.
        theater = _TIE_THEATER[
            int(seed[-1], 16) % len(_TIE_THEATER)
        ].format(paths=" и ".join(_ROMAN[p] for p in tied), chosen=_ROMAN[winner])
        tie_note = (
            f"Голоса разделились ({' и '.join(_ROMAN[p] for p in tied)}) — "
            f"жребий закона по обязательству дня выбрал путь {_ROMAN[winner]}. {theater}"
        )[:200]
    if not round_row.cards:
        loaded = await get_round(session, round_row.id)
        if loaded is not None:
            round_row = loaded
    cards = {card.position: card for card in round_row.cards}
    # Страховка от отравленного дня: без карт тик падал бы каждые 15 секунд
    # и день зависал навсегда. Закрываем с нейтральным каноном.
    winning_card = cards.get(winner) or SimpleNamespace(
        title=f"Путь {_ROMAN[winner]}",
        consequence="Тропа растворилась в тумане, не оставив следа.",
    )
    counts_json = json.dumps({str(key): value for key, value in counts.items()})
    result = await session.execute(
        update(Round)
        .where(Round.id == round_row.id, Round.status == RoundStatus.TALLYING)
        .values(
            winner_card=winner,
            vote_counts_json=counts_json,
            tie_note=tie_note,
            status=RoundStatus.CLOSED,
        )
    )
    if result.rowcount == 0:
        await session.rollback()
        loaded = await get_round(session, round_row.id)
        return (loaded or round_row), False
    round_row.winner_card = winner
    round_row.vote_counts_json = counts_json
    round_row.tie_note = tie_note
    round_row.status = RoundStatus.CLOSED
    session.add(
        StoryBeat(
            day_index=round_row.day_index,
            winning_title=winning_card.title,
            winning_text=winning_card.consequence,
            win_rule=round_row.win_rule.value,
            vote_counts=counts_json,
        )
    )
    # Эха обычно уже рождены в close_voting (см. комментарий там); здесь
    # страховка для путей, миновавших закрытие голосования (/advance и legacy).
    if not await _echoes_already_spawned(session, round_row.day_index):
        spawn_echoes_from_round(session, round_row)
    # Отношения NPC: один шаг по тегу победившего пути (у фолбэка тега нет).
    try:
        from app.relations import apply_round_result

        await apply_round_result(session, getattr(winning_card, "tag", None))
    except Exception:
        logger.warning("Шаг отношений NPC дня %s не удался", round_row.day_index, exc_info=True)
    # Нрав стаи: оси характера дрейфуют по тегу победившего пути
    # (забота → добро+порядок, хитрость → расчёт+порядок, риск → хаос+зло).
    try:
        from app.models import WatcherState as WS
        from app.season import RUN_START_KEY, apply_alignment_drift, set_run_anchor_cache

        anchor = await get_run_anchor(session)
        order, moral, changed = apply_alignment_drift(
            anchor,
            getattr(winning_card, "tag", None) or "care",
            seed=round_row.day_index,
        )
        if changed:
            row = await session.get(WS, RUN_START_KEY)
            if row is not None:
                row.value = json.dumps(anchor, ensure_ascii=False)
                await session.commit()
            set_run_anchor_cache(anchor)
            logger.info(
                "Нрав стаи после дня %s: порядок %d, мораль %d",
                round_row.day_index, order, moral,
            )
    except Exception:
        logger.warning("Дрейф нрава стаи не удался", exc_info=True)
    # AI World Engine: запись выбора стаи
    try:
        from app.world_engine import record_choice, AIChoice
        choice_obj = AIChoice(
            title=winning_card.title,
            description=winning_card.consequence,
            consequence=winning_card.consequence,
            tag=getattr(winning_card, "tag", "custom") or "custom",
            characters_involved=[],
            location=round_row.place,
        )
        await record_choice(
            session,
            day_index=round_row.day_index,
            choice=choice_obj,
            votes_count=sum(counts.values()) if counts else 0,
            won=True,
        )
    except Exception:
        logger.warning("AIWorldEngine: запись выбора не удалась", exc_info=True)
    # AI World Engine: каскад последствий от выбора стаи
    try:
        from app.world_engine import process_choice_consequences, get_world_context
        from app.story import _chat_completion
        from app.models import PackState as PackStateModel
        from sqlalchemy import select as sa_select

        # Загружаем потребности стаи из БД или дефолты
        try:
            ps_result = await session.execute(sa_select(PackStateModel).limit(1))
            ps = ps_result.scalar_one_or_none()
            needs_dict = {
                "hunger": ps.hunger if ps else 5,
                "thirst": ps.thirst if ps else 5,
                "health": ps.health if ps else 10,
            }
        except Exception:
            needs_dict = {"hunger": 5, "thirst": 5, "health": 10}

        ctx = await get_world_context(session, round_row.day_index, needs_dict, season=round_row.season)
        chain = await process_choice_consequences(
            session,
            ctx,
            _chat_completion,
            choice_text=winning_card.consequence,
            choice_tag=getattr(winning_card, "tag", "custom") or "custom",
            day_index=round_row.day_index,
        )
        if chain:
            logger.info(
                "AIWorldEngine: цепочка из %d последствий применена для дня %d",
                len(chain.chain),
                round_row.day_index,
            )
    except Exception:
        logger.warning("AIWorldEngine: каскад последствий не удался", exc_info=True)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        loaded = await get_round(session, round_row.id)
        return (loaded or round_row), False
    # ── DayProjection + Plugin hooks ──
    # Собираем единый объект-факт и передаём его всем плагинам.
    # Проекция используется broadcast, prompt assembly и другими
    # downstream-системами вместо повторных запросов к БД.
    try:
        from app.projection import build_projection
        from app.plugins import PluginContext
        from app.builtin_plugins import register_builtin_plugins

        # Инициализация реестра (идемпотентна — повторный вызов не дублирует)
        register_builtin_plugins()
        loaded = await get_round(session, round_row.id)
        projection = await build_projection(session, loaded or round_row)
        ctx = PluginContext(projection=projection, session=session)
        from app.plugins import registry

        await registry.run_post_day_hooks(ctx)
        # Кэшируем проекцию в WatcherState для доступа из broadcast/prompt
        try:
            from app.models import WatcherState as WS

            ws_key = f"day_projection:{round_row.day_index}"
            ws_row = (await session.execute(select(WS).where(WS.key == ws_key))).scalar_one_or_none()
            if ws_row is None:
                session.add(WS(key=ws_key, value=json.dumps(projection.to_dict(), ensure_ascii=False)))
            else:
                ws_row.value = json.dumps(projection.to_dict(), ensure_ascii=False)
            await session.commit()
        except Exception:
            logger.debug("Кэш DayProjection не записан", exc_info=True)
    except Exception:
        logger.warning("DayProjection/plugin hooks не выполнены", exc_info=True)
    # AI World Engine: создаём снимок мира в конце дня
    try:
        from app.world_engine import create_world_snapshot
        await create_world_snapshot(session, round_row.day_index)
    except Exception:
        logger.debug("AIWorldEngine: снимок мира не создан", exc_info=True)
    return await get_round(session, round_row.id), True  # type: ignore[return-value]


async def polish_stub_images() -> int:
    """Шлифовка заглушек: повторные попытки в течение 24 часов после дня.

    Маркеры img_stubs:* старше суток удаляются (окно вышло). Возвращает
    число дней, отправленных на перерисовку.
    """
    import time as _time

    from app.models import WatcherState

    from app.db import SessionLocal

    now_ts = int(_time.time())
    targets: list[int] = []
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(WatcherState).where(WatcherState.key.like("img_stubs:%"))
            )
        ).scalars()
        for row in list(rows):
            try:
                data = json.loads(row.value)
                ts = int(data.get("ts", 0))
            except Exception:
                ts = 0
            day = int(row.key.split(":", 1)[1])
            if now_ts - ts > 86_400:
                await session.delete(row)
                continue
            targets.append(day)
        await session.commit()

    upgraded_days = 0
    for day in targets:
        try:
            await upgrade_stub_images(day)
            upgraded_days += 1
        except Exception:
            logger.exception("Шлифовка картинок дня %d не удалась", day)
    return upgraded_days
