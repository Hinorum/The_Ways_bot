"""Система стриков и титулов прогрессии.

Титулы присваиваются автоматически за серию правильных голосований.
Стрик считается из current_streak / best_streak в модели Player.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Player, Round, Vote


@dataclass(frozen=True)
class Title:
    key: str
    name: str
    emoji: str
    description: str
    correct_needed: int


# Пороги титулов: от 3 до 50 правильных подряд
TITLES: tuple[Title, ...] = (
    Title("novice", "Щенок", "🐾", "Первые шаги на тропе", 0),
    Title("tracking", "Следопыт", "🐾", "Три верных пути подряд", 3),
    Title("scout", "Разведчик", "🦊", "Пять верных путей подряд", 5),
    Title("ranger", "Следопыт Стаи", "🐺", "Семь верных путей подряд", 7),
    Title("oracle", "Оракул", "🔮", "Десять верных путей подряд — стая помнит твой нюх", 10),
    Title("sage", "Мудрец", "📜", "Пятнадцать верных путей — ты читаешь мир как папку", 15),
    Title("elder", "Старейшина", "🏛️", "Двадцать верных путей — стая идёт за тобой", 20),
    Title("legend", "Легенда Стаи", "⭐", "Тридцать верных путей — твой нюх стал легендой", 30),
    Title("prophet", "Пророк", "🌟", "Пятьдесят верных путей — ты видишь завтра", 50),
)


async def generate_streak_narrative_ai(calling_key: str, streak: int) -> str | None:
    """AI генерирует уникальную фразу достижения на основе титула и стрика.

    Возвращает строку или None при ошибке (фолбэк к статичным описаниям).
    """
    from app.story import _chat_completion

    titles = {
        "novice": "Щенок",
        "tracking": "Следопыт",
        "scout": "Разведчик",
        "ranger": "Следопыт Стаи",
        "oracle": "Оракул",
        "sage": "Мудрец",
        "elder": "Старейшина",
        "legend": "Легенда Стаи",
        "prophet": "Пророк",
    }
    title = titles.get(calling_key, calling_key)

    prompt = (
        f"Титул: {title} ({calling_key}). Серия: {streak} верных путей.\n\n"
        "Напиши одну фразу-достижение (1 предложение, 15-30 слов) — "
        "метафору из личного дневника стаи. Стиль: торжественный, "
        "без кавычек, просто текст."
    )

    result = await _chat_completion(
        [{"role": "user", "content": prompt}],
        timeout=15,
    )
    if result is None:
        return None
    payload, _used_model = result
    try:
        text = str(payload["choices"][0]["message"]["content"]).strip()
        return text if len(text) < 150 else text[:147] + "..."
    except Exception:
        return None


def title_for_streak(streak: int) -> Title:
    """Возвращает титул по текущему стрику."""
    result = TITLES[0]
    for title in TITLES:
        if streak >= title.correct_needed:
            result = title
    return result


def next_title(streak: int) -> Title | None:
    """Возвращает следующий титул, к которому стоит стремиться, или None если максимальный."""
    for title in TITLES:
        if streak < title.correct_needed:
            return title
    return None


async def update_streak(session: AsyncSession, player: Player, was_correct: bool) -> None:
    """Обновляет стрик игрока после подсчёта голосов."""
    if was_correct:
        player.current_streak += 1
        if player.current_streak > player.best_streak:
            player.best_streak = player.current_streak
    else:
        player.current_streak = 0


def streak_text(player: Player) -> str:
    """Форматирует текст стрика для /score."""
    current = player.current_streak
    best = player.best_streak
    title = title_for_streak(current)
    nxt = next_title(current)

    lines = [f"{title.emoji} <b>{title.name}</b>"]
    if current > 0:
        lines.append(f"🔥 Серия верных путей: {current} · Лучшая: {best}")
    else:
        lines.append(f"🔥 Лучшая серия: {best}")

    if nxt:
        remaining = nxt.correct_needed - current
        lines.append(f"📈 До следующего титула: {nxt.emoji} {nxt.name} — ещё {remaining} {remaining_word(remaining)}")
    elif current >= TITLES[-1].correct_needed:
        lines.append("🏆 Ты достиг вершины. Стая идёт за тобой.")

    return "\n".join(lines)


def remaining_word(n: int) -> str:
    """Склонение слова «путь/пути/путей» для числа."""
    if n % 10 == 1 and n % 100 != 11:
        return "путь"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "пути"
    return "путей"


async def calc_rank(session: AsyncSession, player_id: int) -> dict:
    """Вычисляет позицию игрока в рейтинге за текущую неделю и месяц.

    Рейтинг = correct_picks за период + дни голосования за период.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    week_start = now - timedelta(days=now.weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Подсчитываем для каждого игрока: верные голоса + дни голосования за период
    # Используем подзапрос для подсчёта
    week_stats = await session.execute(
        select(
            Vote.player_id,
            func.count(Vote.id).label("votes"),
        )
        .join(Round, Vote.round_id == Round.id)
        .where(Round.opens_at >= week_start)
        .group_by(Vote.player_id)
    )
    week_data = {row.player_id: row.votes for row in week_stats}

    month_stats = await session.execute(
        select(
            Vote.player_id,
            func.count(Vote.id).label("votes"),
        )
        .join(Round, Vote.round_id == Round.id)
        .where(Round.opens_at >= month_start)
        .group_by(Vote.player_id)
    )
    month_data = {row.player_id: row.votes for row in month_stats}

    # Получаем correct_picks для периода (из Round winner + Vote)
    # Упрощённо: используем total correct_picks как приблизительный показатель
    # для ранжирования (точный подсчёт за период требует сложного JOIN)
    all_players = await session.execute(
        select(Player.id, Player.correct_picks, Player.score)
    )
    players = {row.id: (row.correct_picks, row.score) for row in all_players}

    # Сортируем по верным голосам (а потом по очкам)
    ranked = sorted(
        players.keys(),
        key=lambda pid: (players[pid][0], players[pid][1]),
        reverse=True,
    )

    week_ranked = sorted(
        week_data.keys(),
        key=lambda pid: week_data[pid],
        reverse=True,
    )

    player_pos = ranked.index(player_id) + 1 if player_id in ranked else len(ranked) + 1
    player_week_pos = week_ranked.index(player_id) + 1 if player_id in week_ranked else len(week_ranked) + 1

    return {
        "overall_rank": player_pos,
        "overall_total": len(ranked),
        "week_rank": player_week_pos,
        "week_total": len(week_ranked),
        "week_votes": week_data.get(player_id, 0),
        "month_votes": month_data.get(player_id, 0),
    }


async def path_legacy(session: AsyncSession, limit: int = 5) -> list[dict]:
    """Возвращает последние не выбранные пути, которые могут вернуться как эхо.

    Ищет в StoryBeat Winning title и показывает проигравшие карточки.
    """
    from app.models import Card, StoryBeat

    result = await session.execute(
        select(StoryBeat).order_by(StoryBeat.day_index.desc()).limit(limit)
    )
    beats = result.scalars().all()

    legacy = []
    for beat in beats:
        # Получаем карточки раунда
        cards_result = await session.execute(
            select(Card).join(Round, Card.round_id == Round.id)
            .where(Round.day_index == beat.day_index)
            .order_by(Card.position)
        )
        cards = cards_result.scalars().all()
        for card in cards:
            if card.title != beat.winning_title:
                legacy.append({
                    "day": beat.day_index,
                    "title": card.title,
                    "tag": getattr(card, "tag", "care"),
                })

    return legacy[:limit * 2]  # Берём до 2x непобеждённых путей


async def weekly_report(session: AsyncSession) -> str:
    """Генерирует еженедельный отчёт стаи: статистика голосов, настроение, титулы."""
    from datetime import datetime, timedelta, timezone

    from app.models import Card, Round, Vote

    now = datetime.now(timezone.utc)
    week_start = now - timedelta(days=now.weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)

    # Получаем раунды за неделю
    rounds_result = await session.execute(
        select(Round).where(
            Round.status == "closed",
            Round.opens_at >= week_start,
        ).order_by(Round.day_index)
    )
    rounds = list(rounds_result.scalars().all())

    if not rounds:
        return "🐺 На этой неделе стая молчала. Новых дней не было."

    # Считаем теги победивших путей
    tag_counts = {"risk": 0, "care": 0, "cunning": 0}
    total_votes = 0
    for rnd in rounds:
        if rnd.winner_card is not None and rnd.vote_counts_json:
            import json
            counts = json.loads(rnd.vote_counts_json)
            total_votes += sum(counts.values())
            # Получаем карточку-победителя
            card_result = await session.execute(
                select(Card).where(
                    Card.round_id == rnd.id,
                    Card.position == rnd.winner_card,
                )
            )
            winner_card = card_result.scalar_one_or_none()
            if winner_card:
                tag = getattr(winner_card, "tag", "care")
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

    # Определяем доминирующий тег
    dominant = max(tag_counts, key=tag_counts.get) if any(tag_counts.values()) else "care"
    tag_names = {"risk": "Риск", "care": "Забота", "cunning": "Хитрость"}
    tag_emojis = {"risk": "⚔️", "care": "💚", "cunning": "🦊"}

    # Получаем топ-3 игроков по стрику
    streaks_result = await session.execute(
        select(Player).where(Player.current_streak > 0)
        .order_by(Player.current_streak.desc()).limit(3)
    )
    top_streaks = list(streaks_result.scalars().all())

    # Собираем отчёт
    lines = [
        f"🐺 <b>Неделя в пути</b>",
        f"📅 {len(rounds)} дней пройдено · 🗳 {total_votes} голосов",
        "",
        "<b>Настроение стаи:</b>",
    ]

    for tag, count in tag_counts.items():
        bar = "█" * count + "░" * (len(rounds) - count)
        lines.append(f"{tag_emojis[tag]} {tag_names[tag]}: {bar} {count}/{len(rounds)}")

    lines.append("")
    lines.append(f"Доминирующее настроение: {tag_emojis[dominant]} {tag_names[dominant]}")

    if top_streaks:
        lines.append("")
        lines.append("<b>🔥 Серии стаи:</b>")
        for i, p in enumerate(top_streaks, 1):
            name = p.first_name or p.username or f"#{p.id}"
            lines.append(f"{i}. {name} — {p.current_streak} подряд")

    return "\n".join(lines)
