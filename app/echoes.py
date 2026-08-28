"""Тихие последствия выборов: следы дней оседают в каноне и могут всплыть позже.

После итогов дня каждый из трёх путей оставляет след. Победивший — сильный и
всплывает раньше; невыбранные пути тоже не исчезают, но могут раствориться
в земле, а могут и выйти на тропу через несколько дней. Следы вплетаются в
сюжет как естественная часть мира: бот нигде их не подсвечивает и не
оповещает — заметит игрок или нет, зависит только от него.
"""

from __future__ import annotations

import logging
import random

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LoreEcho, Round

logger = logging.getLogger(__name__)


_KINDS = {"risk": "угроза", "care": "память", "cunning": "обман"}

_FADE_CHANCE = {1: 0.55, 2: 0.30}


def _clamp_tag(tag: str | None) -> str:
    return tag if tag in {"risk", "care", "cunning"} else "care"


def spawn_echoes_from_round(session: AsyncSession, round_row: Round) -> int:
    """Вызывается из finish_tally: три следа за день, победитель — strength=3."""
    rng = random.Random(f"echo:{round_row.id}")
    spawned = 0
    for card in sorted(round_row.cards, key=lambda item: item.position):
        won = card.position == round_row.winner_card
        tag = _clamp_tag(getattr(card, "tag", None))
        if won:
            strength = 3
            delay = rng.randint(1, 2)
        else:
            strength = rng.randint(1, 2)
            delay = rng.randint(2, 4)
        session.add(
            LoreEcho(
                born_day=round_row.day_index,
                source_day=round_row.day_index,
                kind=_KINDS[tag],
                title=card.title[:160],
                description=card.consequence,
                strength=strength,
                earliest_day=round_row.day_index + delay,
                status="dormant",
            )
        )
        spawned += 1
    return spawned


async def collect_due_echoes(session: AsyncSession, day_index: int, limit: int = 2) -> list[LoreEcho]:
    """Достаёт созревшие следы перед генерацией дня.

    Слабые следы с некоторым шансом растворяются (status='faded') — появляются
    они по воле мира, без каких-либо уведомлений. Возвращаются только
    всплывшие; сильный след при всплытии оставляет за собой «второй след».
    """
    result = await session.execute(
        select(LoreEcho)
        .where(LoreEcho.status == "dormant", LoreEcho.earliest_day <= day_index)
        .order_by(LoreEcho.strength.desc(), LoreEcho.born_day.asc())
        .limit(limit)
    )
    candidates = list(result.scalars().all())
    surfaced: list[LoreEcho] = []
    for echo in candidates:
        chance = _FADE_CHANCE.get(echo.strength, 0.0)
        rng = random.Random(f"fade:{echo.id}")
        if chance and rng.random() < chance:
            echo.status = "faded"
            continue
        echo.status = "surfaced"
        echo.surfaced_day = day_index
        surfaced.append(echo)
        if echo.strength >= 3:
            chain_rng = random.Random(f"chain:{echo.id}:{day_index}")
            session.add(
                LoreEcho(
                    born_day=day_index,
                    source_day=echo.source_day,
                    kind=echo.kind,
                    title=f"{echo.title}: второй след"[:160],
                    description=f"{echo.description} Теперь это примета мира, которую трудно не заметить.",
                    strength=2,
                    earliest_day=day_index + chain_rng.randint(2, 4),
                    status="dormant",
                )
            )
    return surfaced


def echo_prompt_lines(echoes) -> list[str]:

    return [f"- {echo.title}: {echo.description}" for echo in echoes]


# ---------- Квиз памяти: «откуда след?» ----------


async def surfaced_echoes_for_round(session, day_index: int) -> list[LoreEcho]:
    """Эха, реально вплетённые в главу этого дня (уже вскрытые при генерации)."""
    result = await session.execute(
        select(LoreEcho).where(LoreEcho.surfaced_day == day_index)
    )
    return list(result.scalars().all())


def build_memory_quiz(
    player_id: int, round_id: int, true_titles: list[str], decoy_pool: list[str]
) -> dict | None:
    """Три варианта «откуда след?»: истина + 2 приманки из давнего канона.

    Сид детерминирован парой игрок+день: перезапускать расклад бесполезно,
    а сервер при ответе пересобирает те же варианты и сверяет выбор.
    None — собирать квиз не из чего.
    """
    import random as _random

    truths = sorted({title.strip() for title in true_titles if title and title.strip()})
    if not truths:
        return None
    pool = []
    seen = set(truths)
    for title in decoy_pool:
        clean = (title or "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            pool.append(clean)
    rng = _random.Random(f"memquiz:{player_id}:{round_id}")
    options = [truths[0], *pool[:2]]
    rng.shuffle(options)
    correct_indices = [
        index for index, option in enumerate(options) if option in truths
    ]
    return {"options": options, "correct": set(correct_indices), "true_title": truths[0]}


def correct_memory_choice(quiz: dict, index: int) -> bool:
    """Верен ли ответ на квиз памяти по индексу варианта.

    Расклад хранит правильные позиции, а не текст: сверяем индекс, иначе
    сравнение строки варианта с множеством индексов всегда ложно.
    """
    return (
        quiz is not None
        and 0 <= index < len(quiz["options"])
        and index in quiz["correct"]
    )

