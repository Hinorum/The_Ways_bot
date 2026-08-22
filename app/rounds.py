from __future__ import annotations

import asyncio
import hashlib
import json
import logging
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
    LeaderboardPot,
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
    try:
        text = await generate_epilogue(
            day_index=round_row.day_index,
            winner_title=winner.title,
            winner_consequence=winner.consequence,
            counts_line=counts_line,
            rule_phrase=RULE_PHRASES[round_row.win_rule],
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


async def _plan_and_render(session: AsyncSession, day_index: int) -> dict:
    """Тяжёлая половина создания дня: глава, библия арта и четыре картинки.

    Всё сетевое и медленное — здесь. Результат — лёгкий JSON-payload,
    который материализуется в раунд за миллисекунды.
    """
    beats = await previous_beats(session)
    echoes = await collect_due_echoes(session, day_index)
    rule = secrets.choice(list(WinRule))
    salt = secrets.token_hex(16)
    chapter = await generate_chapter(day_index, beats, rule, echoes)

    # Арт-директор: визуальный план дня, затем промпты каждого кадра.
    # Якорь предыдущего дня держит сериальность палитры и мотивов.
    anchor = await _load_art_anchor(session)
    bible = await plan_day_art(chapter, beats, anchor=anchor)
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
        "day_index": day_index,
        "rule": rule.value,
        "commitment": commit_rule(rule, salt) + ":" + salt,
        "chapter_title": chapter["title"],
        "chapter_text": chapter["text"],
        "lore_summary": chapter["lore_summary"],
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
        chapter_title=payload["chapter_title"],
        chapter_text=payload["chapter_text"],
        lore_summary=payload["lore_summary"],
        cover_path=payload.get("cover_path", ""),
        opens_at=opens_at,
        voting_ends_at=voting_ends_at,
        tally_ends_at=tally_ends_at,
    )
    session.add(round_row)
    await session.flush()
    for card in _payload_cards(payload):
        session.add(Card(round_id=round_row.id, **card))
    return round_row


_PREGEN_LOCK_PREFIX = "pregen_lock:"
_PREGEN_LOCK_TTL = 1800  # секунд: генерация дольше получаса считается мёртвой


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
        payload = await _plan_and_render(session, day_index)
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
                await _ensure_art_files(session, payload)
                round_row = await _materialize_round(session, payload, latest)
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
                loaded = await get_latest_round(session)
                assert loaded is not None
                return loaded, True

    day_index = 1 if latest is None else latest.day_index + 1
    payload = await _plan_and_render(session, day_index)
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
    return await get_latest_round(session), True  # type: ignore[return-value]


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
    """Counts stay secret while the round is open; the law is public from the start."""
    view = {
        "day_index": round_row.day_index,
        "status": round_row.status.value,
        "title": round_row.chapter_title,
        "text": round_row.chapter_text,
        "win_rule": round_row.win_rule.value,
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


def pick_winner(counts: dict[int, int], rule: WinRule) -> int:
    items = [(counts.get(i, 0), i) for i in range(3)]
    if rule is WinRule.MAJORITY:
        best = max(item[0] for item in items)
        return min(index for total, index in items if total == best)
    if rule is WinRule.MINORITY:
        worst = min(item[0] for item in items)
        return min(index for total, index in items if total == worst)
    ordered = sorted(items, key=lambda item: (item[0], item[1]))
    return ordered[1][1]


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
    winner = pick_winner(counts, round_row.win_rule)
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
        .values(winner_card=winner, vote_counts_json=counts_json, status=RoundStatus.CLOSED)
    )
    if result.rowcount == 0:
        await session.rollback()
        loaded = await get_round(session, round_row.id)
        return (loaded or round_row), False
    round_row.winner_card = winner
    round_row.vote_counts_json = counts_json
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
    spawn_echoes_from_round(session, round_row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        loaded = await get_round(session, round_row.id)
        return (loaded or round_row), False
    return await get_round(session, round_row.id), True  # type: ignore[return-value]

