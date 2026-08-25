"""Еженедельная L2-вычитка стиля: сборка корпуса и гейты."""

from __future__ import annotations


from app import style_review
from app.config import settings


async def test_review_returns_editor_list(monkeypatch) -> None:
    from app.db import SessionLocal
    from app.models import StoryBeat

    monkeypatch.setattr(settings, "use_free_story_llm", True)
    # Корпус: минимум три дня канона (глобальная БД).
    async with SessionLocal() as db:
        for i in range(4):
            db.add(StoryBeat(
                day_index=95_000 + i,
                winning_title=f"Тропа {i}",
                winning_text="Стая прошла тропу и оставила след." * 3,
                win_rule="majority",
                vote_counts="{}",
            ))
        await db.commit()

    async def fake_chat(messages, timeout=90):
        # Промпт должен нести корпус глав.
        assert "Тропа 0" in messages[1]["content"]
        return (
            {"choices": [{"message": {"content": "- повтор «мисок» в 4 главах\n- канцелярит в эпилогах\n- сбой лица в день 5"}}]},
            "test-model",
        )

    monkeypatch.setattr("app.story._chat_completion", fake_chat)
    text = await style_review.weekly_style_review()
    assert text is not None
    assert "повтор" in text and len(text) <= 1500
    # Кластер после себя чистит.
    async with SessionLocal() as db:
        from sqlalchemy import delete as _d

        await db.execute(_d(StoryBeat).where(StoryBeat.day_index >= 95_000))
        await db.commit()


async def test_review_gates(monkeypatch) -> None:
    monkeypatch.setattr(settings, "use_free_story_llm", False)
    assert await style_review.weekly_style_review() is None

    async def silent(messages, timeout=90):
        return None

    monkeypatch.setattr(settings, "use_free_story_llm", True)
    monkeypatch.setattr("app.story._chat_completion", silent)
    assert await style_review.weekly_style_review() is None


async def test_run_and_notify_sends_to_admins(monkeypatch) -> None:
    from types import SimpleNamespace

    sent: list[str] = []

    async def fake_notify(bot, text) -> None:
        sent.append(text)

    async def fake_review():
        return "- проблема раз"

    bot = SimpleNamespace()

    monkeypatch.setattr(style_review, "weekly_style_review", fake_review)
    monkeypatch.setattr("app.ops.notify_admins", fake_notify)

    settings.admin_ids = settings.admin_ids or ""
    await style_review.run_weekly_review_and_notify(bot)
    assert len(sent) == 1 and "проблема раз" in sent[0]

    # Модель молчит — алерта нет.
    async def none_review():
        return None

    monkeypatch.setattr(style_review, "weekly_style_review", none_review)
    await style_review.run_weekly_review_and_notify(bot)
    assert len(sent) == 1
