"""Карточный конвейер дня: payload-карты, офлайн-достройка, память мира.

Вынесено из rounds.py (слой 4-6): единая нормализация любой карты под
Card-модель, офлайн-пул тропов при нехватке карт главы и компактный
world_block для мега-промпта. Модуль без зависимостей на rounds — живёт
на lore, поэтому его независимо тестировать и переиспользовать.
"""

from __future__ import annotations

import json
import secrets

from app.lore import _cards, card_rich_payload


def _card_payload(card: dict, position: int, day_index: int) -> dict:
    """Payload-словарь под Card-модель.

    Трата ресурсов, урон и числовое доверие отключены: food_cost/water_cost/
    health_risk/trust_change всегда 0. LLM-карты главы несут
    emotional_consequence/npc_reactions — явные значения уважаются; настоящие
    пустоты (None/пустая строка/отсутствие) выравниваются деривацией
    lore.card_rich_payload по архетипу и названию.
    """
    rich = card_rich_payload(
        str(card.get("title", "")),
        str(card.get("tag", "care")),
        day_index,
    )
    npc = card.get("npc_reactions") or rich["npc_reactions"]
    return {
        "position": position,
        "title": card["title"],
        "description": card["description"],
        "consequence": str(card.get("consequence", "")),
        "tag": card.get("tag", "care"),
        "image_path": "",
        "food_cost": 0,
        "water_cost": 0,
        "health_risk": 0,
        "trust_change": 0,
        "emotional_consequence": str(
            card.get("emotional_consequence") or rich["emotional_consequence"]
        ),
        "npc_reactions_json": json.dumps(npc, ensure_ascii=False),
    }


def _assemble_cards(chapter: dict, day_index: int) -> list[dict]:
    """Карты дня из единой генерации главы (chapter["cards"]), достроенные
    офлайн-пулом при нехватке. Возвращает payload-словари под Card-модель."""
    cards = []
    for card in chapter.get("cards") or []:
        if not isinstance(card, dict):
            continue
        title = str(card.get("title", "")).strip()
        description = str(card.get("description", "")).strip()
        if title and description:
            cards.append(card)
    if len(cards) < 3:
        rng = secrets.SystemRandom()
        used = {str(c["title"]).strip().lower() for c in cards}
        for pool_card in _cards(rng, day_index):
            if len(cards) >= 3:
                break
            key = str(pool_card.title).strip().lower()
            if key in used:
                continue
            cards.append(
                {
                    "title": pool_card.title,
                    "description": pool_card.description,
                    "consequence": pool_card.consequence,
                    "tag": pool_card.tag,
                }
            )
            used.add(key)
    return [
        _card_payload(card, position, day_index)
        for position, card in enumerate(cards[:3])
    ]


def _world_block_text(world_ctx) -> str | None:
    """Компактная память живого мира для мега-промпта главы.

    Мир приходит в генерацию ДО написания главы (один вызов вместо
    отдельной AI-локации, патчившей текст задним числом). None — мир пуст.
    Общий бюджет ~600 символов и сортировка по важности: растущий лабиринт
    не раздувает контекст, а в промпт попадают самые посещаемые места.
    """
    if world_ctx is None:
        return None
    budget = 600
    parts = []
    mood = getattr(world_ctx, "world_mood", None)
    if mood:
        parts.append(f"- настроение лабиринта: {mood}")
    threads = getattr(world_ctx, "open_threads", None) or []
    if threads:
        parts.append("- незакрытые сюжетные линии: " + "; ".join(str(t)[:60] for t in threads[:3]))
    locs = getattr(world_ctx, "active_locations", None) or []
    if locs:
        locs = sorted(locs, key=lambda loc: loc.get("times_visited", 0), reverse=True)
        names = [
            f"{str(loc['name'])[:40]} ({loc.get('times_visited', 0)} посещ.)"
            for loc in locs[:5]
        ]
        parts.append("- известные стае места: " + ", ".join(names))
    chars = getattr(world_ctx, "active_characters", None) or []
    if chars:
        cnames = [
            f"{str(char['name'])[:40]}"
            for char in chars[:5]
        ]
        parts.append("- долгожители лабиринта: " + ", ".join(cnames))
    if not parts:
        return None
    block = "\n".join(parts)
    if len(block) <= budget:
        return block
    kept, total = [], 0
    for part in parts:
        if total + len(part) + 1 > budget:
            break
        kept.append(part)
        total += len(part) + 1
    return "\n".join(kept)