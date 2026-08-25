"""Еженедельная L2-вычитка стиля: редактор над главами, отчёт хранителю.

Раз в неделю (воскресенье 18:00 UTC) последние 7 глав уходят модели одним
запросом: найди 3 главных стилевых проблемы. Результат — админу в личку.
На посты игрокам не влияет: это наблюдаемость качества, не рерайт.
"""

from __future__ import annotations

import logging


from app.config import settings
from app.db import SessionLocal
from app.models import StoryBeat

logger = logging.getLogger(__name__)


async def weekly_style_review() -> str | None:
    """Возвращает краткую вычитку недели или None (модель молчит/нечего)."""
    if not settings.use_free_story_llm:
        return None
    from sqlalchemy import select

    from app.story import _chat_completion

    async with SessionLocal() as session:
        beats = (
            await session.execute(
                select(StoryBeat.winning_title, StoryBeat.winning_text)
                .order_by(StoryBeat.day_index.desc())
                .limit(7)
            )
        ).all()
    if len(beats) < 3:
        logger.info("Вычитка недели: меньше трёх дней — пропускаю")
        return None

    parts = [
        f"Глава: {title}\n{(text or '')[:600]}"
        for title, text in reversed(beats)
    ]
    corpus = "\n\n".join(parts)[:7000]
    messages = [
        {"role": "system", "content": "Ты — строгий, но доброжелательный редактор."},
        {
            "role": "user",
            "content": (
                "Вот главы игровой недели (тёмная сказка про стаю собак у "
                "порталов, второе лицо, настоящее время). Найди ТРИ главных "
                "стилевых проблемы недели: повторы формулировок, канцелярит, "
                "сбои тона или лица. Ответь коротким списком из трёх пунктов, "
                "каждый — одна фраза с примером. Без похвалы и вступлений.\n\n"
                f"{corpus}"
            ),
        },
    ]
    result = await _chat_completion(messages, timeout=90)
    if result is None:
        logger.info("Вычитка недели: модель молчит")
        return None
    try:
        text = str(result[0]["choices"][0]["message"]["content"]).strip()
    except Exception:
        return None
    if not text or not text_is_clean_local(text):
        return None
    logger.info("Вычитка недели готова (%d знаков)", len(text))
    return text[:1500]


def text_is_clean_local(text: str) -> bool:
    """Локальный лёгкий фильтр: непустой, не начинается с извинения модели."""
    low = text.lower().lstrip()
    return bool(low) and not low.startswith(("извин", "к сожалению", "sorry"))


async def run_weekly_review_and_notify(bot) -> None:
    if bot is None:
        return
    try:
        review = await weekly_style_review()
        if not review:
            return
        from app.ops import notify_admins

        await notify_admins(bot, f"📝 Вычитка стиля недели:\n{review}")
    except Exception:
        logger.exception("Еженедельная вычитка стиля не удалась")
