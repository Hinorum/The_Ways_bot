from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.echoes import collect_due_echoes, spawn_echoes_from_round
from app.models import (
    Card,
    LoreEcho,
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
    WinRule,
)
from app.art_director import build_image_prompt, plan_day_art, short_image_prompt
from app.memory import recall_beats
from app.season import season_key
from app.story import fetch_day_image, generate_chapter, generate_epilogue, render_card, render_cover
from app.ton_pay import pending_payout_count


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
    voting_ends_at = _next_hour_slot(opens_at, settings.day_open_hour_utc - 1)
    if voting_ends_at - opens_at < timedelta(hours=6):
        # Открылись слишком близко к границе — короткий день никому не нужен,
        # переносим закрытие на следующие сутки (сетка сохраняется).
        voting_ends_at += timedelta(days=1)
    tally_ends_at = voting_ends_at + timedelta(seconds=settings.tally_seconds)
    return voting_ends_at, tally_ends_at


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
        from app.season import is_finale_day

        if is_finale_day(utc_aware(round_row.voting_ends_at)):
            season_note = (
                "Этот день закрыл сезон: эпилог должен прозвучать финальным "
                "аккордом месяца — мир после Первого Лая уже другой."
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


async def _villain_block(session: AsyncSession, open_moment: datetime) -> str | None:
    """Сюжет-машина сезона: продвигает план Хозяина Ошибки и отдаёт блок промпта.

    Состояние живёт в watcher_state (один ключ на весь мир): ступень и список
    событий. Новая ступень добавляет ровно одно каноническое событие,
    детерминированное по сезону — два инстанса бота согласованы.
    """
    import json as _json

    from app.models import WatcherState
    from app.season import (
        VILLAIN_KEY,
        day_of_season,
        days_in_season,
        season_key,
        villain_event,
        villain_prompt_block,
        villain_stage,
    )

    key = season_key(open_moment)
    day = day_of_season(open_moment)
    total = days_in_season(key)
    stage = villain_stage(day, total)

    row = await session.get(WatcherState, VILLAIN_KEY)
    data: dict = {}
    if row is not None and row.value:
        try:
            data = json.loads(row.value)
        except ValueError:
            data = {}
    if data.get("season") != key or not isinstance(data, dict):
        data = {"season": key, "stage": -1, "events": []}

    changed = False
    while data["stage"] < stage:
        data["stage"] += 1
        data.setdefault("events", []).append(villain_event(key, data["stage"]))
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


async def _plan_and_render(
    session: AsyncSession, day_index: int, opens_hint: datetime | None = None
) -> dict:
    """Тяжёлая половина создания дня: глава, библия арта и четыре картинки.

    Всё сетевое и медленное — здесь. Результат — лёгкий JSON-payload,
    который материализуется в раунд за миллисекунды.
    """
    beats = await previous_beats(session)
    echoes = await collect_due_echoes(session, day_index)
    rule = secrets.choice(list(WinRule))
    salt = secrets.token_hex(16)

    # Сезонная рамка: акты месяца и финал Первого Лая по календарю UTC.
    open_moment = utc_aware(opens_hint) if opens_hint is not None else _now()
    from app.season import (
        day_of_season,
        season_block as build_season_block,
        season_key,
    )

    key = season_key(open_moment)
    balance = await season_tag_balance(session, key)
    prev_summary = None
    if day_of_season(open_moment) <= 2:
        prev_summary = await previous_season_summary(session, key)
    sblock = build_season_block(
        open_moment=open_moment,
        balance=balance,
        previous_season_summary=prev_summary,
    )
    places_block = await places_memory_block(session)
    # План Хозяина Ошибки: продвигается по ступеням сезона, канон — в промпт.
    villain = await _villain_block(session, open_moment)

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

    chapter = await generate_chapter(
        day_index, beats, rule, echoes, distant_echoes=distant,
        season_block=sblock, places_block=places_block,
        villain_block=villain, sealed=sealed_day(day_index),
    )

    # Арт-директор: визуальный план дня, затем промпты каждого кадра.
    # Якорь предыдущего дня держит сериальность палитры и мотивов.
    echo_motifs = sorted({_ECHO_ART_MOTIFS.get(echo.kind, "portal hum haze") for echo in echoes})
    anchor = await _load_art_anchor(session)
    bible = await plan_day_art(chapter, beats, anchor=anchor, extra_motifs=echo_motifs)
    await _save_art_anchor(session, bible)

    media_root = Path(settings.media_dir)
    cover_path = media_root / f"day{day_index}_cover.jpg"
    day_seed = 10_000 + day_index * 7
    jobs = []
    for position, card in enumerate(chapter["cards"]):
        image_path = media_root / f"day{day_index}_card{position}.jpg"
        prompt = build_image_prompt(bible, str(position), seed=day_seed + position + 1)
        short = short_image_prompt(bible, str(position), seed=day_seed + position + 1)
        jobs.append((position, card, image_path, prompt, short))
    fetched = await asyncio.gather(
        # Обложка — широкий кинематографичный кадр, карты — портретные сцены.
        fetch_day_image(
            build_image_prompt(bible, "cover", seed=day_seed),
            short_image_prompt(bible, "cover", seed=day_seed),
            cover_path,
            seed=day_seed,
            width=1280,
            height=720,
        ),
        *(
            fetch_day_image(job[3], job[4], job[2], seed=day_seed + job[0] + 1)
            for job in jobs
        ),
    )
    if not fetched[0]:
        # PIL-рендер синхронный и тяжёлый — уводим из event loop.
        await asyncio.to_thread(render_cover, cover_path, chapter["title"], chapter["text"])
    cards_payload = []
    for (position, card, image_path, _prompt, _short), ok in zip(jobs, fetched[1:]):
        if not ok:
            await asyncio.to_thread(render_card, image_path, card["title"], card["description"], position)
        cards_payload.append(
            {
                "position": position,
                "title": card["title"],
                "description": card["description"],
                "consequence": card["consequence"],
                "tag": card.get("tag", "care"),
                "image_path": str(image_path),
            }
        )
    return {
        "v": PREPARED_PAYLOAD_VERSION,
        "day_index": day_index,
        "rule": rule.value,
        "commitment": commit_rule(rule, salt) + ":" + salt,
        "sealed": sealed_day(day_index),
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


async def _ensure_art_files(session: AsyncSession, payload: dict) -> None:
    """Страховка: если файлы заготовки пропали (чистка диска), рисуем фолбэк."""
    import os

    cover = payload.get("cover_path", "")
    if cover and not os.path.exists(cover):
        await asyncio.to_thread(render_cover, Path(cover), payload["chapter_title"], payload["chapter_text"])
    for card in _payload_cards(payload):
        image = card.get("image_path", "")
        if image and not os.path.exists(image):
            await asyncio.to_thread(
                render_card, Path(image), card["title"], card["description"], card["position"]
            )


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
    return round_row


# Визуальная фактура типов эхов: содержание скрыто, фактура повторяется —
# внимательный игрок учится узнавать класс следа по кадру дня.
_ECHO_ART_MOTIFS = {
    "угроза": "ominous burnt-wire glow in the fog",
    "память": "a warm amber keepsake bowl catching light",
    "обман": "a mirage-like silhouette of an unfamiliar dog",
}


_PREGEN_LOCK_PREFIX = "pregen_lock:"
_PREGEN_LOCK_TTL = 1800  # секунд: генерация дольше получаса считается мёртвой

# Формат payload'а заготовки. Незнакомая версия выбрасывается при
# материализации — день честно генерируется заново по текущим правилам.
PREPARED_PAYLOAD_VERSION = 1


async def prepare_next_day(session: AsyncSession, current_day_index: int) -> bool:
    """Прегенерация следующего дня в час подсчёта.

    Тяжёлая генерация уходит в окно TALLYING, поэтому в 11:00 UTC день
    открывается мгновенно из готовой заготовки. Claim через PreparedDay-строку
    и временный лок в watcher_state: повторные тики и второй инстанс не плодят
    параллельных генераций. False — готовить нечего/уже готовится.
    """
    day_index = current_day_index + 1
    existing = (
        await session.execute(select(Round.id).where(Round.day_index == day_index).limit(1))
    ).scalar_one_or_none()
    if existing is not None:
        return False
    prepared = await session.get(PreparedDay, day_index)
    if prepared is not None and prepared.payload:
        return False
    from app.models import WatcherState

    lock_key = f"{_PREGEN_LOCK_PREFIX}{day_index}"
    now_stamp = int(_now().timestamp())
    lock = await session.get(WatcherState, lock_key)
    if lock is not None:
        try:
            if now_stamp - int(lock.value) < _PREGEN_LOCK_TTL:
                return False
        except ValueError:
            pass
        lock.value = str(now_stamp)
    else:
        session.add(WatcherState(key=lock_key, value=str(now_stamp)))
    await session.commit()
    try:
        current = (
            await session.execute(
                select(Round).order_by(Round.day_index.desc()).limit(1)
            )
        ).scalar_one_or_none()
        opens_hint = utc_aware(current.tally_ends_at) if current and current.tally_ends_at else None
        payload = await _plan_and_render(session, day_index, opens_hint=opens_hint)
        blob = json.dumps(payload, ensure_ascii=False)
        if prepared is None:
            session.add(PreparedDay(day_index=day_index, payload=blob))
        else:
            prepared.payload = blob
        await session.commit()
        return True
    finally:
        fresh = await session.get(WatcherState, lock_key)
        if fresh is not None:
            await session.delete(fresh)
            await session.commit()


async def create_next_round_detailed(session: AsyncSession) -> tuple[Round, bool]:
    """Создаёт следующий день. Второе значение — был ли день создан сейчас.

    Сначала пробует готовую заготовку из часа подсчёта (мгновенно), иначе
    делает полный цикл «план → рендер → материализация» на месте.
    """
    latest = await get_latest_round(session)
    if latest is not None:
        next_day = latest.day_index + 1
        prepared = await session.get(PreparedDay, next_day)
        if prepared is not None and prepared.payload:
            try:
                payload = json.loads(prepared.payload)
                if int(payload.get("v", 0)) != PREPARED_PAYLOAD_VERSION:
                    raise ValueError("неизвестная версия заготовки")
                await _ensure_art_files(session, payload)
                materialized = await _materialize_round(session, payload, latest)
            except (ValueError, KeyError, TypeError):
                # Битая заготовка — выбрасываем и идём обычным путём.
                # Rollback протухает объекты: day_index держим в переменной,
                # а latest перечитываем заново.
                await session.rollback()
                await session.execute(
                    delete(PreparedDay).where(PreparedDay.day_index == next_day)
                )
                await session.commit()
                latest = await get_latest_round(session)
            else:
                await session.delete(prepared)
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    existing = await get_latest_round(session)
                    if existing is None:
                        raise
                    return existing, False
                return materialized, True

    day_index = 1 if latest is None else latest.day_index + 1
    opens_hint = (
        max(_now(), utc_aware(latest.tally_ends_at))
        if latest is not None and latest.tally_ends_at is not None
        else None
    )
    payload = await _plan_and_render(session, day_index, opens_hint=opens_hint)
    try:
        round_row = await _materialize_round(session, payload, latest)
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


async def create_next_round(session: AsyncSession) -> Round:
    row, _created = await create_next_round_detailed(session)
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
    await session.execute(delete(Card))
    # Заготовки старого канона больше не имеют силы: мир переписан заново.
    await session.execute(delete(PreparedDay))
    if not keep_story:
        await session.execute(delete(StoryBeat))
        await session.execute(delete(LoreEcho))
        # Полный сброс стирает и план Хозяина Ошибки: новый мир — новый план.
        from app.models import WatcherState
        from app.season import VILLAIN_KEY

        await session.execute(
            delete(WatcherState).where(WatcherState.key == VILLAIN_KEY)
        )
    await session.execute(delete(Round))
    await session.execute(update(Player).values(score=0, correct_picks=0))
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
    # подсчёта: прегенерация завтрашней главы собирается именно в этот час,
    # и эхо победителя (earliest_day=завтра) обязано уже существовать,
    # иначе оно системно всплывало бы на день позже замысла. Исход дня при
    # этом не раскрывается: ни StoryBeat, ни счётчики не публикуются.
    counts = await count_votes_for_tally(session, round_row.id)
    seed = f"{round_row.rule_commitment}:{round_row.day_index}"
    round_row.winner_card = pick_winner(counts, round_row.win_rule, seed=seed)
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
    winner = pick_winner(counts, round_row.win_rule, seed=seed)
    tied = tied_positions(counts, round_row.win_rule)
    tie_note: str | None = None
    if len(tied) > 1:
        tie_note = (
            f"Голоса разделились ({' и '.join(_ROMAN[p] for p in tied)}) — "
            f"жребий закона по обязательству дня выбрал путь {_ROMAN[winner]}."
        )
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
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        loaded = await get_round(session, round_row.id)
        return (loaded or round_row), False
    return await get_round(session, round_row.id), True  # type: ignore[return-value]

