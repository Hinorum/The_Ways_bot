"""Прогон нескольких дней без Telegram: лор, скрытый закон, победитель."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal, init_db
from app.models import LoreEcho, Player, Vote
from app.rounds import close_voting, create_next_round, finish_tally, get_latest_round, pick_winner
from app.tally import award_points, format_results


async def simulate(days: int, skip_images: bool) -> None:
    if skip_images:
        settings.use_free_images = False
        settings.use_free_story_llm = False
    Path("data").mkdir(exist_ok=True)
    Path(settings.media_dir).mkdir(parents=True, exist_ok=True)
    await init_db()
    async with SessionLocal() as session:
        for day in range(1, days + 1):
            round_row = await create_next_round(session)
            print("=" * 60)
            print(round_row.chapter_title)
            print(round_row.chapter_text)
            print()
            for card in sorted(round_row.cards, key=lambda item: item.position):
                print(f"  {card.position + 1}. {card.title} — {card.description}")
            surfaced = (
                await session.execute(
                    select(LoreEcho).where(LoreEcho.surfaced_day == round_row.day_index)
                )
            ).scalars().all()
            for echo in surfaced:
                print(f"  Эхо [{echo.kind}]: {echo.title}")
            print(f"\nЗакон дня объявлен (commitment {round_row.rule_commitment[:16]}…)")
            votes = [0, 1, 1, 1, 2, 2]
            for index, position in enumerate(votes, start=1):
                player = await session.get(Player, index)
                if player is None:
                    session.add(Player(id=index, username=f"p{index}", first_name=f"Игрок {index}"))
                    await session.flush()
                session.add(Vote(round_id=round_row.id, player_id=index, card_position=position))
            await session.commit()
            await close_voting(session, round_row)
            round_row, _closed_here = await finish_tally(session, round_row)
            await award_points(session, round_row)
            print()
            print(format_results(round_row))
            print()
        assert pick_winner({0: 1, 1: 3, 2: 2}, round_row.win_rule) in {0, 1, 2}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--skip-images", action="store_true")
    args = parser.parse_args()
    asyncio.run(simulate(args.days, args.skip_images))


if __name__ == "__main__":
    main()
