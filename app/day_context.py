"""DayContext — единый объект состояния дня для генерации главы.

До рефактора _plan_and_render держал с десяток параллельных чтений состояния
мира (канон, эха, шрамы, эмоции, потребности, ветви последствий, динамические
правила, сезон, арка, NPC, призвания, тропа) как расхожие локальные переменные
и вложенные try/except. Теперь все системы собираются одной функцией —
build_day_context — в ТОМ ЖЕ порядке обращений к БД, а итог живёт в одном
immutable-объекте DayContext.

Порядок систем при сборке (зависимостей) сохранён байт-в-байт: шрамы зависят
от вчерашнего тега (канон), эмоции и потребности — от того же тега, ветви — от
последней строки канона, динамические правила — от шрамов/эмоций/ветвей, сезон —
от якоря забега, правило дня — от поворота и DDA прошлого дня.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import LoreEcho, Round, StoryBeat, WinRule
from app.narrative.canon import load_canon

logger = logging.getLogger(__name__)


def sealed_day(day_index: int) -> bool:
    """Глухой день: закон не объявляется утром — только хеш-обязательство.

    Расписание детерминированное (каждый N-й день), чтобы игроки могли
    положиться на ритм; день 1 никогда не глухой.
    """
    every = max(0, settings.sealed_day_every)
    if every <= 0:
        return False
    return day_index % every == min(7, every - 1)


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


async def _previous_round_stats(
    session: AsyncSession,
) -> tuple[dict[int, int], int, int]:
    """Голоса, ставки и численность предыдущего закрытого дня для DDA.

    Возвращает (vote_counts, total_stakes_nanotons, player_count).
    Если предыдущего дня нет — дефолты (3, 0, 10).
    """
    logger.warning("DIAG-PRS: ENTER _previous_round_stats")
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
    from app.models import Card, RoundStatus

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
    from app.models import RoundStatus

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


@dataclass(frozen=True)
class DayContext:
    """Все системы состояния дня, собранные одним проходом build_day_context."""

    day_index: int
    beats: list[str]  # канон (строки previous_beats)
    echoes: list[LoreEcho]  # созревшие эха
    active_scar_keys: frozenset[str]
    yesterday_winner_tag: str | None
    emotion_block: str | None
    branches_block: str | None
    dynamic_rules_block: str | None
    sblock: str  # сезон + призвания + тропа + отношения + арка + Еретик
    places_block: str | None
    villain: str | None
    twist: bool
    rule: WinRule
    distant: list[str]  # дальние эха (recall_beats)
    focus_line: str | None
    pack_focus_line: str | None
    repeat_block: str | None
    characters_block: str
    npc_profiles: dict[str, dict] | None
    order_axis: int
    moral_axis: int
    run_salt: str
    key: str  # ключ забега ({anchor["key"]})
    open_moment: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def build_day_context(
    session: AsyncSession,
    day_index: int,
    opens_hint: datetime | None = None,
) -> DayContext:
    """Собирает состояние дня в одном проходе (порядок систем — как в production).

    Вызывается один раз на день; все чтения/обновления систем выполняются в той
    же последовательности зависимостей, что и прежде, но результат — единый
    объект DayContext вместо десятка локальных переменных.
    """
    logger.warning("DIAG-PR: ENTER day_index=%s", day_index)
    canon = await load_canon(session, day_index)
    beats = canon.lines
    echoes = canon.echoes

    # Шрамы мира: контур выживания/урона отключён (settings.world_scars=False).
    # Шрамы не создаются и не загружаются — сюжет ведёт без «боли мира».
    from app.lore import tags_from_beats

    history_tags = tags_from_beats(beats)
    yesterday_winner_tag = history_tags[-1] if history_tags else None
    active_scar_keys: set[str] = set()
    active_scars: list = []
    if settings.world_scars:
        from app.scar_rules import load_active_scars, process_round_scars

        active_scars = await load_active_scars(session, day_index)
        active_scar_keys = {s.scar_key for s in active_scars}
        if yesterday_winner_tag is not None:
            new_scars = await process_round_scars(session, yesterday_winner_tag, history_tags, day_index)
            for scar in new_scars:
                active_scar_keys.add(scar.scar_key)

    # Эмоциональный профиль: выключен (settings.emotion_system=False) — профиль
    # остаётся в дефолте, поэтому блок усталости/паранойи в промпт не попадает.
    from app.emotional_state import EmotionProfile

    if settings.emotion_system:
        from app.emotional_state import emotion_block_for_prompt, process_round_emotions

        emotion_profile = await process_round_emotions(session, yesterday_winner_tag, day_index)
        emotion_block = emotion_block_for_prompt(emotion_profile)
    else:
        emotion_profile = EmotionProfile()
        emotion_block = None

    # Деревья последствий: загрузка активных ветвей
    from app.consequence_trees import (
        load_active_branches, format_active_branches,
        create_branch, CONSEQUENCE_TREES,
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
        get_active_overrides, get_dynamic_rule_text,
    )

    dynamic_overrides = get_active_overrides(active_scars, emotion_profile, active_branches, day_index)
    dynamic_rules_block = get_dynamic_rule_text(dynamic_overrides)

    # Сезонная рамка: арка привязана к забегу (от сброса), финал — День
    # Первого Лая на длине месяца старта забега.
    open_moment = _utc_aware(opens_hint) if opens_hint is not None else _now()
    from app.season import (
        anchor_axes,
        heretic_prompt_block,
        season_block as build_season_block,
    )

    from app.rounds import get_run_anchor

    anchor = await get_run_anchor(session)
    guests = guest_blocks_for(day_index)
    key = anchor["key"]
    balance = await season_tag_balance(session, key)
    prev_summary = None
    if day_index <= 2:
        prev_summary = await previous_season_summary(session, key)

    # Стена отложенных клятв и целостность стаи: финал называет их ценой выбора,
    # но нигде не вычитает числа (мир не штрафует — он просит честно решить).
    try:
        from app.streaks import vow_wall_count

        vow_wall = await vow_wall_count(session)
    except Exception:
        logger.debug("Стена клятв для дня %s не посчитана", day_index, exc_info=True)
        vow_wall = 0
    try:
        from app.dog_memories import healed_memories_count

        healed_memories = await healed_memories_count(session)
    except Exception:
        logger.debug("Целостность стаи для дня %s не посчитана", day_index, exc_info=True)
        healed_memories = 0

    # Load AI-generated prologue beats and season arc from DB
    db_prologue_beats = None
    db_season_arc = None
    try:
        from app.prologue import load_prologue_beats_from_db
        from app.story_arc import load_season_arc_from_db
        from app.season import current_season as _current_season

        season_num = _current_season(anchor, open_moment)
        db_prologue_beats = await load_prologue_beats_from_db(session, season=season_num)
        db_season_arc = await load_season_arc_from_db(session, season=season_num)
    except Exception:
        logger.warning("DIAG-PR: prologue/arc DB query failed — rolling back")
        await session.rollback()
        pass

    sblock = build_season_block(
        anchor=anchor,
        moment=open_moment,
        balance=balance,
        previous_season_summary=prev_summary,
        db_prologue_beats=db_prologue_beats,
        db_season_arc=db_season_arc,
        vow_count=vow_wall,
        healed_memories=healed_memories,
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
        logger.warning("DIAG-PR: callings_prompt_block failed — rolling back")
        await session.rollback()
        pass
    if callings_block:
        sblock = f"{sblock}\n{callings_block}"
    # Характер стаи: определённый по голосованиям, влияет на тон повествования.
    from app.trail import trail_prompt_block

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
        logger.debug("Trail-блок дня %s не собран", day_index, exc_info=True)
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
    # AI-реакции NPC: уникальные описания поведения. Параллелим через gather —
    # глобальный семафор _chat_completion (story) не даст потоку провайдера
    # захлебнуться, а независимые NPC-промпты изображены одновременно.
    try:
        import asyncio as _asyncio
        from app.relations import generate_npc_reaction
        tasks = [
            generate_npc_reaction(npc_key, sentiment)
            for npc_key, sentiment in npc_sentiments.items()
            if sentiment != 0
        ]
        results = await _asyncio.gather(*tasks, return_exceptions=True) if tasks else []
        npc_reactions = [r for r in results if isinstance(r, str) and r]
        if npc_reactions:
            sblock = f"{sblock}\nРеакции NPC: " + " ".join(npc_reactions)
    except Exception:
        logger.warning("NPC реакции дня %s не собраны", day_index, exc_info=True)
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
        logger.warning("DIAG-PR: plugin blocks failed — rolling back")
        await session.rollback()
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
    canon_rows_result = await session.execute(
        select(StoryBeat).order_by(StoryBeat.day_index.asc())
    )
    canon = [
        f"{beat.winning_title}: {beat.winning_text}"
        for beat in (canon_rows_result.scalars() if canon_rows_result else [])
    ]
    query_parts = [beats[-1] if beats else "", *(echo.title for echo in echoes)]
    from app.memory import recall_beats

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
        logger.warning("NPC focus line дня %s не сгенерирована", day_index, exc_info=True)
        focus_line = None
    # Фокус-день стаи: одна собака выходит в центр сцены главы дня.
    pack_focus_line = None
    pack_focus_dog_key = None
    try:
        from app.story import pack_focus_line_for, pick_pack_focus

        pack_focus_line = pack_focus_line_for(day_index, key)
        pack_focus_dog_key = pick_pack_focus(day_index, key)
    except Exception:
        logger.debug("Фокус-день стаи для дня %s не выбран", day_index, exc_info=True)
    # Личная память стаи: заботливый/рискованный вчерашний день поднимает или
    # принимает слой жизни собаки-героя. Затем сама память попадает в промпт.
    try:
        from app.dog_memories import day_dog_memory_sync, dog_memory_block_for

        if pack_focus_dog_key is not None:
            await day_dog_memory_sync(
                session, pack_focus_dog_key, yesterday_winner_tag, day_index
            )
            memory_block = await dog_memory_block_for(session, pack_focus_dog_key)
            if memory_block:
                sblock = f"{sblock}\n{memory_block}"
    except Exception:
        logger.debug("Память стаи для дня %s не синхронизирована", day_index, exc_info=True)
    # Мягкая нехватка как метафора: мир отзывается тоном, не числами.
    try:
        from app.lore import scarcity_breath_block

        scarcity_block = scarcity_breath_block(history_tags)
        if scarcity_block:
            sblock = f"{sblock}\n{scarcity_block}"
    except Exception:
        logger.debug("Блок мягкой нехватки для дня %s не собран", day_index, exc_info=True)
    # AI World Engine: блок персонажей для промпта
    characters_block = ""
    try:
        from app.story import _build_dynamic_character_block
        characters_block = await _build_dynamic_character_block(session)
    except Exception:
        logger.debug("Dynamic character block не собран", exc_info=True)
    # Банк повторов: формулировки и места последних дней — модель не должна
    # дублировать их дословно (литературный де-дуп, окно 7 дней).
    repeat_block = await recent_repeats_block(session, day_index)
    # AI-профили NPC из БД для voice cards
    npc_profiles = None
    try:
        from app.npc_cog import load_all_npc_profiles
        npc_profiles = await load_all_npc_profiles(session)
    except Exception:
        logger.debug("AI-профили NPC дня %s не загружены", day_index, exc_info=True)
    # AI-кэши из БД
    try:
        from app.lore import (
            load_all_atmospheric, load_all_voice_examples, load_all_voice_banned,
            load_all_inner_thoughts, load_all_dog_pads, load_all_echo_tones,
            load_weather_pool, load_places,
        )
        from app.season import load_villain_events, load_heretic_events
        await load_all_atmospheric(session, season=1)
        await load_all_voice_examples(session, season=1)
        await load_all_voice_banned(session, season=1)
        await load_all_inner_thoughts(session, season=1)
        await load_all_dog_pads(session, season=1)
        await load_all_echo_tones(session, season=1)
        await load_weather_pool(session, season=1)
        await load_places(session, season=1)
        await load_villain_events(session, season=1)
        await load_heretic_events(session, season=1)
    except Exception:
        logger.debug("AI-кэши лора для дня %s не загружены", day_index, exc_info=True)

    return DayContext(
        day_index=day_index,
        beats=beats,
        echoes=echoes,
        active_scar_keys=frozenset(active_scar_keys),
        yesterday_winner_tag=yesterday_winner_tag,
        emotion_block=emotion_block,
        branches_block=branches_block,
        dynamic_rules_block=dynamic_rules_block,
        sblock=sblock,
        places_block=places_block,
        villain=villain,
        twist=twist,
        rule=rule,
        distant=distant,
        focus_line=focus_line,
        pack_focus_line=pack_focus_line,
        repeat_block=repeat_block,
        characters_block=characters_block,
        npc_profiles=npc_profiles,
        order_axis=order_axis,
        moral_axis=moral_axis,
        run_salt=run_salt,
        key=key,
        open_moment=open_moment,
    )