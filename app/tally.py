from __future__ import annotations

import json

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    LeaderboardPot,
    PackFund,
    Player,
    Payout,
    Round,
    Stake,
    Vote,
    WeeklyPot,
    WinRule,
    RULE_PHRASES,
)
from app.rounds import pick_winner
from app.stakes import current_network
from app.ton_utils import from_nano
from app.weeks import iso_week_key


_CHUNK = 500  # лимит параметров IN(...): большие дни чанкуются


def _tg_escape(text: str) -> str:
    """Экранирование спецсимволов HTML для Telegram."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clip(text: str, limit: int) -> str:
    """Обрезка по словам с многоточием — не режет слово пополам."""
    if len(text) <= limit:
        return text
    truncated = text[: limit - 1]
    # Обрезаем по последнему пробелу, чтобы не резать слово
    last_space = truncated.rfind(" ")
    if last_space > limit // 2:
        truncated = truncated[:last_space]
    return truncated.rstrip(" ,.;:") + "…"


def _chunks(ids: list[int], size: int = _CHUNK):
    for start in range(0, len(ids), size):
        yield ids[start : start + size]


async def award_points(session: AsyncSession, round_row: Round) -> int:
    if round_row.winner_card is None:
        return 0
    voters = await session.execute(select(Vote.player_id).where(Vote.round_id == round_row.id))
    voter_ids = [row[0] for row in voters.all()]
    for chunk in _chunks(voter_ids):
        await session.execute(
            update(Player).where(Player.id.in_(chunk)).values(score=Player.score + 1)
        )
    winners = await session.execute(
        select(Vote.player_id).where(
            Vote.round_id == round_row.id,
            Vote.card_position == round_row.winner_card,
        )
    )
    winner_ids = [row[0] for row in winners.all()]
    for chunk in _chunks(winner_ids):
        await session.execute(
            update(Player)
            .where(Player.id.in_(chunk))
            .values(score=Player.score + 10, correct_picks=Player.correct_picks + 1)
        )
    # Вдохновение («Второй нюх») за верную серию: каждый 7-й верный путь
    # кладёт жетон. Жетон тратится только на личную микросцену — на механику
    # дня он не влияет.
    for chunk in _chunks(winner_ids):
        await session.execute(
            update(Player)
            .where(Player.id.in_(chunk), (Player.correct_picks % 7) == 0, Player.correct_picks > 0)
            .values(inspiration=Player.inspiration + 1)
        )

    # Обновление стриков: победители увеличивают, проигравшие сбрасывают
    from app.streaks import update_streak

    # Все голосовавшие
    all_voters_result = await session.execute(
        select(Player).where(Player.id.in_(voter_ids))
    )
    all_voters = {p.id: p for p in all_voters_result.scalars().all()}

    winner_set = set(winner_ids)
    for voter_id in voter_ids:
        player = all_voters.get(voter_id)
        if player:
            was_correct = voter_id in winner_set
            await update_streak(session, player, was_correct)

    await session.commit()
    return len(winner_ids)


async def register_memory_hit(session: AsyncSession, player_id: int, round_id: int) -> bool:
    """Одна отметка «Я помню» на игрока в день. True — отметка создана сейчас.

    Создание отметки дарит жетон вдохновения: внимательность вознаграждается
    нарративом, не деньгами.
    """
    from sqlalchemy import select as _select

    from app.models import MemoryHit

    existing = (
        await session.execute(
            _select(MemoryHit).where(
                MemoryHit.player_id == player_id,
                MemoryHit.round_id == round_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return False
    session.add(MemoryHit(player_id=player_id, round_id=round_id))
    await session.execute(
        update(Player).where(Player.id == player_id).values(inspiration=Player.inspiration + 1)
    )
    await session.commit()
    return True


def _reveal_phrase(counts: dict[int, int], win_rule, winner_card: int | None) -> str:
    """Реплика вскрытия урны: как закон дня сыграл против голосов стаи.

    Детерминированная драматургия без нейросети: сам момент раскрытия цифр —
    главный твист суток, подавать его протоколом расточительно.
    """
    import random as _random

    if not counts or winner_card is None or win_rule is None:
        return "Урна пуста — стая сегодня промолчала."
    values = sorted(counts.values())
    w = counts.get(winner_card, 0)
    max_v, min_v = values[-1], values[0]
    rng = _random.Random(f"reveal:{winner_card}:{max_v}:{min_v}:{values}")
    if win_rule == WinRule.MAJORITY:
        phrases = (
            f"большинство ({w} голосов) само провело этот путь",
            f"стая кричала за этот путь чаще всех — {w} голосов",
            f"{w} хвостов решили всё: закон и толпа совпали",
        )
    elif win_rule == WinRule.MINORITY:
        if w >= max_v > min_v:
            phrases = (
                "парадокс: громче всего лаяли за другой путь — архив записал бунт",
                "стая проголосовала против закона и проиграла сама себе",
                "закон был против толпы, и толпа этого не заметила",
            )
        else:
            phrases = (
                f"тихие голоса ({w}) оказались точнее всех",
                f"всего {w} хвостов пошло сюда — и именно они выбрали канон",
                "меньшинство взяло своё: тихие оказались дальновиднее",
            )
    else:  # MEDIAN
        median_v = values[len(values) // 2] if len(values) >= 3 else values[0]
        if w == median_v:
            phrases = (
                f"середина ({w} голосов) взяла своё: крайности остались ни с чем",
                f"закон выбрал меру — {w} голосов ровно посередине",
            )
        else:
            phrases = ("счёт разошёлся с законом так, что Архивариус пожал плечами",)
    return phrases[rng.randrange(len(phrases))]


_FLIP_SEARCH_CAP = 15  # отрыв больше этого уже не «на волоске» — строку не пишем


def _votes_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "голос"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "голоса"
    return "голосов"


def flip_margin(counts: dict[int, int], win_rule, winner_card: int | None) -> tuple[int, int] | None:
    """«Канон на волоске»: минимальное число переносов голосов, меняющее исход.

    Перенос = один голос меняет путь. Ищем минимальный k, при котором
    существует перераспределение k голосов, дающее другого победителя по
    закону дня. Возвращает (k, альтернативный победитель) или None: развязка
    была плотной, но перебор ограничен капом — разгромные дни недраматичны.
    """
    if winner_card is None or win_rule is None:
        return None
    total = sum(counts.values())
    if total < 2:
        return None
    base = [counts.get(i, 0) for i in range(3)]
    cap = min(total, _FLIP_SEARCH_CAP)
    for k in range(1, cap + 1):
        # Все способы снять k голосов с трёх путей.
        takings = [
            (d0, d1, k - d0 - d1)
            for d0 in range(min(k, base[0]) + 1)
            for d1 in range(min(k - d0, base[1]) + 1)
            if 0 <= k - d0 - d1 <= base[2]
        ]
        # Все способы положить k голосов обратно (куда угодно).
        gives = [
            (g0, g1, k - g0 - g1)
            for g0 in range(k + 1)
            for g1 in range(k - g0 + 1)
        ]
        for taking in takings:
            for giving in gives:
                fresh = {i: base[i] - taking[i] + giving[i] for i in range(3)}
                alt = pick_winner(fresh, win_rule)
                if alt != winner_card:
                    return k, alt
    return None


def format_results(round_row: Round) -> str:
    from app.style import result_mark

    raw = json.loads(round_row.vote_counts_json or "{}")
    counts = {int(key): int(value) for key, value in raw.items()}
    names = {card.position: _tg_escape(card.title) for card in round_row.cards}
    mark_key = str(getattr(round_row, "id", round_row.day_index))
    reveal = _reveal_phrase(counts, getattr(round_row, "win_rule", None), round_row.winner_card)
    lines = [
        f"{result_mark(mark_key)} День {round_row.day_index} закрыт",
        f"🕯 Архивариус вскрывает урну: {reveal}.",
    ]
    total_votes = sum(counts.values())
    if total_votes:
        word_v = _votes_word(total_votes)
        lines.append(f"🗳 Проголосовало: {total_votes} {word_v}")
    # «Канон на волоске»: сколько голосов отделяло мир от другого исхода.
    margin = flip_margin(counts, getattr(round_row, "win_rule", None), round_row.winner_card)
    if margin is not None:
        k, alt = margin
        alt_name = names.get(alt)
        if alt_name:
            word = _votes_word(k)
            lines.append(
                f"🩸 на волоске: ещё {k} {word} за «{alt_name}» — "
                "и тропа повела бы иначе."
            )
    if getattr(round_row, "sealed", False):
        lines.append(f"🗝 Запечатанный закон: {RULE_PHRASES[round_row.win_rule]}")
    else:
        lines.append(f"⚖️ Закон: {RULE_PHRASES[round_row.win_rule]}")
    lines.append("")
    for position in range(3):
        mark = " ← 🏆 След" if position == round_row.winner_card else ""
        lines.append(f"{names[position]}: {counts.get(position, 0)}{mark}")
    if getattr(round_row, "tie_note", None):
        lines.append(f"🤝 {round_row.tie_note}")
    winner = names[round_row.winner_card or 0]
    consequence = _tg_escape(next(
        card.consequence for card in round_row.cards if card.position == round_row.winner_card
    ))
    lines += ["", f"📜 Канон: {winner}", _clip(consequence, 240)]
    return "\n".join(lines)


def format_world_effects(round_row: Round, session=None) -> str:
    """Форматирует эффекты AI World Engine для итогов дня."""
    lines = []

    # Локация: атмосфера
    if round_row.place and session:
        try:
            from sqlalchemy import select as sa_select
            from app.models import WorldLocation
            import asyncio

            async def _get_atmosphere():
                q = sa_select(WorldLocation).where(WorldLocation.name == round_row.place)
                result = await session.execute(q)
                loc = result.scalar_one_or_none()
                return loc.atmosphere if loc and loc.atmosphere else None

            loop = asyncio.get_event_loop()
            if loop.is_running():
                atmosphere = None
            else:
                atmosphere = loop.run_until_complete(_get_atmosphere())
            if atmosphere:
                lines.append(f"🌫 Атмосфера: {_clip(atmosphere, 120)}")
        except Exception:
            pass

    # Цепочка последствий: события дня
    if session:
        try:
            from sqlalchemy import select as sa_select
            from app.models import WorldEvent

            async def _get_events():
                q = (
                    sa_select(WorldEvent)
                    .where(WorldEvent.day_index == round_row.day_index)
                    .limit(3)
                )
                result = await session.execute(q)
                return result.scalars().all()

            loop = asyncio.get_event_loop()
            if loop.is_running():
                events = []
            else:
                events = loop.run_until_complete(_get_events())
            if events:
                for event in events:
                    lines.append(f"🔗 {_clip(event.description, 150)}")
        except Exception:
            pass

    # Trust changes: доверие NPC
    if session:
        try:
            from sqlalchemy import select as sa_select
            from app.models import WorldCharacter

            async def _get_trust_changes():
                q = (
                    sa_select(WorldCharacter)
                    .where(WorldCharacter.is_alive == True)
                    .where(WorldCharacter.last_seen_day == round_row.day_index)
                    .limit(3)
                )
                result = await session.execute(q)
                chars = result.scalars().all()
                changes = []
                for c in chars:
                    if c.trust_stay >= 7:
                        changes.append(f"{c.name}: доверие ↑")
                    elif c.trust_stay <= 3:
                        changes.append(f"{c.name}: доверие ↓")
                return changes

            loop = asyncio.get_event_loop()
            if loop.is_running():
                trust_lines = []
            else:
                trust_lines = loop.run_until_complete(_get_trust_changes())
            if trust_lines:
                lines.append("🤝 " + "; ".join(trust_lines))
        except Exception:
            pass

    return "\n".join(lines)


async def format_plugin_results(
    round_row: Round, session: AsyncSession | None = None
) -> str:
    """Собирает дополнительные строки итогов от плагинов с RESULTS_FORMAT."""
    from app.plugins import PluginContext, registry as _plugin_registry
    from app.builtin_plugins import register_builtin_plugins

    register_builtin_plugins()

    # Пытаемся загрузить проекцию дня, если есть
    projection = None
    try:
        from app.projection import build_projection

        if session is not None:
            projection = await build_projection(session, round_row)
    except Exception:
        pass

    ctx = PluginContext(projection=projection, session=session)
    lines = await _plugin_registry.collect_results_format(ctx)
    return "\n".join(lines) if lines else ""


async def day_economics(session: AsyncSession, round_row: Round) -> dict:
    """Цифры дня для поста итогов: банк, проценты, коэффициент, копилка.

    Читает уже созданные финализацией выплаты, поэтому вызывать стоит после
    finalize_day_payouts; при пустом банке деньги просто не попадут в пост.
    """
    raw = json.loads(round_row.vote_counts_json or "{}")
    counts = {int(key): int(value) for key, value in raw.items()}
    players = sum(counts.values())
    # Ставки по путям: сколько денег стояло за каждый вариант (по голосам
    # игроков). Победившему пути — отдельная строка с долей банка.
    path_stakes_rows = await session.execute(
        select(Vote.card_position, func.coalesce(func.sum(Stake.amount_nanotons), 0))
        .join(Stake, Stake.player_id == Vote.player_id)
        .where(
            Vote.round_id == round_row.id,
            Stake.round_id == round_row.id,
            Stake.status == "confirmed",
            Stake.network == current_network(),
        )
        .group_by(Vote.card_position)
    )
    stats: dict = {
        "players": players,
        "counts": counts,
        "pot": round_row.pot_nanotons or 0,
        "multiplier": None,
        "week_today": round_row.weekly_nanotons or 0,
        "week_total": 0,
        "board_today": 0,
        "bank_total": 0,
        "fund_total": 0,
        "refunded": False,
        "path_stakes": {int(p): int(v) for p, v in path_stakes_rows.all()},
    }
    stats["winner_stake"] = (
        stats["path_stakes"].get(round_row.winner_card, 0)
        if round_row.winner_card is not None
        else 0
    )
    stats["winner_share_pct"] = (
        round(stats["winner_stake"] * 100 / stats["pot"]) if stats["pot"] else None
    )
    if not players and not stats["pot"]:
        return stats

    async def kind_sum(kind: str) -> int:
        row = await session.execute(
            select(func.coalesce(func.sum(Payout.amount_nanotons), 0)).where(
                Payout.round_id == round_row.id,
                Payout.kind == kind,
            )
        )
        return int(row.scalar_one())

    board_today = await kind_sum("leaderboard")
    prize_sum = await kind_sum("prize")
    bank_row = await session.execute(select(func.coalesce(func.sum(LeaderboardPot.nanotons), 0)))
    stats["bank_total"] = int(bank_row.scalar_one())
    week_row = await session.execute(
        select(func.coalesce(func.sum(WeeklyPot.nanotons), 0)).where(
            WeeklyPot.week == iso_week_key(round_row.opens_at)
        )
    )
    stats["week_total"] = int(week_row.scalar_one())
    # Фонд Стаи — единое накопление без периода: показываем общий баланс,
    # а не «сегодня», т.к. разыгрывается вручную, а не по расписанию.
    fund_row = await session.execute(
        select(func.coalesce(func.sum(PackFund.nanotons), 0))
    )
    stats["fund_total"] = int(fund_row.scalar_one())
    if board_today or stats["bank_total"]:
        stats["board_today"] = board_today

    if round_row.winner_card is not None:
        winners_subq = select(Vote.player_id).where(
            Vote.round_id == round_row.id,
            Vote.card_position == round_row.winner_card,
        )
        staked_winners = await session.execute(
            select(func.coalesce(func.sum(Stake.amount_nanotons), 0)).where(
                Stake.round_id == round_row.id,
                Stake.network == current_network(),
                Stake.status == "confirmed",
                Stake.player_id.in_(winners_subq),
            )
        )
        winning_total = int(staked_winners.scalar_one())
        if winning_total > 0 and prize_sum > 0:
            stats["multiplier"] = prize_sum / winning_total
        elif stats["pot"] > 0 and prize_sum == 0 and stats["week_today"] == 0:
            # Настоящий возврат: на верный путь не ставил никто. Если же
            # week_today > 0, призовые сгорели в газ сети и ушли в копилку
            # недели — это не возврат, и строка про возврат не показывается.
            stats["refunded"] = True
    return stats


def format_economics(stats: dict) -> str:
    """Текстовый блок экономики дня; без банка показывает только ставки.

    Явка и счёт путей уже есть в основном тексте итогов — здесь только
    деньги, чтобы одна цифра не встречалась в посте дважды.
    """
    lines: list[str] = []
    path_stakes = stats.get("path_stakes") or {}
    if any(path_stakes.values()):
        parts = " · ".join(
            f"{('I', 'II', 'III')[pos]} {from_nano(path_stakes.get(pos, 0)):.2f}"
            for pos in range(3)
        )
        lines.append(f"💸 Ставки на пути: {parts} Gram")
        winner_stake = int(stats.get("winner_stake") or 0)
        share = stats.get("winner_share_pct")
        if winner_stake:
            winner_pos = ("I", "II", "III")[int(stats.get("winner_card") or 0)] if stats.get("winner_card") is not None else ""
            share_note = f" ({share}% банка)" if share is not None else ""
            lines.append(
                f"🎯 На путь {winner_pos} поставлено {from_nano(winner_stake):.2f} Gram{share_note}"
            )
    if stats["pot"] <= 0:
        return "\n".join(lines)
    ton = from_nano
    lines.insert(0, f"💰 Банк дня: {ton(stats['pot']):.2f} Gram")
    if stats["refunded"]:
        lines.append("🎯 На верный путь не поставил никто — все ставки возвращены игрокам")
    elif stats["multiplier"] is not None:
        lines.append(f"🎯 Коэффициент верного пути: ×{stats['multiplier']:.2f}")
    if stats["week_today"] > 0 or stats["week_total"] > 0:
        lines.append(
            f"🗓 Неделя: ушло {ton(stats['week_today']):.2f} Gram"
            f" · в банке недели {ton(stats['week_total']):.2f} Gram"
        )
    if stats["board_today"] > 0 or stats["bank_total"] > 0:
        lines.append(
            f"🏆 Месяц: ушло {ton(stats['board_today']):.2f} Gram"
            f" · в банке месяца {ton(stats['bank_total']):.2f} Gram"
        )
    if stats["fund_total"] > 0:
        lines.append(f"🐾 В Фонде Стаи: {ton(stats['fund_total']):.2f} Gram")
    return "\n".join(lines)
